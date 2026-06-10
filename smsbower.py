"""
SMSBower API client for automated SMS verification code retrieval.
API docs: https://smsbower.app/api?page=client
"""

import time
import json
from typing import Optional
import requests


SMSBOWER_API = "https://smsbower.page/stubs/handler_api.php"

COUNTRY_ISO_BY_EN = {
    "afghanistan": "AF", "albania": "AL", "algeria": "DZ", "angola": "AO",
    "argentina": "AR", "argentinas": "AR", "armenia": "AM", "australia": "AU",
    "austria": "AT", "azerbaijan": "AZ", "bahamas": "BS", "bahrain": "BH",
    "bangladesh": "BD", "belarus": "BY", "belgium": "BE", "belize": "BZ",
    "benin": "BJ", "bhutan": "BT", "bolivia": "BO", "bosnia": "BA",
    "botswana": "BW", "brazil": "BR", "bulgaria": "BG", "burkina faso": "BF",
    "burundi": "BI", "cambodia": "KH", "cameroon": "CM", "canada": "CA",
    "chad": "TD", "chile": "CL", "china": "CN", "colombia": "CO",
    "congo": "CG", "costa rica": "CR", "croatia": "HR", "cyprus": "CY",
    "czech": "CZ", "czech republic": "CZ", "denmark": "DK", "dominican republic": "DO",
    "ecuador": "EC", "egypt": "EG", "england": "GB", "estonia": "EE",
    "ethiopia": "ET", "finland": "FI", "france": "FR", "georgia": "GE",
    "germany": "DE", "ghana": "GH", "greece": "GR", "guatemala": "GT",
    "guinea": "GN", "haiti": "HT", "honduras": "HN", "hong kong": "HK",
    "hungary": "HU", "india": "IN", "indonesia": "ID", "iran": "IR",
    "iraq": "IQ", "ireland": "IE", "israel": "IL", "italy": "IT",
    "ivory coast": "CI", "jamaica": "JM", "japan": "JP", "jordan": "JO",
    "kazakhstan": "KZ", "kenya": "KE", "kuwait": "KW", "kyrgyzstan": "KG",
    "laos": "LA", "latvia": "LV", "lebanon": "LB", "lesotho": "LS",
    "liberia": "LR", "libya": "LY", "lithuania": "LT", "luxembourg": "LU",
    "macau": "MO", "madagascar": "MG", "malawi": "MW", "malaysia": "MY",
    "maldives": "MV", "mali": "ML", "mauritania": "MR", "mauritius": "MU",
    "mexico": "MX", "moldova": "MD", "moldova, republic of": "MD", "mongolia": "MN", "montenegro": "ME",
    "morocco": "MA", "mozambique": "MZ", "myanmar": "MM", "namibia": "NA",
    "nepal": "NP", "netherlands": "NL", "new zealand": "NZ", "nicaragua": "NI",
    "niger": "NE", "nigeria": "NG", "norway": "NO", "oman": "OM",
    "pakistan": "PK", "panama": "PA", "papua new guinea": "PG",
    "papua new gvineya": "PG", "paraguay": "PY", "peru": "PE",
    "philippines": "PH", "poland": "PL", "portugal": "PT", "puerto rico": "PR",
    "qatar": "QA", "romania": "RO", "russia": "RU", "russian federation": "RU",
    "rwanda": "RW", "saudi arabia": "SA", "senegal": "SN", "serbia": "RS",
    "singapore": "SG", "slovakia": "SK", "slovenia": "SI", "somalia": "SO",
    "south africa": "ZA", "south korea": "KR", "spain": "ES", "sri lanka": "LK",
    "sudan": "SD", "sweden": "SE", "switzerland": "CH", "syria": "SY",
    "taiwan": "TW", "tajikistan": "TJ", "tanzania": "TZ", "thailand": "TH",
    "togo": "TG", "tunisia": "TN", "turkey": "TR", "turkmenistan": "TM",
    "uganda": "UG", "ukraine": "UA", "united arab emirates": "AE",
    "united kingdom": "GB", "united states": "US", "united states (virtual)": "US", "usa": "US",
    "uruguay": "UY", "uzbekistan": "UZ", "venezuela": "VE",
    "viet nam": "VN", "vietnam": "VN", "yemen": "YE", "zambia": "ZM",
    "zimbabwe": "ZW",
}


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

    def find_service(self, keyword: str) -> list[dict]:
        services = self.list_services()
        kw = keyword.lower()
        return [
            s for s in services
            if kw in s.get("code", "").lower()
            or kw in s.get("name", "").lower()
        ]

    def list_countries(self) -> list[dict]:
        r = requests.get(
            SMSBOWER_API,
            params={"api_key": self.api_key, "action": "getCountries"},
            timeout=20,
        )
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
            rows = []
            for key, value in data.items():
                item = dict(value)
                title = item.get("title") or item.get("eng") or item.get("name") or ""
                iso = item.get("iso") or COUNTRY_ISO_BY_EN.get(str(title).strip().lower(), "")
                item.setdefault("id", key)
                item["title"] = title
                item["iso"] = iso
                rows.append(item)
            return rows
        for key in ("countries", "data", "items", "result"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, list):
                return value
        return []

    def get_prices_v3(self, service: str = "dr", country: str = "") -> dict:
        params = {
            "api_key": self.api_key,
            "action": "getPricesV3",
            "service": service,
        }
        if country:
            params["country"] = country
        r = requests.get(SMSBOWER_API, params=params, timeout=20)
        return r.json()

    def list_country_prices(self, service: str = "dr") -> list[dict]:
        countries = self.list_countries()
        prices = self.get_prices_v3(service=service)
        country_meta = {}
        for item in countries:
            code = str(item.get("id") or item.get("code") or item.get("activate_org_code") or "").strip()
            if not code:
                continue
            country_meta[code] = {
                "code": code,
                "title": item.get("title") or item.get("eng") or "",
                "iso": item.get("iso") or COUNTRY_ISO_BY_EN.get(str(item.get("title") or item.get("eng") or "").strip().lower(), ""),
                "prefix": item.get("prefix", ""),
            }

        rows = []
        for code, service_map in (prices or {}).items():
            code = str(code)
            providers = {}
            if isinstance(service_map, dict):
                providers = service_map.get(service) or service_map.get(str(service)) or {}
                if "price" in service_map and "count" in service_map:
                    providers = {"default": service_map}
            min_price = None
            total_count = 0
            if isinstance(providers, dict):
                for info in providers.values():
                    if not isinstance(info, dict):
                        continue
                    try:
                        count = int(info.get("count", 0) or 0)
                    except (TypeError, ValueError):
                        count = 0
                    if count <= 0:
                        continue
                    total_count += count
                    try:
                        price = float(info.get("price"))
                    except (TypeError, ValueError):
                        continue
                    if min_price is None or price < min_price:
                        min_price = price
            if min_price is None:
                continue
            meta = country_meta.get(code, {"code": code, "title": "", "iso": "", "prefix": ""})
            rows.append({
                **meta,
                "min_price": min_price,
                "count": total_count,
            })
        rows.sort(key=lambda x: (x["min_price"], x.get("title") or x["code"]))
        return rows

    def get_cheapest_provider(
        self, service: str = "dr", country: str = "151"
    ) -> tuple[str, float]:
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
        service: str = "dr",
        country: str = "151",
        provider_ids: str = "",
        min_price: str = "",
        max_price: str = "",
    ) -> tuple[str, str]:
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
