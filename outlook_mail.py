#!/usr/bin/env python3
"""Outlook mailbox pool and verification-code polling."""

import base64
import imaplib
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, List, Optional, Set

import requests

try:
    from curl_cffi import requests as curl_requests
except Exception:  # pragma: no cover - optional TLS fallback
    curl_requests = None


TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
GRAPH_SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@(outlook|hotmail|live|msn)\.[A-Z0-9.-]+", re.I)
_RESERVE_LOCK = threading.Lock()


@dataclass
class OutlookAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str
    raw: str = ""


class _StripHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ""

    def handle_data(self, data):
        self.text += data


def _repo_path(path: str) -> Path:
    p = Path(path or "")
    if p.is_absolute():
        return p
    return Path(__file__).parent / p


def _is_inline_pool_text(value: str) -> bool:
    text = (value or "").strip()
    return "----" in text and bool(EMAIL_RE.search(text))


def _pool_source_label(value: str) -> str:
    if _is_inline_pool_text(value):
        return "inline outlook pool"
    return str(_repo_path(value or "outlook.txt"))


def _decode_header(value: str) -> str:
    parts = []
    for data, charset in decode_header(value or ""):
        if isinstance(data, bytes):
            parts.append(data.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(data)
    return "".join(parts)


def _strip_html(text: str) -> str:
    parser = _StripHTML()
    parser.feed(text or "")
    return parser.text


def _extract_code(text: str, excluded: Set[str]) -> Optional[str]:
    text = text or ""
    patterns = [
        r"(?:log-?in\s+code|enter\s+this\s+code|verification\s+code)[^0-9]{0,32}(\d{6})",
        r"(?:code|验证码|代碼)[^0-9]{0,24}(\d{6})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match and match.group(1) not in excluded:
            return match.group(1)
    for code in re.findall(r"\b(\d{6})\b", text):
        if code not in excluded:
            return code
    return None


def _used_emails(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    used = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = EMAIL_RE.search(line)
        if match:
            used.add(match.group(0).lower())
    return used


def load_outlook_accounts(path: str = "outlook.txt") -> List[OutlookAccount]:
    source = (path or "outlook.txt").strip() or "outlook.txt"
    source_label = _pool_source_label(source)
    if _is_inline_pool_text(source):
        lines = source.splitlines()
    else:
        pool = _repo_path(source)
        if not pool.exists():
            raise FileNotFoundError(f"Outlook pool not found: {pool}")
        lines = pool.read_text(encoding="utf-8", errors="ignore").splitlines()

    accounts = []
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 3)
        if len(parts) != 4:
            raise ValueError(f"Invalid outlook pool format at line {lineno}")
        email, password, client_id, refresh_token = [p.strip() for p in parts]
        if not EMAIL_RE.fullmatch(email):
            raise ValueError(f"Invalid Outlook email at line {lineno}: {email}")
        accounts.append(OutlookAccount(email, password, client_id, refresh_token, raw=line))
    if not accounts:
        raise RuntimeError(f"No Outlook accounts found in {source_label}")
    return accounts


def reserve_next_outlook(
    pool_path: str = "outlook.txt",
    used_path: str = "outlook_used.txt",
) -> OutlookAccount:
    with _RESERVE_LOCK:
        used_file = _repo_path(used_path)
        used = _used_emails(used_file)

        for account in load_outlook_accounts(pool_path):
            if account.email.lower() in used:
                continue
            used_file.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with used_file.open("a", encoding="utf-8") as f:
                f.write(f"{ts}\t{account.email}\treserved\n")
            return account

        raise RuntimeError(f"No unused Outlook accounts left in {_pool_source_label(pool_path)}")


def get_outlook_account(email: str, pool_path: str = "outlook.txt") -> OutlookAccount:
    target = (email or "").strip().lower()
    if not target:
        raise ValueError("Outlook email is required")
    for account in load_outlook_accounts(pool_path):
        if account.email.lower() == target:
            return account
    raise RuntimeError(f"Outlook account not found in pool: {email}")


def mark_outlook_status(
    email: str,
    status: str,
    used_path: str = "outlook_used.txt",
) -> None:
    used_file = _repo_path(used_path)
    used_file.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with used_file.open("a", encoding="utf-8") as f:
        f.write(f"{ts}\t{email}\t{status}\n")


def _mask_email(email: str) -> str:
    local, sep, domain = (email or "").partition("@")
    if not sep:
        return "***"
    if len(local) <= 2:
        local = local[:1] + "***"
    else:
        local = local[:2] + "***" + local[-1:]
    return f"{local}@{domain}"


class OutlookMailClient:
    def __init__(
        self,
        account: OutlookAccount,
        verbose: bool = False,
        proxy: str = "",
        prefer_imap: bool = True,
    ):
        self.account = account
        self.verbose = verbose
        self.proxy = (proxy or "").strip()
        self._prefer_imap = bool(prefer_imap)
        self._graph_token = ""
        self._imap_token = ""
        self._graph_failed = False

    def _log(self, msg: str):
        if self.verbose:
            print(f"[Outlook] {msg}")

    @staticmethod
    def _is_recent_enough(message_ts: Optional[float], start_after: float, grace_seconds: float = 1.0) -> bool:
        if not message_ts:
            return True
        return message_ts >= (float(start_after) - grace_seconds)

    def _proxies(self):
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _post(self, url: str, **kwargs):
        proxies = self._proxies()
        if proxies:
            kwargs["proxies"] = proxies
        if curl_requests is not None:
            try:
                return curl_requests.post(url, impersonate="chrome", **kwargs)
            except Exception:
                if not proxies:
                    raise
        return requests.post(url, **kwargs)

    def _get(self, url: str, **kwargs):
        proxies = self._proxies()
        if proxies:
            kwargs["proxies"] = proxies
        if curl_requests is not None:
            try:
                return curl_requests.get(url, impersonate="chrome", **kwargs)
            except Exception:
                if not proxies:
                    raise
        return requests.get(url, **kwargs)

    def _access_token(self, scope: str) -> str:
        resp = self._post(
            TOKEN_URL,
            data={
                "client_id": self.account.client_id,
                "refresh_token": self.account.refresh_token,
                "grant_type": "refresh_token",
                "scope": scope,
            },
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"token refresh HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        token = data.get("access_token", "")
        if not token:
            raise RuntimeError(f"token refresh failed: {data}")
        return token

    def _get_graph_token(self) -> str:
        if not self._graph_token:
            self._graph_token = self._access_token(GRAPH_SCOPE)
        return self._graph_token

    def _get_imap_token(self) -> str:
        if not self._imap_token:
            self._imap_token = self._access_token(IMAP_SCOPE)
        return self._imap_token

    def poll_code(
        self,
        sender_filters: Optional[Iterable[str]] = None,
        timeout: int = 120,
        interval: int = 5,
        exclude_codes: Optional[Iterable[str]] = None,
        start_after: Optional[float] = None,
    ) -> Optional[str]:
        filters = [f.lower() for f in (sender_filters or ["openai", "noreply", "no-reply", "verification"])]
        excluded = set(exclude_codes or [])
        if start_after is None:
            start_after = time.time() - 15
        else:
            start_after = float(start_after)
        start = time.time()

        self._log(f"polling {_mask_email(self.account.email)}, timeout={timeout}s")
        while time.time() - start < timeout:
            code = None
            pollers = [
                ("imap", lambda: self._poll_imap_once(filters, excluded, start_after)),
                ("graph", lambda: self._poll_graph_once(filters, excluded, start_after)),
            ]
            if not self._prefer_imap:
                pollers.reverse()

            for name, poller in pollers:
                if name == "graph" and self._graph_failed:
                    continue
                try:
                    code = poller()
                except Exception as e:
                    if name == "graph":
                        self._graph_failed = True
                        self._log(f"Graph disabled: {e}")
                    else:
                        self._log(f"IMAP poll error: {e}")
                    continue
                if code:
                    return code
            time.sleep(interval)
        return None

    def list_recent_messages(
        self,
        limit: int = 20,
        sender_filter: str = "",
        include_body: bool = True,
    ) -> List[dict]:
        """Return recent inbox messages without exposing mailbox credentials."""
        limit = max(1, min(50, int(limit or 20)))
        sender_filter = (sender_filter or "").strip().lower()
        listers = [
            ("imap", lambda: self._list_imap_messages(limit, sender_filter, include_body)),
            ("graph", lambda: self._list_graph_messages(limit, sender_filter, include_body)),
        ]
        if not self._prefer_imap:
            listers.reverse()

        last_error = None
        for name, lister in listers:
            if name == "graph" and self._graph_failed:
                continue
            try:
                items = lister()
            except Exception as e:
                last_error = e
                if name == "graph":
                    self._graph_failed = True
                    self._log(f"Graph list disabled: {e}")
                else:
                    self._log(f"IMAP list error: {e}")
                continue
            if items or name == listers[-1][0]:
                return items
        if last_error:
            raise last_error
        return []

    def _poll_graph_once(self, filters: List[str], excluded: Set[str], start_after: float) -> Optional[str]:
        try:
            token = self._get_graph_token()
            resp = self._get(
                GRAPH_MESSAGES_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Prefer": 'outlook.body-content-type="text"',
                },
                params={
                    "$top": "20",
                    "$orderby": "receivedDateTime desc",
                    "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
                },
                timeout=30,
            )
            if not resp.ok:
                self._graph_failed = True
                raise RuntimeError(f"Graph HTTP {resp.status_code}: {resp.text[:200]}")
            for msg in resp.json().get("value", []):
                received = self._parse_graph_time(msg.get("receivedDateTime", ""))
                if not self._is_recent_enough(received, start_after):
                    continue
                sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "")
                subject = msg.get("subject", "") or ""
                preview = msg.get("bodyPreview", "") or ""
                body = ((msg.get("body") or {}).get("content") or "")
                haystack = f"{sender}\n{subject}\n{preview}\n{body}".lower()
                if not any(f in haystack for f in filters):
                    continue
                code = _extract_code(_strip_html(f"{subject}\n{preview}\n{body}"), excluded)
                if code:
                    self._log("code found via Graph")
                    return code
            return None
        except Exception:
            self._graph_failed = True
            raise

    def _list_graph_messages(self, limit: int, sender_filter: str, include_body: bool) -> List[dict]:
        token = self._get_graph_token()
        resp = self._get(
            GRAPH_MESSAGES_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Prefer": 'outlook.body-content-type="text"',
            },
            params={
                "$top": str(min(50, max(limit * 2, limit))),
                "$orderby": "receivedDateTime desc",
                "$select": "id,subject,from,receivedDateTime,bodyPreview,body",
            },
            timeout=30,
        )
        if not resp.ok:
            self._graph_failed = True
            raise RuntimeError(f"Graph HTTP {resp.status_code}: {resp.text[:200]}")
        rows = []
        for msg in resp.json().get("value", []):
            sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address", "") or ""
            subject = msg.get("subject", "") or ""
            preview = msg.get("bodyPreview", "") or ""
            body = _strip_html(((msg.get("body") or {}).get("content") or ""))
            haystack = f"{sender}\n{subject}\n{preview}\n{body}".lower()
            if sender_filter and sender_filter not in haystack:
                continue
            text = body or preview
            rows.append({
                "id": msg.get("id", ""),
                "from": sender,
                "subject": subject,
                "date": msg.get("receivedDateTime", ""),
                "preview": self._clip(preview or text, 260),
                "body": self._clip(text, 4000) if include_body else "",
                "source": "graph",
            })
            if len(rows) >= limit:
                break
        return rows

    def _poll_imap_once(self, filters: List[str], excluded: Set[str], start_after: float) -> Optional[str]:
        token = self._get_imap_token()
        auth = f"user={self.account.email}\x01auth=Bearer {token}\x01\x01"
        last_error = None
        for host in ("outlook.office365.com", "imap-mail.outlook.com"):
            mail = None
            try:
                mail = imaplib.IMAP4_SSL(host, 993)
                mail.authenticate("XOAUTH2", lambda _: auth.encode())
                mail.select("INBOX")
                status, data = mail.search(None, "ALL")
                if status != "OK":
                    continue
                for msg_id in reversed(data[0].split()[-30:]):
                    status, fetched = mail.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        continue
                    raw = next((item[1] for item in fetched if isinstance(item, tuple)), b"")
                    msg = message_from_bytes(raw)
                    msg_ts = self._message_time(msg)
                    if not self._is_recent_enough(msg_ts, start_after):
                        continue
                    sender = msg.get("From", "")
                    subject = _decode_header(msg.get("Subject", ""))
                    body = self._message_body(msg)
                    haystack = f"{sender}\n{subject}\n{body}".lower()
                    if not any(f in haystack for f in filters):
                        continue
                    code = _extract_code(_strip_html(f"{subject}\n{body}"), excluded)
                    if code:
                        self._log(f"code found via IMAP ({host})")
                        return code
                return None
            except Exception as e:
                last_error = e
            finally:
                try:
                    if mail:
                        mail.logout()
                except Exception:
                    pass
        if last_error:
            raise last_error
        return None

    def _list_imap_messages(self, limit: int, sender_filter: str, include_body: bool) -> List[dict]:
        token = self._get_imap_token()
        auth = f"user={self.account.email}\x01auth=Bearer {token}\x01\x01"
        last_error = None
        for host in ("outlook.office365.com", "imap-mail.outlook.com"):
            mail = None
            try:
                mail = imaplib.IMAP4_SSL(host, 993)
                mail.authenticate("XOAUTH2", lambda _: auth.encode())
                mail.select("INBOX")
                status, data = mail.search(None, "ALL")
                if status != "OK":
                    continue
                rows = []
                for msg_id in reversed(data[0].split()[-100:]):
                    status, fetched = mail.fetch(msg_id, "(RFC822)")
                    if status != "OK":
                        continue
                    raw = next((item[1] for item in fetched if isinstance(item, tuple)), b"")
                    msg = message_from_bytes(raw)
                    sender_raw = _decode_header(msg.get("From", ""))
                    sender_name, sender_addr = parseaddr(sender_raw)
                    sender = sender_addr or sender_raw
                    subject = _decode_header(msg.get("Subject", ""))
                    body = _strip_html(self._message_body(msg))
                    haystack = f"{sender_raw}\n{subject}\n{body}".lower()
                    if sender_filter and sender_filter not in haystack:
                        continue
                    msg_ts = self._message_time(msg)
                    rows.append({
                        "id": msg_id.decode(errors="ignore"),
                        "from": sender,
                        "from_name": sender_name,
                        "subject": subject,
                        "date": msg.get("Date", ""),
                        "timestamp": msg_ts or 0,
                        "preview": self._clip(body, 260),
                        "body": self._clip(body, 4000) if include_body else "",
                        "source": f"imap:{host}",
                    })
                    if len(rows) >= limit:
                        break
                return rows
            except Exception as e:
                last_error = e
            finally:
                try:
                    if mail:
                        mail.logout()
                except Exception:
                    pass
        if last_error:
            raise last_error
        return []

    @staticmethod
    def _parse_graph_time(value: str) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _message_time(msg) -> Optional[float]:
        try:
            dt = parsedate_to_datetime(msg.get("Date", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    @staticmethod
    def _message_body(msg) -> str:
        chunks = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    chunks.append(payload.decode(charset, errors="ignore"))
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                chunks.append(payload.decode(charset, errors="ignore"))
        return "\n".join(chunks)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = re.sub(r"\s+", " ", text or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "..."


def poll_outlook_for_code(
    account: OutlookAccount,
    sender_filters: Optional[Iterable[str]] = None,
    timeout: int = 120,
    interval: int = 5,
    exclude_codes: Optional[Iterable[str]] = None,
    start_after: Optional[float] = None,
    verbose: bool = False,
    proxy: str = "",
    prefer_imap: bool = True,
) -> Optional[str]:
    return OutlookMailClient(
        account,
        verbose=verbose,
        proxy=proxy,
        prefer_imap=prefer_imap,
    ).poll_code(
        sender_filters=sender_filters,
        timeout=timeout,
        interval=interval,
        exclude_codes=exclude_codes,
        start_after=start_after,
    )


if __name__ == "__main__":
    accounts = load_outlook_accounts()
    print(f"Loaded {len(accounts)} Outlook accounts")
