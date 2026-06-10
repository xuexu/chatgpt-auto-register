#!/usr/bin/env python3
"""
iCloud Hide My Email 鈥?绾崗璁疄鐜?鍩轰簬 FlowPilot reverse engineering锛屼笉渚濊禆娴忚鍣ㄨ繍琛屻€?
鐢ㄦ硶:
    # 浠?Chrome 鑷姩鎻愬彇 cookie
    python icloud_hme.py list

    # 浣跨敤鎵嬪姩鎻愪緵鐨?cookies.json
    python icloud_hme.py list --cookies cookies.json

    # 鐢熸垚鏂板埆鍚?    python icloud_hme.py generate

    # 鍒犻櫎鎸囧畾鍒悕
    python icloud_hme.py delete --email xxx@icloud.com

    # 瀵煎嚭 Chrome cookies 鍒版枃浠讹紙鏂逛究鍚庣画澶嶇敤锛?    python icloud_hme.py export-cookies --output cookies.json

渚濊禆: pip install requests pycryptodome pywin32
"""

import sys
import os
import json
import re
import time
import sqlite3
import argparse
import hashlib
import base64
from datetime import datetime
from email import message_from_bytes
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional, Dict, List, Any, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

# ============================================================
# 甯搁噺锛堟潵鑷?FlowPilot background.js锛?# ============================================================

SETUP_URLS = [
    "https://setup.icloud.com/setup/ws/1",
    "https://setup.icloud.com.cn/setup/ws/1",
]

LOGIN_URLS = [
    "https://www.icloud.com/",
    "https://www.icloud.com.cn/",
]

CLIENT_BUILD_NUMBER = "2206Hotfix11"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_DELAYS = [1, 2.5, 5]

# iCloud 鉴权所需的 cookie 域
ICLOUD_COOKIE_DOMAINS = [
    ".icloud.com",
    ".icloud.com.cn",
    "icloud.com",
    "icloud.com.cn",
    "setup.icloud.com",
    "setup.icloud.com.cn",
    "www.icloud.com",
    "www.icloud.com.cn",
]


# ============================================================
# Cookie 鎻愬彇
# ============================================================

def _get_chrome_cookie_path() -> Optional[str]:
    """Locate the Chrome cookie database."""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_appdata, "Google", "Chrome", "User Data", "Default", "Network", "Cookies"),
        os.path.join(local_appdata, "Google", "Chrome", "User Data", "Default", "Cookies"),
    ]
    if not local_appdata:
        return None
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _get_chrome_key() -> Optional[bytes]:
    """浠?Chrome Local State 鑾峰彇鍔犲瘑瀵嗛挜 (Windows DPAPI)"""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    state_path = os.path.join(local_appdata, "Google", "Chrome", "User Data", "Local State")
    if not os.path.isfile(state_path):
        return None

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    encrypted_key = base64.b64decode(
        state.get("os_crypt", {}).get("encrypted_key", "")
    )
    if not encrypted_key or len(encrypted_key) < 6:
        return None

    # 鍘绘帀 "DPAPI" 鍓嶇紑 (5 bytes)
    encrypted_key = encrypted_key[5:]

    try:
        import win32crypt
        return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    except ImportError:
        pass

    # 鍥為€€锛氫娇鐢?ctypes 璋?crypt32.dll
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p,
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
        ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL

    blob_in = DATA_BLOB(len(encrypted_key), ctypes.c_char_p(encrypted_key))
    blob_out = DATA_BLOB()
    if crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        result = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return result
    return None


def extract_chrome_cookies() -> Dict[str, str]:
    """浠?Chrome 鎻愬彇 iCloud 鐩稿叧 cookie锛岃繑鍥?{name: value} 瀛楀吀"""
    cookie_path = _get_chrome_cookie_path()
    if not cookie_path:
        raise RuntimeError("鎵句笉鍒?Chrome Cookie 鏁版嵁搴擄紝璇峰厛鐢?Chrome 鐧诲綍 icloud.com")

    key = _get_chrome_key()
    if not key:
        raise RuntimeError("鏃犳硶鑾峰彇 Chrome 鍔犲瘑瀵嗛挜")
    from Crypto.Cipher import AES

    # 杩炴帴鏁版嵁搴?    conn = None
    try:
        # 鐩存帴杩炴帴 (Chrome WAL 妯″紡, 鍙)
        conn = sqlite3.connect(f"file:{cookie_path}?mode=ro", uri=True)
    except Exception as e:
        raise RuntimeError(f"鏃犳硶璇诲彇 Chrome Cookie 鏁版嵁搴?(璇峰叧闂瑿hrome鍚庨噸璇?: {e}")

    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        placeholders = ",".join("?" * len(ICLOUD_COOKIE_DOMAINS))
        cursor.execute(
            f"SELECT name, encrypted_value, host_key FROM cookies WHERE host_key IN ({placeholders})",
            ICLOUD_COOKIE_DOMAINS,
        )
        rows = cursor.fetchall()
    finally:
        if conn:
            conn.close()

    cookies = {}
    for row in rows:
        name = row["name"]
        encrypted = row["encrypted_value"]
        if not encrypted:
            continue

        value = _decrypt_chrome_cookie(encrypted, key)
        if value:
            cookies[name] = value

    return cookies


