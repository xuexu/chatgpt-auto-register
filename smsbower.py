"""
SMSBower API client for automated SMS verification code retrieval.
API docs: https://smsbower.app/api?page=client
"""

import time
import json
from typing import Optional
import requests


SMSBOWER_API = "https://smsbower.page/stubs/handler_api.php"
COUNTRY_ALIASES = {
    "indonesia": "6",
    "印度尼西亚": "6",
    "印尼": "6",
}


def normalize_service(service: str) -> str:
    """Normalize legacy/local service aliases to SMSBower API service codes."""
    code = str(service or "").strip().lower()
    if code in ("", "openai"):
        return "dr"
    return code


def _call(api_key: str, params: dict) -> str:
    params["api_key"] = api_key
    r = requests.get(SMSBOWER_API, params=params, timeout=30)
    text = r.text.strip()
    # SMSBower sometimes returns JSON errors instead of text
    if text.startswith("{") and "message" in text:
        try:
            data = json.loads(text)
            if data.get("message") == "No access":
                raise RuntimeError("SMSBower: API key invalid or no access")
        except json.JSONDecodeError:
            pass
    return text



class SmsBower:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.activation_id: Optional[str] = None
        self.phone: Optional[str] = None

    def balance(self) -> str:
        return _call(self.api_key, {"action": "getBalance"})

    def list_services(self) -> list[dict]:
        r = requests.get(
            SMSBOWER_API,
            params={"api_key": self.api_key, "action": "getServicesList"},
            timeout=15,
        )
        return r.json().get("services", [])

    def list_countries(self) -> list[dict]:
        r = requests.get(
            SMSBOWER_API,
            params={"api_key": self.api_key, "action": "getCountries"},
            timeout=15,
        )
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("countries"), list):
                return data["countries"]
            rows = []
            for key, value in data.items():
                if isinstance(value, dict):
                    row = dict(value)
                    row.setdefault("id", key)
                else:
                    row = {"id": key, "name": str(value)}
                rows.append(row)
            return rows
        return []

    def top_countries_by_service(self, service: str) -> dict:
        service = normalize_service(service)
        r = requests.get(
            SMSBOWER_API,
            params={
                "api_key": self.api_key,
                "action": "getTopCountriesByService",
                "service": service,
            },
            timeout=15,
        )
        data = r.json()
        if isinstance(data, dict):
            for key in ("countries", "data", "result"):
                if isinstance(data.get(key), dict):
                    return data[key]
                if isinstance(data.get(key), list):
                    rows = {}
                    for row in data[key]:
                        if isinstance(row, dict):
                            cid = row.get("id") or row.get("code") or row.get("country") or row.get("name")
                            if cid is not None:
                                rows[str(cid)] = row
                    return rows
        return data if isinstance(data, dict) else {}

    def find_service(self, keyword: str) -> list[dict]:
        services = self.list_services()
        kw = keyword.lower()
        return [
            s for s in services
            if kw in s.get("code", "").lower()
            or kw in s.get("name", "").lower()
        ]

    def resolve_country_id(self, country: str) -> str:
        raw = str(country or "").strip()
        if not raw:
            return raw
        if raw.isdigit():
            return raw
        needle = raw.lower()
        if needle in COUNTRY_ALIASES:
            return COUNTRY_ALIASES[needle]
        for item in self.list_countries():
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("code") or item.get("country") or "").strip()
            aliases = [
                str(item.get(k) or "").strip().lower()
                for k in ("name", "eng", "chn", "rus", "title")
            ]
            aliases.append(cid.lower())
            if needle in aliases and cid:
                return cid
        return raw

    def get_cheapest_provider(
        self, service: str = "openai", country: str = "151"
    ) -> tuple[str, float]:
        service = normalize_service(service)
        country = self.resolve_country_id(country)
        r = requests.get(
            SMSBOWER_API,
            params={
                "api_key": self.api_key,
                "action": "getPricesV3",
                "service": service,
                "country": country,
            },
            timeout=15,
        )
        data = r.json()
        providers = data.get(country, {}).get(service, {})
        cheapest, cheapest_price = "", 999.0
        for pid, info in providers.items():
            price = float(info.get("price", 999))
            if price < cheapest_price:
                cheapest_price = price
                cheapest = pid
        return cheapest, cheapest_price

    def get_number(
        self,
        service: str = "openai",
        country: str = "151",
        provider_ids: str = "",
        min_price: str = "",
        max_price: str = "",
    ) -> tuple[str, str]:
        service = normalize_service(service)
        country = self.resolve_country_id(country)
        params = {"action": "getNumber", "service": service, "country": country}
        if provider_ids:
            params["providerIds"] = provider_ids
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price
        resp = _call(self.api_key, params)

        if resp.startswith("ACCESS_NUMBER:"):
            _, aid, phone = resp.split(":")
            self.activation_id = aid
            self.phone = phone
            return aid, phone
        raise RuntimeError(f"getNumber failed: {resp}")

    def set_ready(self):
        _call(self.api_key, {
            "action": "setStatus", "status": "1", "id": self.activation_id
        })

    def wait_code(self, timeout: int = 300, interval: int = 3) -> Optional[str]:
        if not self.activation_id:
            raise RuntimeError("No active activation")
        started = time.time()
        while time.time() - started < timeout:
            resp = _call(self.api_key, {
                "action": "getStatus", "id": self.activation_id
            })
            if resp.startswith("STATUS_OK:"):
                return resp.split(":", 1)[1].strip()
            elif resp == "STATUS_CANCEL":
                raise RuntimeError("Activation cancelled (may have timed out)")
            time.sleep(interval)
        return None

    def complete(self):
        _call(self.api_key, {
            "action": "setStatus", "status": "6", "id": self.activation_id
        })

    def cancel(self):
        try:
            _call(self.api_key, {
                "action": "setStatus", "status": "8", "id": self.activation_id
            })
        except Exception:
            pass
