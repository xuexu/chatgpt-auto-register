#!/usr/bin/env python3
"""cloudflare_temp_email Address JWT client."""

from __future__ import annotations

import json
import html as html_lib
import random
import re
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


DEFAULT_USED_FILE = str(Path(__file__).parent / "tempmail_used.json")
CODE_RE = re.compile(r"(?<!\d)(\d{6,8})(?!\d)")
STANDALONE_CODE_RE = re.compile(r"^\s*(\d{6,8})\s*$")
STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
PHRASE_CODE_RE = re.compile(
    r"(?:verification\s+code|temporary\s+code|security\s+code|验证码|驗證碼|代码|代碼)"
    r"[\s\S]{0,120}?(?<!\d)(\d{6,8})(?!\d)",
    re.IGNORECASE,
)
FIRST_NAMES = (
    "amelia", "olivia", "emma", "ava", "sophia", "isabella", "mia", "charlotte",
    "evelyn", "harper", "luna", "ella", "scarlett", "grace", "chloe", "victoria",
    "james", "liam", "noah", "oliver", "elijah", "william", "henry", "lucas",
    "benjamin", "theodore", "jack", "levi", "alexander", "jackson", "daniel",
)
LAST_NAMES = (
    "smith", "johnson", "brown", "taylor", "anderson", "thomas", "martin",
    "lee", "walker", "hall", "allen", "young", "king", "wright", "scott",
    "green", "baker", "adams", "nelson", "carter", "mitchell", "perez",
    "roberts", "turner", "phillips", "campbell", "parker", "evans",
)


@dataclass(frozen=True)
class TempMailAccount:
    base_url: str
    jwt: str
    email: str = ""
    site_password: str = ""


def parse_tempmail_pool(pool: str, default_base_url: str = "", default_site_password: str = "") -> list[TempMailAccount]:
    accounts: list[TempMailAccount] = []
    for raw_line in str(pool or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("----")]
        if len(parts) == 1:
            base_url, email, jwt, site_password = default_base_url, "", parts[0], default_site_password
        elif len(parts) == 2:
            base_url, email, jwt, site_password = default_base_url, parts[0], parts[1], default_site_password
        else:
            base_url, email, jwt = parts[0], parts[1], parts[2]
            site_password = parts[3] if len(parts) >= 4 else default_site_password
        if base_url and jwt:
            accounts.append(TempMailAccount(base_url=base_url.rstrip("/"), jwt=jwt, email=email, site_password=site_password))
    return accounts


def html_to_visible_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = STYLE_SCRIPT_RE.sub(" ", text)
    text = HTML_COMMENT_RE.sub(" ", text)
    text = re.sub(r"</(p|div|tr|td|th|table|br|h[1-6]|li)>", "\n", text, flags=re.IGNORECASE)
    text = TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def extract_verification_code(row: dict) -> str:
    """Prefer visible OpenAI OTP text over arbitrary CSS/HTML numbers."""
    direct_fields = ("code", "otp", "verification_code", "verificationCode")
    for key in direct_fields:
        value = str(row.get(key) or "").strip()
        match = STANDALONE_CODE_RE.match(value)
        if match:
            return match.group(1)

    subject = str(row.get("subject") or "")
    sender = str(row.get("sender") or row.get("source") or "")
    text_body = str(row.get("text") or "")
    html_body = str(row.get("html") or "")
    visible_parts = [html_to_visible_text(html_body), html_to_visible_text(text_body)]
    visible = "\n".join(part for part in visible_parts if part)

    is_openai_mail = any(
        marker in f"{sender}\n{subject}\n{visible[:1000]}".lower()
        for marker in ("openai", "verification code", "temporary code", "验证码", "驗證碼")
    )

    if is_openai_mail:
        for line in visible.splitlines():
            match = STANDALONE_CODE_RE.match(line)
            if match:
                return match.group(1)

    for source in (visible, f"{subject}\n{visible}", f"{subject}\n{text_body}\n{html_body}"):
        match = PHRASE_CODE_RE.search(source)
        if match:
            return match.group(1)

    haystack = "\n".join(str(row.get(k) or "") for k in ("sender", "source", "subject", "text", "html"))
    match = CODE_RE.search(haystack)
    return match.group(1) if match else ""


