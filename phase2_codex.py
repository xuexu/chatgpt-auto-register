"""
Phase 2 wrapper: OAuth login + bind email + upload to SUB2API.
"""

import json
import sys
from typing import Dict
from urllib.parse import parse_qs, urlparse


_DOCS = r"D:\qingfeng\Documents\逆向包"
if _DOCS not in sys.path:
    sys.path.insert(0, _DOCS)


def codex_login(
    session_token: str,
    phone: str,
    password: str,
    bind_email: str,
    oauth_url,
    icloud_cookies: dict = None,
    proxy: str = "",
    sub2api_url: str = "",
    sub2api_email: str = "",
    sub2api_pwd: str = "",
    sub2api_proxy_id: int = 0,
    sub2api_session_id: str = "",
    sub2api_state: str = "",
    verbose: bool = True,
) -> Dict:
    """
    Run the Phase 2 flow and, when session/state are available, finish the
    SUB2API exchange-code upload step.
    """
    from openai_bind_email import run_second_half

    del session_token  # Kept for compatibility with older callers.

    if isinstance(oauth_url, dict):
        oauth_info = oauth_url
        oauth_url = oauth_info.get("auth_url") or oauth_info.get("oauth_url") or ""
        sub2api_session_id = sub2api_session_id or oauth_info.get("session_id", "")
        sub2api_state = sub2api_state or oauth_info.get("state", "")

    if not sub2api_state and oauth_url:
        sub2api_state = parse_qs(urlparse(oauth_url).query).get("state", [""])[0]

    return run_second_half(
        oauth_url=oauth_url,
        phone=phone,
        password=password,
        icloud_email=bind_email,
        icloud_cookies=icloud_cookies or {},
        sub2api_url=sub2api_url,
        sub2api_email=sub2api_email,
        sub2api_password=sub2api_pwd,
        sub2api_proxy_id=sub2api_proxy_id,
        sub2api_session_id=sub2api_session_id,
        sub2api_state=sub2api_state,
        proxy=proxy,
        verbose=verbose,
    )


def get_oauth_url(
    sub2api_url: str,
    sub2api_email: str,
    sub2api_pwd: str,
    sub2api_proxy_id: int = 0,
) -> Dict[str, str]:
    """Generate OAuth URL metadata from SUB2API."""
    import requests as req

    login_resp = req.post(
        f"{sub2api_url}/api/v1/auth/login",
        json={"email": sub2api_email, "password": sub2api_pwd},
        timeout=30,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API login failed: {login_data}")

    token = login_data["data"]["access_token"]
    body = {"redirect_uri": "http://localhost:1455/auth/callback"}
    if sub2api_proxy_id:
        body["proxy_id"] = sub2api_proxy_id

    oauth_resp = req.post(
        f"{sub2api_url}/api/v1/admin/openai/generate-auth-url",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    oauth_data = oauth_resp.json()
    if oauth_data.get("code") != 0:
        raise RuntimeError(f"Generate OAuth URL failed: {oauth_data}")

    payload = oauth_data["data"]
    auth_url = payload["auth_url"]
    state = payload.get("state", "") or parse_qs(urlparse(auth_url).query).get("state", [""])[0]
    return {
        "auth_url": auth_url,
        "oauth_url": auth_url,
        "session_id": payload.get("session_id", ""),
        "state": state,
    }


def upload_session(
    session_token: str,
    icloud_email: str,
    sub2api_url: str,
    sub2api_email: str,
    sub2api_pwd: str,
    sub2api_proxy_id: int = 0,
    group_ids: list = None,
    access_token: str = "",
) -> dict:
    """Upload session_token + access_token directly to SUB2API."""
    import requests as req

    if group_ids is None:
        group_ids = [1]

    login_resp = req.post(
        f"{sub2api_url}/api/v1/auth/login",
        json={"email": sub2api_email, "password": sub2api_pwd},
        timeout=30,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API login failed: {login_data}")

    admin_token = login_data["data"]["access_token"]
    body = {
        "content": json.dumps(
            {
                "session_token": session_token,
                "access_token": access_token,
                "email": icloud_email,
            }
        ),
        "group_ids": group_ids,
        "priority": 1,
        "auto_pause_on_expired": True,
        "update_existing": True,
    }
    if sub2api_proxy_id:
        body["proxy_id"] = sub2api_proxy_id

    upload_resp = req.post(
        f"{sub2api_url}/api/v1/admin/accounts/import/codex-session",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    upload_data = upload_resp.json()
    result = {"ok": upload_data.get("code") == 0, "_raw": upload_data}
    if result["ok"]:
        items = upload_data.get("data", {}).get("items", [])
        if items:
            result["account_id"] = items[0].get("account_id") or items[0].get("id")
            result["action"] = items[0].get("action", "unknown")
        else:
            result["account_id"] = upload_data.get("data", {}).get("created") or upload_data.get("data", {}).get("updated")
        result["warnings"] = [str(w) for w in (upload_data.get("data", {}).get("warnings", []) or [])]
    return result
