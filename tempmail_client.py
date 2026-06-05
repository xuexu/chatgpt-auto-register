#!/usr/bin/env python3
"""Client for dreamhunter2333/cloudflare_temp_email deployments.

The worker exposes two useful surfaces for this project:
  - POST /admin/new_address: create an address and get its address JWT.
  - GET /api/parsed_mails: poll parsed inbox entries with the address JWT.
"""

import html
import os
import re
import secrets
import string
import time
from typing import Any, Dict, List, Optional

import requests


DEFAULT_USED_FILE = os.path.join(os.path.dirname(__file__), "used_tempmails.json")
DEFAULT_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "tempmail_tokens.json")


class TempMailClient:
    """Cloudflare Temp Email API client."""

    def __init__(
        self,
        base_url: str,
        admin_auth: str = "",
        domain: str = "",
        site_password: str = "",
        used_file: str = DEFAULT_USED_FILE,
        token_file: str = DEFAULT_TOKEN_FILE,
        verbose: bool = False,
    ):
        if not base_url:
            raise ValueError("tempmail base_url is required")
        self.base_url = base_url.rstrip("/")
        self.admin_auth = admin_auth
        self.domain = domain
        self.site_password = site_password
        self.used_file = used_file
        self.token_file = token_file
        self.verbose = verbose
        self._tokens: Dict[str, str] = self._load_tokens()
        self._used = self._load_used()
        self.last_created: Dict[str, str] = {}

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [TempMail] {msg}")

    def _load_used(self) -> set:
        if not os.path.isfile(self.used_file):
            return set()
        try:
            import json

            with open(self.used_file, "r", encoding="utf-8") as f:
                return set(json.load(f).get("used", []))
        except Exception:
            return set()

    def _save_used(self):
        import json

        with open(self.used_file, "w", encoding="utf-8") as f:
            json.dump({"used": sorted(self._used)}, f, indent=2, ensure_ascii=False)

    def _load_tokens(self) -> Dict[str, str]:
        if not os.path.isfile(self.token_file):
            return {}
        try:
            import json

            with open(self.token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k).lower(): str(v) for k, v in (data.get("tokens") or {}).items()}
        except Exception:
            return {}

    def _save_tokens(self):
        import json

        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump({"tokens": self._tokens}, f, indent=2, ensure_ascii=False)

    @staticmethod
    def random_name(length: int = 10) -> str:
        chars = string.ascii_lowercase + string.digits
        return "".join(secrets.choice(chars) for _ in range(length))

    def _admin_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.admin_auth:
            headers["x-admin-auth"] = self.admin_auth
        if self.site_password:
            headers["x-custom-auth"] = self.site_password
        return headers

    def _address_headers(self, jwt: str) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json",
            "x-lang": "en",
        }
        if self.site_password:
            headers["x-custom-auth"] = self.site_password
        return headers

    @staticmethod
    def _json_or_text(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except Exception:
            return resp.text

    @staticmethod
    def _unwrap_payload(data: Any) -> Dict:
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_address_data(data: Any) -> Dict[str, str]:
        payload = TempMailClient._unwrap_payload(data)
        email = (
            payload.get("address")
            or payload.get("email")
            or payload.get("name")
            or ""
        )
        jwt = payload.get("jwt") or payload.get("token") or payload.get("address_jwt") or ""
        address_id = payload.get("address_id") or payload.get("id") or ""
        password = payload.get("password") or ""
        return {
            "email": str(email),
            "jwt": str(jwt),
            "address_id": str(address_id),
            "password": str(password),
        }

    def create_address(
        self,
        name: str = "",
        domain: str = "",
        enable_prefix: bool = False,
        enable_random_subdomain: bool = False,
        retries: int = 5,
    ) -> Dict[str, str]:
        """Create a mailbox via admin API.

        Returns {"email": ..., "jwt": ..., "address_id": ...}.
        """
        last_error = ""
        endpoint = "/admin/new_address" if self.admin_auth else "/api/new_address"
        for i in range(max(1, retries)):
            local = name or self.random_name()
            payload = {
                "name": local,
                "domain": domain or self.domain,
                "enablePrefix": bool(enable_prefix),
            }
            if enable_random_subdomain:
                payload["enableRandomSubdomain"] = True

            self._log(
                f"creating address via {endpoint} name={local} "
                f"domain={payload.get('domain') or '(auto)'}"
            )
            resp = requests.post(
                f"{self.base_url}{endpoint}",
                json=payload,
                headers=self._admin_headers(),
                timeout=30,
            )
            if resp.ok:
                data = self._json_or_text(resp)
                parsed = self._extract_address_data(data)
                email = parsed["email"]
                jwt = parsed["jwt"]
                if email and jwt:
                    self._tokens[email.lower()] = jwt
                    self._save_tokens()
                    self.last_created = parsed
                    return parsed
                last_error = f"unexpected response: {data}"
            else:
                last_error = resp.text[:300]

            if name:
                break
            time.sleep(0.5 + i * 0.5)

        raise RuntimeError(f"create tempmail address failed: {last_error}")

    def get_available_email(self, **kwargs) -> str:
        data = self.create_address(**kwargs)
        email = data["email"]
        if email.lower() in self._used:
            self._log(f"created address was already marked used locally: {email}")
        return email

    def mark_used(self, email: str):
        email = email.strip().lower()
        if email and email not in self._used:
            self._used.add(email)
            self._save_used()
            self._log(f"marked used: {email}")

    def register_token(self, email: str, jwt: str):
        if email and jwt:
            self._tokens[email.strip().lower()] = jwt.strip()
            self._save_tokens()

    def get_token(self, email: str, jwt: str = "") -> str:
        token = jwt or self._tokens.get(email.strip().lower(), "")
        if not token:
            raise ValueError(f"missing address JWT for {email}")
        return token

    def list_parsed_mails(self, email: str, jwt: str = "", limit: int = 20, offset: int = 0) -> List[Dict]:
        token = self.get_token(email, jwt)
        resp = requests.get(
            f"{self.base_url}/api/parsed_mails",
            params={"limit": limit, "offset": offset},
            headers=self._address_headers(token),
            timeout=30,
        )
        if resp.status_code == 429:
            raise RuntimeError("tempmail rate limited")
        if not resp.ok:
            raise RuntimeError(f"list parsed mails failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("results") or data.get("data") or data.get("mails") or []
        return []

    @staticmethod
    def extract_code(text: str, exclude_codes: Optional[List[str]] = None) -> Optional[str]:
        exclude = {str(c) for c in (exclude_codes or []) if c}
        for code in re.findall(r"(?<!\d)(\d{6})(?!\d)", text or ""):
            if code not in exclude:
                return code
        return None

    def poll_mail_for_code(
        self,
        target_email: str,
        jwt: str = "",
        sender_filters: Optional[List[str]] = None,
        keyword: str = "",
        timeout: int = 60,
        interval: int = 5,
        exclude_codes: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Poll parsed mails and return the first 6-digit verification code."""
        sender_filters = [s.lower() for s in (sender_filters or []) if s]
        keyword = (keyword or "").lower()
        seen_ids = set()
        start = time.time()
        self._log(f"polling {target_email} for code timeout={timeout}s")

        while time.time() - start < timeout:
            try:
                for mail in self.list_parsed_mails(target_email, jwt=jwt):
                    mail_id = str(mail.get("id") or mail.get("message_id") or "")
                    if mail_id and mail_id in seen_ids:
                        continue
                    if mail_id:
                        seen_ids.add(mail_id)

                    sender = str(mail.get("sender") or mail.get("source") or "").lower()
                    subject = str(mail.get("subject") or "")
                    text = str(mail.get("text") or "")
                    html_body = html.unescape(re.sub(r"<[^>]+>", " ", str(mail.get("html") or "")))
                    haystack = f"{sender}\n{subject}\n{text}\n{html_body}"
                    haystack_lower = haystack.lower()

                    if sender_filters and not any(s in sender for s in sender_filters):
                        continue
                    if keyword and keyword not in haystack_lower:
                        continue

                    code = self.extract_code(haystack, exclude_codes=exclude_codes)
                    if code:
                        self._log(f"code found: {code}")
                        return code
            except Exception as e:
                self._log(f"poll failed: {e}")

            time.sleep(interval)

        self._log("code polling timed out")
        return None


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Cloudflare Temp Email client")
    p.add_argument("--base-url", required=True)
    p.add_argument("--admin-auth", default="")
    p.add_argument("--domain", default="")
    p.add_argument("--site-password", default="")
    p.add_argument("--email", default="")
    p.add_argument("--jwt", default="")
    p.add_argument("--command", choices=["create", "code"], default="create")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    client = TempMailClient(
        args.base_url,
        admin_auth=args.admin_auth,
        domain=args.domain,
        site_password=args.site_password,
        verbose=args.verbose,
    )
    if args.command == "create":
        print(client.create_address())
    else:
        print(client.poll_mail_for_code(args.email, jwt=args.jwt, timeout=120))