class TempMailClient:
    def __init__(
        self,
        base_url: str,
        jwt: str = "",
        site_password: str = "",
        admin_password: str = "",
        domain: str = "",
        name_prefix: str = "",
        pool: str = "",
        used_file: str = DEFAULT_USED_FILE,
        verbose: bool = False,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.jwt = (jwt or "").strip()
        self.site_password = site_password or ""
        self.admin_password = admin_password or ""
        self.domain = (domain or "").strip()
        self.name_prefix = (name_prefix or "").strip()
        self.pool = pool or ""
        self.used_file = used_file
        self.verbose = verbose
        self._used = self._load_used()

    def _log(self, msg: str):
        if self.verbose:
            print(f"  [TempMail] {msg}")

    def _load_used(self) -> set[str]:
        path = Path(self.used_file)
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return set(str(x).lower() for x in data.get("used", []))
        except Exception:
            return set()

    def _save_used(self):
        Path(self.used_file).write_text(
            json.dumps({"used": sorted(self._used)}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def mark_used(self, email: str):
        key = (email or "").strip().lower()
        if key and key not in self._used:
            self._used.add(key)
            self._save_used()
            self._log(f"标记已用: {email}")

    def _headers(self, account: TempMailAccount | None = None) -> dict:
        account = account or TempMailAccount(self.base_url, self.jwt, site_password=self.site_password)
        headers = {
            "Authorization": f"Bearer {account.jwt}",
            "Accept": "application/json",
            "x-lang": "zh",
        }
        if account.site_password:
            headers["x-custom-auth"] = account.site_password
        return headers

    def _create_headers(self, admin: bool = False) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-lang": "zh",
        }
        if self.site_password:
            headers["x-custom-auth"] = self.site_password
        if admin and self.admin_password:
            headers["x-admin-auth"] = self.admin_password
        return headers

    def _get_json(self, account: TempMailAccount, path: str, params: dict | None = None) -> dict:
        resp = requests.get(
            f"{account.base_url}{path}",
            headers=self._headers(account),
            params=params or {},
            timeout=30,
        )
        if resp.status_code == 401:
            raise RuntimeError("tempmail Address JWT 无效或已过期")
        if resp.status_code == 429:
            raise RuntimeError("tempmail 请求被限流")
        resp.raise_for_status()
        return resp.json()

    def _post_json(self, path: str, body: dict, admin: bool = False) -> dict:
        if not self.base_url:
            raise RuntimeError("tempmail API 地址未配置")
        resp = requests.post(
            f"{self.base_url}{path}",
            headers=self._create_headers(admin=admin),
            json=body,
            timeout=30,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError(f"tempmail 创建邮箱认证失败: HTTP {resp.status_code} {resp.text[:200]}")
        if resp.status_code == 429:
            raise RuntimeError("tempmail 请求被限流")
        try:
            data = resp.json()
        except Exception as exc:
            if not resp.ok:
                raise RuntimeError(f"tempmail 请求失败: HTTP {resp.status_code} {resp.text[:200]}") from exc
            raise RuntimeError(f"tempmail 返回非 JSON: {resp.text[:200]}") from exc
        if not resp.ok:
            message = data.get("message") or data.get("error") or resp.text[:200]
            raise RuntimeError(f"tempmail 请求失败: HTTP {resp.status_code} {message}")
        return data

    def _random_name(self) -> str:
        prefix = re.sub(r"[^a-zA-Z0-9._-]+", "", self.name_prefix)[:24]
        if prefix:
            suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
            return f"{prefix}{suffix}"
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        digits = "".join(random.choices(string.digits, k=random.randint(2, 4)))
        return f"{first}{last}{digits}"

    @staticmethod
    def _account_from_create_response(base_url: str, site_password: str, data: dict) -> TempMailAccount:
        jwt = str(data.get("jwt") or data.get("token") or data.get("address_jwt") or "").strip()
        email = str(data.get("address") or data.get("email") or data.get("name") or "").strip()
        if not jwt:
            raise RuntimeError(f"tempmail 创建邮箱未返回 jwt: {data}")
        if not email:
            raise RuntimeError(f"tempmail 创建邮箱未返回 address: {data}")
        return TempMailAccount(base_url=base_url, jwt=jwt, email=email, site_password=site_password)

    def create_address(self, name: str = "", domain: str = "", enable_prefix: bool = True) -> TempMailAccount:
        """Create one mailbox and return its Address JWT account."""
        name = (name or "").strip() or self._random_name()
        domain = (domain or "").strip() or self.domain

        if self.admin_password:
            body = {"enablePrefix": bool(enable_prefix), "name": name}
            if domain:
                body["domain"] = domain
            data = self._post_json("/admin/new_address", body, admin=True)
        else:
            body = {"name": name}
            if domain:
                body["domain"] = domain
            data = self._post_json("/api/new_address", body, admin=False)

        account = self._account_from_create_response(self.base_url, self.site_password, data)
        self._log(f"创建邮箱: {account.email}")
        return account

    def test_connection(self) -> dict:
        """Verify tempmail connectivity. Prefer existing JWT/pool; otherwise create one mailbox."""
        if not self.base_url:
            raise RuntimeError("tempmail API 地址未配置")

        accounts = parse_tempmail_pool(self.pool, self.base_url, self.site_password)
        if not accounts and self.jwt:
            accounts = [TempMailAccount(self.base_url, self.jwt, site_password=self.site_password)]
        if accounts:
            account = self._resolve_account(accounts[0])
            return {"mode": "settings", "address": account.email, "base_url": account.base_url}

        account = self.create_address()
        return {"mode": "create_address", "address": account.email, "base_url": account.base_url}

    def settings(self, account: TempMailAccount | None = None) -> dict:
        account = account or TempMailAccount(self.base_url, self.jwt, site_password=self.site_password)
        return self._get_json(account, "/api/settings")

    def _resolve_account(self, account: TempMailAccount) -> TempMailAccount:
        if account.email:
            return account
        data = self.settings(account)
        email = str(data.get("address") or data.get("email") or "").strip()
        if not email:
            raise RuntimeError("tempmail /api/settings 未返回 address")
        return TempMailAccount(
            base_url=account.base_url,
            jwt=account.jwt,
            email=email,
            site_password=account.site_password,
        )

    def get_available_email(self) -> Optional[str]:
        account = self.reserve_account()
        return account.email if account else None

    def reserve_account(self) -> Optional[TempMailAccount]:
        accounts = parse_tempmail_pool(self.pool, self.base_url, self.site_password)
        if not accounts and self.base_url and self.jwt:
            accounts = [TempMailAccount(self.base_url, self.jwt, site_password=self.site_password)]
        for account in accounts:
            resolved = self._resolve_account(account)
            if resolved.email.lower() in self._used:
                continue
            self._log(f"选定邮箱: {resolved.email}")
            return resolved
        self._log("无可用邮箱")
        return None

    def find_account_for_email(self, email: str) -> Optional[TempMailAccount]:
        target = (email or "").strip().lower()
        if not target:
            return None
        accounts = parse_tempmail_pool(self.pool, self.base_url, self.site_password)
        if not accounts and self.base_url and self.jwt:
            accounts = [TempMailAccount(self.base_url, self.jwt, site_password=self.site_password)]
        for account in accounts:
            resolved = self._resolve_account(account)
            if resolved.email.lower() == target:
                return resolved
        return None

    def poll_code(
        self,
        email: str = "",
        keyword: str = "openai",
        timeout: int = 60,
        interval: int = 5,
        start_after: float = 0.0,
    ) -> str:
        account = self.find_account_for_email(email) if email else self.reserve_account()
        if not account:
            raise RuntimeError(f"tempmail 未找到邮箱配置: {email}")

        started = time.time()
        while time.time() - started < timeout:
            try:
                data = self._get_json(account, "/api/parsed_mails", {"limit": 20, "offset": 0})
                rows = data.get("results") or data.get("mails") or []
                for row in rows:
                    created_at = str(row.get("created_at") or "")
                    parsed_ts = _parse_created_at(created_at)
                    if start_after and parsed_ts and parsed_ts < start_after:
                        continue
                    haystack = "\n".join(
                        str(row.get(k) or "") for k in ("sender", "source", "subject", "text", "html")
                    )
                    if keyword and keyword.lower() not in haystack.lower():
                        sender_subject = " ".join(str(row.get(k) or "") for k in ("sender", "source", "subject"))
                        if "openai" not in sender_subject.lower() and "verification" not in sender_subject.lower():
                            continue
                    code = extract_verification_code(row)
                    if code:
                        self._log(f"获取到验证码: {code}")
                        return code
                self._log(f"未找到验证码, {interval}s 后重试...")
            except RuntimeError as exc:
                if "限流" in str(exc):
                    time.sleep(max(interval, 10))
                    continue
                raise
            time.sleep(interval)
        return ""


def _parse_created_at(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0

    # cloudflare_temp_email stores created_at in UTC. Treat timezone-less
    # timestamps as UTC; otherwise local timezone parsing will skip valid mails
    # when the runner is in Asia/Shanghai or another non-UTC timezone.
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            continue
    return 0.0