def _decrypt_chrome_cookie(encrypted_value: bytes, key: bytes) -> Optional[str]:
    """瑙ｅ瘑鍗曚釜 Chrome cookie (AES-256-GCM)"""
    from Crypto.Cipher import AES

    # Chrome 80+: v10 (prefix) + 12-byte nonce + ciphertext + 16-byte tag
    if len(encrypted_value) < 3:
        return None
    prefix = encrypted_value[:3]
    if prefix == b"v10" or prefix == b"v11":
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:-16]
        tag = encrypted_value[-16:]
        if len(ciphertext) < 1:
            return None
        try:
            cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
            plain = cipher.decrypt_and_verify(ciphertext, tag)
            return plain.decode("utf-8", errors="replace")
        except Exception:
            return None

    # 鏃х増 Chrome: 鐩存帴鐢?DPAPI
    if prefix == b"\x01\x00\x00\x00":
        try:
            import win32crypt
            decrypted = win32crypt.CryptUnprotectData(encrypted_value, None, None, None, 0)[1]
            return decrypted.decode("utf-8", errors="replace")
        except Exception:
            pass

    return None


# ============================================================
# iCloud Hide My Email API 瀹㈡埛绔?# ============================================================

class ICloudHME:
    """iCloud Hide My Email 绾崗璁鎴风"""

    def __init__(
        self,
        cookies: Any,
        host: str = "icloud.com",
        verbose: bool = False,
    ):
        self.cookies = self._normalize_cookies(cookies)
        self.host = self._normalize_host(host)
        self.verbose = verbose
        self.session = requests.Session()
        self.session.cookies.update(self.cookies)
        self._setup_url: Optional[str] = None
        self._service_url: Optional[str] = None
        self._preferred_host: Optional[str] = None

    @staticmethod
    def _normalize_cookies(cookies: Any) -> Dict[str, str]:
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if k and v is not None}
        if isinstance(cookies, list):
            normalized = {}
            for item in cookies:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                value = item.get("value")
                if name and value is not None:
                    normalized[name] = str(value)
            return normalized
        return {}

    @staticmethod
    def _optional_bool(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "y", "used"}:
                return True
            if normalized in {"0", "false", "no", "n", "unused", "free"}:
                return False
        return None

    @staticmethod
    def _normalize_host(host: str) -> str:
        h = host.strip().lower()
        try:
            h = urlparse(h if "://" in h else f"https://{h}").hostname or h
        except Exception:
            pass
        if h.endswith(".icloud.com.cn") or h == "icloud.com.cn":
            return "icloud.com.cn"
        return "icloud.com"

    @property
    def setup_url(self) -> str:
        if not self._setup_url:
            self._setup_url = (
                "https://setup.icloud.com.cn/setup/ws/1"
                if self.host == "icloud.com.cn"
                else "https://setup.icloud.com/setup/ws/1"
            )
        return self._setup_url

    @property
    def origin(self) -> str:
        return f"https://www.{self.host}"

    def _log(self, msg: str):
        if self.verbose:
            print(f"[iCloud] {msg}")

    def _build_url(self, url: str) -> str:
        """杩藉姞 clientBuildNumber / clientMasteringNumber 鍙傛暟"""
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        params["clientBuildNumber"] = [CLIENT_BUILD_NUMBER]
        params["clientMasteringNumber"] = [CLIENT_BUILD_NUMBER]
        new_query = urlencode(params, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _request(
        self,
        method: str,
        url: str,
        json_data: Any = None,
        content_type: Optional[str] = None,
        timeout: int = REQUEST_TIMEOUT,
        max_attempts: int = MAX_RETRIES,
    ) -> Any:
        """鍙戦€佸甫閲嶈瘯鐨?HTTP 璇锋眰"""
        full_url = self._build_url(url)
        headers = {
            "Origin": self.origin,
            "Referer": self.origin + "/",
            "Accept": "application/json, text/plain, */*",
        }
        if content_type:
            headers["Content-Type"] = content_type
        elif json_data is not None:
            # maildomainws 鐢?text/plain
            parsed = urlparse(url)
            if "maildomainws" in parsed.hostname:
                headers["Content-Type"] = "text/plain;charset=UTF-8"
            else:
                headers["Content-Type"] = "application/json"
        else:
            headers["Content-Type"] = "application/json"

        body = None
        if json_data is not None:
            body = json.dumps(json_data, ensure_ascii=False)

        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.request(
                    method=method,
                    url=full_url,
                    headers=headers,
                    data=body,
                    timeout=timeout,
                )

                if not resp.ok:
                    text = resp.text[:300]
                    last_error = RuntimeError(
                        f"{method} {url} 鈫?HTTP {resp.status_code}: {text}"
                    )
                    if resp.status_code in (401, 403):
                        raise last_error
                    if attempt < max_attempts:
                        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                        self._log(f"Retry {attempt}/{max_attempts} after {delay}s...")
                        import time
                        time.sleep(delay)
                        continue
                    raise last_error

                text = resp.text
                if not text:
                    return {}
                return resp.json()

            except requests.exceptions.Timeout:
                last_error = RuntimeError(f"{method} {url} 鈫?瓒呮椂 ({timeout}s)")
                if attempt < max_attempts:
                    delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    self._log(f"Timeout retry {attempt}/{max_attempts} after {delay}s...")
                    import time
                    time.sleep(delay)
                    continue
                raise last_error

            except requests.exceptions.ConnectionError as e:
                last_error = RuntimeError(f"{method} {url} 鈫?杩炴帴澶辫触: {e}")
                if attempt < max_attempts:
                    delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
                    self._log(f"Connection retry {attempt}/{max_attempts} after {delay}s...")
                    import time
                    time.sleep(delay)
                    continue
                raise last_error

        raise last_error or RuntimeError("鏈煡閿欒")

    # ---------- 浼氳瘽 ----------

    def validate_session(self) -> Dict:
        """鏍￠獙 iCloud 浼氳瘽锛岃繑鍥?webservices 淇℃伅"""
        self._log("姝ｅ湪鏍￠獙 iCloud 浼氳瘽...")
        data = self._request("POST", f"{self.setup_url}/validate", timeout=20)
        premium = data.get("webservices", {}).get("premiummailsettings", {})
        if not premium.get("url"):
            raise RuntimeError(
                "iCloud session validation failed: Hide My Email service not found. "
                "Confirm iCloud+ is enabled and you are signed in to icloud.com."
            )
        self._service_url = premium["url"].rstrip("/")
        self._log(f"浼氳瘽鏈夋晥 ({self.host})锛孭remium Mail: {self._service_url}")
        return data

    def _resolve_service(self):
        """纭繚宸叉牎楠屼細璇濆苟鑾峰彇鏈嶅姟 URL"""
        if not self._service_url:
            # 灏濊瘯涓や釜鍩熷悕
            errors = []
            for host in [self.host] + (
                ["icloud.com.cn"] if self.host == "icloud.com" else ["icloud.com"]
            ):
                backup = self.host
                self.host = host
                self._setup_url = None
                try:
                    return self.validate_session()
                except Exception as e:
                    errors.append(f"{host}: {e}")
                    self.host = backup
                    self._setup_url = None
            raise RuntimeError("; ".join(errors))

    # ---------- 鍒悕鎿嶄綔 ----------

    def list_aliases(self) -> List[Dict]:
        """鍒楀嚭鎵€鏈?Hide My Email 鍒悕"""
        self._resolve_service()
        self._log("姝ｅ湪鑾峰彇鍒悕鍒楄〃...")
        response = self._request("GET", f"{self._service_url}/v2/hme/list")
        aliases = self._parse_alias_list(response)
        self._log(f"Aliases loaded: {len(aliases)}")
        return aliases

    def generate(self) -> str:
        """鐢熸垚鏂扮殑鍊欓€夊埆鍚嶏紙鏈繚鐣欙級"""
        self._resolve_service()
        self._log("姝ｅ湪鐢熸垚鍊欓€夊埆鍚?..")
        response = self._request(
            "POST",
            f"{self._service_url}/v1/hme/generate",
            max_attempts=2,
        )
        if not response.get("success"):
            err = response.get("error", {})
            raise RuntimeError(f"鐢熸垚澶辫触: {err.get('errorMessage', 'unknown')}")
        hme = response.get("result", {}).get("hme", "")
        if isinstance(hme, dict):
            hme = hme.get("hme") or hme.get("email") or ""
        self._log(f"鍊欓€夊埆鍚? {hme}")
        return hme

    def reserve(self, hme: str, label: Optional[str] = None) -> str:
        """Reserve a generated alias."""
        self._resolve_service()
        if not label:
            now = datetime.now()
            label = f"MultiPage {now.strftime('%Y-%m-%d')}"
        self._log(f"姝ｅ湪淇濈暀鍒悕 {hme}...")
        data = {"hme": hme, "label": label, "note": "Generated through FlowPilot"}
        response = self._request(
            "POST",
            f"{self._service_url}/v1/hme/reserve",
            json_data=data,
            max_attempts=2,
        )
        if not response.get("success"):
            err = response.get("error", {})
            raise RuntimeError(f"淇濈暀澶辫触: {err.get('errorMessage', 'unknown')}")
        result = response.get("result", {}).get("hme", {})
        alias = result.get("hme", hme) if isinstance(result, dict) else hme
        self._log(f"宸蹭繚鐣? {alias}")
        return alias

    def create_alias(self, label: Optional[str] = None, max_retries: int = 5) -> str:
        """Generate and reserve a new alias."""
        for attempt in range(max_retries):
            if attempt > 0:
                # 鍒锋柊鏈嶅姟鑺傜偣閲嶆柊鑾峰彇
                self._service_url = None
                self._setup_url = None
            hme = self.generate()
            try:
                return self.reserve(hme, label)
            except Exception as e:
                self._log(f"reserve 澶辫触 (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    continue
        raise RuntimeError(f"reserve 閲嶈瘯 {max_retries} 娆″潎澶辫触")

    def reuse_or_create_alias(self, label: Optional[str] = None) -> str:
        """Reuse an active alias when possible, otherwise create one."""
        aliases = self.list_aliases()

        reusable = next(
            (a for a in aliases if a.get("active") and a.get("used") is False),
            None,
        )
        if reusable is None:
            reusable = next(
                (a for a in aliases if a.get("active") and a.get("used") is None),
                None,
            )

        if reusable is not None:
            alias = reusable["email"]
            self._log(f"澶嶇敤鍒悕: {alias}")
            return alias

        alias = self.create_alias(label=label)
        self._log(f"鏃犲彲澶嶇敤鍒悕锛屽凡鍒涘缓: {alias}")
        return alias

    def deactivate(self, anonymous_id: str) -> bool:
        """鍋滅敤鍒悕"""
        self._resolve_service()
        self._log(f"姝ｅ湪鍋滅敤 {anonymous_id}...")
        response = self._request(
            "POST",
            f"{self._service_url}/v1/hme/deactivate",
            json_data={"anonymousId": anonymous_id},
            max_attempts=2,
        )
        ok = response.get("success", False)
        self._log("Alias deactivated" if ok else f"Deactivate failed: {response.get('error', {})}")
        return ok

    def delete(self, anonymous_id: str) -> bool:
        """鍒犻櫎鍒悕锛堝け璐ユ椂浼氬皾璇曞厛鍋滅敤鍐嶅垹闄わ級"""
        self._resolve_service()
        self._log(f"姝ｅ湪鍒犻櫎 {anonymous_id}...")
        try:
            response = self._request(
                "POST",
                f"{self._service_url}/v1/hme/delete",
                json_data={"anonymousId": anonymous_id},
                max_attempts=2,
            )
            if response.get("success") is False:
                raise RuntimeError(response.get("error", {}).get("errorMessage", "delete failed"))
        except Exception as e:
            self._log(f"鐩存帴鍒犻櫎澶辫触: {e}锛屽皾璇曞厛鍋滅敤鍐嶅垹闄?..")
            self.deactivate(anonymous_id)
            response = self._request(
                "POST",
                f"{self._service_url}/v1/hme/delete",
                json_data={"anonymousId": anonymous_id},
                max_attempts=2,
            )
            if response.get("success") is False:
                raise RuntimeError(response.get("error", {}).get("errorMessage", "delete failed"))
        self._log("Alias deleted")
        return True

    # ---------- 瑙ｆ瀽 ----------

    @staticmethod
    def _parse_alias_list(response: Any) -> List[Dict]:
        """Parse aliases from the API response."""
        aliases_raw = None

        # 浼樺厛: result.hmeEmails (鏂扮増 icloud API)
        if isinstance(response, dict):
            result = response.get("result", {})
            if isinstance(result, dict):
                hme = result.get("hmeEmails")
                if isinstance(hme, list):
                    aliases_raw = hme

        # 鍥為€€: 娣卞害閬嶅巻鎵剧涓€涓?dict 鍏冪礌缁勬垚鐨勬暟缁?        if not aliases_raw:
            def _find_dict_array(d, depth=0):
                if depth > 4 or d is None:
                    return None
                if isinstance(d, list) and len(d) > 0 and isinstance(d[0], dict):
                    return d
                if isinstance(d, dict):
                    for v in d.values():
                        r = _find_dict_array(v, depth + 1)
                        if r:
                            return r
                return None
            aliases_raw = _find_dict_array(response)

        if not aliases_raw:
            return []

        aliases = []
        for item in aliases_raw:
            if not isinstance(item, dict):
                continue
            email = str(
                item.get("hme")
                or item.get("email")
                or item.get("alias")
                or item.get("address")
                or item.get("metaData", {}).get("hme")
                or ""
            ).strip().lower()
            if not email or "@" not in email:
                continue

            state = str(item.get("state") or item.get("status") or "").strip().lower()
            aliases.append({
                "email": email,
                "anonymousId": str(item.get("anonymousId") or item.get("id") or ""),
                "label": str(item.get("label") or item.get("metaData", {}).get("label") or ""),
                "note": str(item.get("note") or item.get("metaData", {}).get("note") or ""),
                "used": ICloudHME._optional_bool(
                    item.get("used", item.get("isUsed", item.get("metaData", {}).get("used")))
                ),
                "active": item.get("active", True) and item.get("isActive", True) and state not in ("inactive", "deleted"),
                "state": state,
                "createdAt": item.get("createTimestamp") or item.get("createdAt") or None,
            })

        # 鎺掑簭锛歛ctive 浼樺厛锛屾寜 email 瀛楀吀搴?        aliases.sort(key=lambda a: (not a["active"], a["email"]))
        return aliases

    # ---------- 閭欢杞 (maildomainws API) ----------

    def poll_mail_for_code(
        self,
        target_email: str,
        sender_filters: Optional[List[str]] = None,
        timeout: int = 120,
        interval: int = 5,
        exclude_codes: Optional[List[str]] = None,
        imap_user: str = "",
        imap_password: str = "",
        start_after: Optional[float] = None,
    ) -> Optional[str]:
        """
        杞 iCloud 閭鎵鹃獙璇佺爜 (IMAP)

        Args:
            target_email: 鐩爣鏀朵欢閭 (鏄剧ず鐢?
            sender_filters: 鍙戜欢浜鸿繃婊?            timeout: 鎬昏秴鏃剁鏁?            interval: 杞闂撮殧
            exclude_codes: 鎺掗櫎鐨勯獙璇佺爜
            imap_user: iCloud 鐧诲綍閭 (濡?alias@icloud.com)
            imap_password: app-specific password
        """
        if imap_user and imap_password:
            return self._poll_mail_imap(
                target_email, sender_filters, timeout, interval, exclude_codes,
                imap_user, imap_password,
                start_after=start_after,
            )
        return self._poll_mail_api(
            target_email, sender_filters, timeout, interval, exclude_codes,
            start_after=start_after,
        )

    def _poll_mail_imap(
        self, target_email, sender_filters, timeout, interval, exclude_codes,
        imap_user, imap_password,
    ) -> Optional[str]:
        """IMAP 杞 iCloud 閭 鈥?宸查獙璇侀€氳繃"""
        import imaplib, quopri
        from html.parser import HTMLParser

        class _StripHTML(HTMLParser):
            def __init__(self): super().__init__(); self.text = ""
            def handle_data(self, d): self.text += d

        excluded = set(exclude_codes or [])
        filters = [f.lower() for f in (sender_filters or ["openai", "noreply", "verification"])]

        self._log(f"IMAP {imap_user} 寮€濮嬭疆璇?...")
        start = time.time()
        last_count = -1  # -1 琛ㄧず绗竴杞紝鍙褰曞熀鍑嗕笉鏌ラ偖浠?
        while time.time() - start < timeout:
            try:
                mail = imaplib.IMAP4_SSL("imap.mail.me.com", 993)
                mail.login(imap_user, imap_password)
                mail.select("INBOX")

                status, data = mail.search(None, "ALL")
                if status != "OK":
                    mail.logout()
                    time.sleep(interval)
                    continue

                msg_ids = data[0].split()
                current_count = len(msg_ids)

                # 绗竴杞? 鍙鍩哄噯鏁帮紝涓嶆煡閭欢
                if last_count == -1:
                    last_count = current_count
                    self._log(f"IMAP baseline: {current_count} existing messages")
                    mail.logout()
                    time.sleep(interval)
                    continue

                # 鍙鏌ユ柊閭欢
                if current_count > last_count:
                    new_ids = msg_ids[last_count:]
                    last_count = current_count
                    self._log(f"IMAP 鍙戠幇 {len(new_ids)} 灏佹柊閭欢")

                    for mid in reversed(new_ids):
                        status, msg_data = mail.fetch(mid, "(BODY[TEXT])")
                        if status != "OK":
                            continue

                        raw = b""
                        for item in msg_data:
                            if isinstance(item, (bytes, bytearray)):
                                raw = bytes(item)
                                break
                            if isinstance(item, tuple) and len(item) > 1:
                                raw = item[1] if isinstance(item[1], bytes) else raw
                                break

                        # 瑙ｇ爜 quoted-printable
                        try:
                            text = quopri.decodestring(raw).decode("utf-8", errors="ignore")
                        except Exception:
                            text = raw.decode("utf-8", errors="ignore")

                        # 杩囨护鍙戜欢浜?涓婚鍏抽敭瀛?                        lower = text.lower()
                        if not any(f in lower for f in filters):
                            continue

                        # 鍓?HTML 鎻愬彇楠岃瘉鐮?                        parser = _StripHTML()
                        parser.feed(text)
                        plain = parser.text

                        codes = re.findall(r"\b(\d{6})\b", plain)
                        for code in codes:
                            if code not in excluded:
                                self._log(f"IMAP 鎵惧埌楠岃瘉鐮? {code}")
                                mail.logout()
                                return code

                mail.logout()
                time.sleep(interval)

            except Exception as e:
                self._log(f"IMAP 寮傚父: {e}")
                time.sleep(interval)

        self._log(f"IMAP {timeout}s 瓒呮椂")
        return None

    def _poll_mail_api(
        self, target_email: str, sender_filters: list, timeout: int,
        interval: int, exclude_codes: set,
    ) -> Optional[str]:
        excluded = set(exclude_codes or [])
        filters = [f.lower() for f in (sender_filters or [])]
        if not filters:
            filters = ["openai", "chatgpt", "noreply", "no-reply", "verification"]

        self._log(f"寮€濮嬭疆璇?iCloud 閭锛堝彂浠朵汉杩囨护: {filters}, 瓒呮椂 {timeout}s锛?..")
        start = time.time()
        seen_ids = set()

        while time.time() - start < timeout:
            try:
                # 鐢?maildomainws API 鑾峰彇閭欢鍒楄〃
                messages = self._fetch_mail_messages()
                if not messages:
                    self._log(f"鏆傛棤鏂伴偖浠讹紝{interval}s 鍚庨噸璇?..")
                    time.sleep(interval)
                    continue

                for msg in messages:
                    msg_id = str(msg.get("guid", ""))
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    sender = str(msg.get("from", "") or msg.get("sender", "")).lower()
                    subject = str(msg.get("subject", "")).lower()

                    # 妫€鏌ュ彂浠朵汉/涓婚鏄惁鍖归厤
                    match = any(f in sender or f in subject for f in filters)
                    if not match:
                        continue

                    self._log(f"鍖归厤閭欢: {subject[:60]} (from: {sender[:40]})")

                    # 鑾峰彇閭欢姝ｆ枃
                    body = self._fetch_mail_body(msg_id)
                    if not body:
                        continue

                    # 鎻愬彇楠岃瘉鐮?                    code = self._extract_code_from_text(body, excluded)
                    if code:
                        self._log(f"宸叉壘鍒伴獙璇佺爜: {code}")
                        return code

            except Exception as e:
                self._log(f"杞寮傚父: {e}")

            time.sleep(interval)

        self._log(f"No verification code within {timeout}s")
        return None

    def _poll_mail_imap(
        self, target_email, sender_filters, timeout, interval, exclude_codes,
        imap_user, imap_password, start_after: Optional[float] = None,
    ) -> Optional[str]:
        import imaplib, quopri
        from html.parser import HTMLParser

        class _StripHTML(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = ""

            def handle_data(self, data):
                self.text += data

        excluded = set(exclude_codes or [])
        filters = [f.lower() for f in (sender_filters or ["openai", "noreply", "verification"])]
        normalized_target = self._normalize_target_email(target_email)

        self._log(f"IMAP {imap_user} 瀵偓婵鐤嗙拠?...")
        started = time.time()
        last_count = -1
        while time.time() - started < timeout:
            try:
                mail = imaplib.IMAP4_SSL("imap.mail.me.com", 993)
                mail.login(imap_user, imap_password)
                mail.select("INBOX")

                status, data = mail.search(None, "ALL")
                if status != "OK":
                    mail.logout()
                    time.sleep(interval)
                    continue

                msg_ids = data[0].split()
                current_count = len(msg_ids)
                if last_count == -1 and start_after is None:
                    last_count = current_count
                    self._log(f"IMAP baseline: {current_count} existing messages")
                    mail.logout()
                    time.sleep(interval)
                    continue
                if start_after is None and current_count <= last_count:
                    mail.logout()
                    time.sleep(interval)
                    continue

                candidate_ids = msg_ids if start_after is not None else msg_ids[last_count:]
                last_count = current_count
                if start_after is None:
                    self._log(f"IMAP new messages: {len(candidate_ids)}")

                for mid in reversed(candidate_ids):
                    status, msg_data = mail.fetch(mid, "(BODY.PEEK[])")
                    if status != "OK":
                        continue

                    raw = b""
                    for item in msg_data:
                        if isinstance(item, (bytes, bytearray)):
                            raw = bytes(item)
                            break
                        if isinstance(item, tuple) and len(item) > 1:
                            raw = item[1] if isinstance(item[1], bytes) else raw
                            break
                    if not raw:
                        continue

                    try:
                        text = quopri.decodestring(raw).decode("utf-8", errors="ignore")
                    except Exception:
                        text = raw.decode("utf-8", errors="ignore")

                    try:
                        message = message_from_bytes(raw)
                    except Exception:
                        message = None

                    if start_after is not None and message is not None:
                        msg_ts = self._coerce_timestamp(message.get("Date"))
                        if msg_ts is not None and msg_ts < start_after:
                            continue

                    sender = str(message.get("From") or "") if message is not None else ""
                    subject = str(message.get("Subject") or "") if message is not None else ""
                    header_blob = ""
                    if message is not None:
                        header_blob = " ".join(
                            str(message.get(name) or "")
                            for name in ("To", "Delivered-To", "X-Original-To", "Cc", "Bcc")
                        )

                    lower = f"{sender} {subject} {text}".lower()
                    if not any(f in lower for f in filters):
                        continue

                    parser = _StripHTML()
                    parser.feed(text)
                    plain = parser.text

                    if normalized_target and normalized_target not in (
                        f"{header_blob}\n{text}\n{plain}".lower()
                    ):
                        continue

                    for code in re.findall(r"\b(\d{6})\b", plain):
                        if code not in excluded:
                            self._log(f"IMAP code found: {code}")
                            mail.logout()
                            return code

                mail.logout()
                time.sleep(interval)

            except Exception as exc:
                self._log(f"IMAP poll error: {exc}")
                time.sleep(interval)

        self._log(f"IMAP timeout after {timeout}s")
        return None

    def _poll_mail_api(
        self, target_email: str, sender_filters: list, timeout: int,
        interval: int, exclude_codes: set, start_after: Optional[float] = None,
    ) -> Optional[str]:
        excluded = set(exclude_codes or [])
        filters = [f.lower() for f in (sender_filters or [])]
        normalized_target = self._normalize_target_email(target_email)
        if not filters:
            filters = ["openai", "chatgpt", "noreply", "no-reply", "verification"]

        self._log(f"Start polling iCloud mail filters={filters} timeout={timeout}s")
        started = time.time()
        seen_ids = set()
        baseline_ready = False

        while time.time() - started < timeout:
            try:
                messages = self._fetch_mail_messages()
                if not messages:
                    self._log(f"No new messages, retry after {interval}s")
                    time.sleep(interval)
                    continue

                if start_after is None and not baseline_ready:
                    seen_ids.update(str(msg.get("guid", "")) for msg in messages if msg.get("guid"))
                    baseline_ready = True
                    self._log(f"API baseline: {len(seen_ids)} existing messages")
                    time.sleep(interval)
                    continue

                baseline_ready = True
                for msg in messages:
                    msg_id = str(msg.get("guid", ""))
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)

                    msg_ts = self._message_timestamp(msg)
                    if start_after is not None and msg_ts is not None and msg_ts < start_after:
                        continue

                    sender = str(msg.get("from", "") or msg.get("sender", "")).lower()
                    subject = str(msg.get("subject", "")).lower()
                    if not any(f in sender or f in subject for f in filters):
                        continue

                    self._log(f"Matched mail: {subject[:60]} (from: {sender[:40]})")
                    body = self._fetch_mail_body(msg_id)
                    if not body:
                        continue
                    if normalized_target and not self._message_matches_target_email(
                        msg, body, normalized_target
                    ):
                        continue

                    code = self._extract_code_from_text(body, excluded)
                    if code:
                        self._log(f"Code found: {code}")
                        return code

            except Exception as exc:
                self._log(f"Poll error: {exc}")

            time.sleep(interval)

        self._log(f"No verification code within {timeout}s")
        return None

    def _fetch_mail_messages(self, limit: int = 20) -> List[Dict]:
        """Fetch recent iCloud Mail messages."""
        # maildomainws 绔偣
        mail_url = f"{self._service_url}/maildomainws"
        try:
            response = self._request(
                "GET",
                f"{mail_url}/messages?folder=INBOX&limit={limit}",
                timeout=20,
            )
            return response.get("messages", []) if isinstance(response, dict) else []
        except Exception:
            # 鍥為€€鍒?webmail API
            try:
                response = self._request(
                    "GET",
                    f"https://www.{self.host}/mail/",
                    timeout=20,
                )
                return []
            except Exception:
                return []

    def _fetch_mail_body(self, msg_id: str) -> str:
        """鑾峰彇閭欢姝ｆ枃"""
        mail_url = f"{self._service_url}/maildomainws"
        try:
            response = self._request(
                "GET",
                f"{mail_url}/messages/{msg_id}",
                timeout=20,
            )
            if isinstance(response, dict):
                return str(response.get("body", "") or response.get("textBody", "") or "")
            return ""
        except Exception:
            return ""

    @staticmethod
    def _normalize_target_email(target_email: str) -> str:
        return str(target_email or "").strip().lower()

    @staticmethod
    def _coerce_timestamp(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        text = str(value).strip()
        if not text:
            return None
        try:
            numeric = float(text)
            return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            return parsedate_to_datetime(text).timestamp()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None

    @classmethod
    def _message_timestamp(cls, msg: Dict[str, Any]) -> Optional[float]:
        for key in (
            "dateReceived",
            "receivedDate",
            "receivedDateTime",
            "date",
            "sentDate",
            "createdAt",
            "timestamp",
            "time",
        ):
            ts = cls._coerce_timestamp(msg.get(key))
            if ts is not None:
                return ts
        return None

    @classmethod
    def _collect_message_emails(cls, value: Any) -> set[str]:
        found = set()
        if value is None:
            return found
        if isinstance(value, dict):
            for nested in value.values():
                found.update(cls._collect_message_emails(nested))
            return found
        if isinstance(value, (list, tuple, set)):
            for nested in value:
                found.update(cls._collect_message_emails(nested))
            return found
        text = str(value).strip()
        if not text:
            return found
        for _label, email in getaddresses([text]):
            normalized = email.strip().lower()
            if normalized and "@" in normalized:
                found.add(normalized)
        return found

    @classmethod
    def _message_matches_target_email(
        cls,
        msg: Dict[str, Any],
        body: str,
        normalized_target: str,
    ) -> bool:
        if not normalized_target:
            return True
        if normalized_target in cls._collect_message_emails(msg):
            return True
        body_lower = str(body or "").lower()
        if normalized_target in body_lower:
            return True
        return normalized_target in str(msg).lower()

    @staticmethod
    def _extract_code_from_text(text: str, excluded: set) -> Optional[str]:
        text = str(text or "")
        patterns = (
            r"(?:verification\s+code|login\s+code|log-?in\s+code|enter\s+this\s+code)[^0-9]{0,24}(\d{6})",
            r"(?:验证码|驗證碼|認証コード|確認コード)[^0-9]{0,24}(\d{6})",
            r"code[:\s]+is[:\s]+(\d{6})",
            r"code[:\s]+(\d{6})",
        )
        for pattern in patterns:
            m = re.search(pattern, text, re.I)
            if not m:
                continue
            code = next((group for group in m.groups() if group), "")
            if code and code not in excluded:
                return code

        for code in re.findall(r"\b(\d{6})\b", text):
            if code not in excluded:
                return code
        return None


# ============================================================
# CLI
# ============================================================

def _load_cookies(args) -> Dict[str, str]:
    """鏍规嵁鍛戒护琛屽弬鏁板姞杞?cookies"""
    if args.cookies:
        with open(args.cookies, "r", encoding="utf-8") as f:
            return json.load(f)
    # 鑷姩浠?Chrome 鎻愬彇
    print("[*] 姝ｅ湪浠?Chrome 鎻愬彇 iCloud cookies...")
    cookies = extract_chrome_cookies()
    if not cookies:
        raise RuntimeError("鏈彁鍙栧埌 iCloud cookies锛岃鍏堝湪 Chrome 鐧诲綍 icloud.com")
    print(f"[+] 宸叉彁鍙?{len(cookies)} 涓?cookie")
    return cookies


def _validate_cookies(cookies: Dict[str, str]):
    """妫€鏌ユ槸鍚﹀寘鍚繀瑕?cookie"""
    key_names = [k.lower() for k in cookies.keys()]
    has_web_auth = any("webauth" in k for k in key_names)
    has_session = any(k in key_names for k in ("dssid2", "dssid", "session"))
    if not has_web_auth and not has_session:
        print("[!] 璀﹀憡锛氭湭妫€娴嬪埌鍏稿瀷鐨?iCloud 閴存潈 cookie (X-APPLE-WEBAUTH-* 鎴?session cookie)")
        print("[!] 濡傛灉鍚庣画璇锋眰澶辫触锛岃纭宸插湪 Chrome 鐧诲綍 https://www.icloud.com")


def cmd_list(args):
    cookies = _load_cookies(args)
    _validate_cookies(cookies)
    client = ICloudHME(cookies, host=args.host, verbose=args.verbose)
    aliases = client.list_aliases()
    print(f"\n鍏?{len(aliases)} 涓?Hide My Email 鍒悕:\n")
    for a in aliases:
        status = "[ACTIVE]" if a["active"] else "[INACTIVE]"
        print(f"  {status} {a['email']}")
        if a["label"]:
            print(f"          label: {a['label']}")
        if a["anonymousId"]:
            print(f"          id: {a['anonymousId']}")
        if a["createdAt"]:
            print(f"          created: {a['createdAt']}")
        print()


def cmd_generate(args):
    cookies = _load_cookies(args)
    _validate_cookies(cookies)
    client = ICloudHME(cookies, host=args.host, verbose=args.verbose)
    alias = client.create_alias(args.label)
    print(f"\n[+] 鏂板埆鍚嶅凡鍒涘缓: {alias}")


def cmd_delete(args):
    cookies = _load_cookies(args)
    _validate_cookies(cookies)
    client = ICloudHME(cookies, host=args.host, verbose=args.verbose)

    if args.email:
        # 鍏堝垪鍑烘壘鍒?anonymousId
        aliases = client.list_aliases()
        target = args.email.strip().lower()
        found = next((a for a in aliases if a["email"] == target), None)
        if not found:
            print(f"[!] 鏈壘鍒板埆鍚? {target}")
            sys.exit(1)
        anonymous_id = found["anonymousId"]
        if not anonymous_id:
            print(f"[!] {target} missing anonymousId, cannot delete")
            sys.exit(1)
        client.delete(anonymous_id)
        print(f"[+] 宸插垹闄? {target}")
    elif args.id:
        client.delete(args.id)
        print(f"[+] 宸插垹闄? {args.id}")
    else:
        print("[!] 璇锋寚瀹?--email 鎴?--id")
        sys.exit(1)


def cmd_export_cookies(args):
    cookies = extract_chrome_cookies()
    output = args.output or "icloud_cookies.json"
    with open(output, "w", encoding="utf-8") as f:
        json.dump(cookies, f, indent=2, ensure_ascii=False)
    print(f"[+] 宸插鍑?{len(cookies)} 涓?cookie 鍒?{output}")


def main():
    parser = argparse.ArgumentParser(
        description="iCloud Hide My Email tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="鍒楀嚭鎵€鏈?Hide My Email 鍒悕")
    p_list.add_argument("--cookies", help="cookies.json 鏂囦欢璺緞")
    p_list.add_argument("--host", default="icloud.com", choices=["icloud.com", "icloud.com.cn"])
    p_list.add_argument("--verbose", "-v", action="store_true")

    # generate
    p_gen = sub.add_parser("generate", help="鍒涘缓鏂扮殑 Hide My Email 鍒悕")
    p_gen.add_argument("--cookies", help="cookies.json 鏂囦欢璺緞")
    p_gen.add_argument("--host", default="icloud.com", choices=["icloud.com", "icloud.com.cn"])
    p_gen.add_argument("--label", help="Alias label")
    p_gen.add_argument("--verbose", "-v", action="store_true")

    # delete
    p_del = sub.add_parser("delete", help="鍒犻櫎 Hide My Email 鍒悕")
    p_del.add_argument("--cookies", help="cookies.json 鏂囦欢璺緞")
    p_del.add_argument("--host", default="icloud.com", choices=["icloud.com", "icloud.com.cn"])
    p_del.add_argument("--email", help="瑕佸垹闄ょ殑鍒悕閭鍦板潃")
    p_del.add_argument("--id", help="瑕佸垹闄ょ殑鍒悕鐨?anonymousId")
    p_del.add_argument("--verbose", "-v", action="store_true")

    # export-cookies
    p_exp = sub.add_parser("export-cookies", help="Export cookies to file")
    p_exp.add_argument("--output", "-o", default="icloud_cookies.json")

    args = parser.parse_args()

    try:
        if args.command == "list":
            cmd_list(args)
        elif args.command == "generate":
            cmd_generate(args)
        elif args.command == "delete":
            cmd_delete(args)
        elif args.command == "export-cookies":
            cmd_export_cookies(args)
    except RuntimeError as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] Interrupted")
        sys.exit(1)


if __name__ == "__main__":
    main()
