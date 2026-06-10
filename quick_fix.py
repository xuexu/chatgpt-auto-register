#!/usr/bin/env python3
"""
Quick fix: login → submit profile → bind email → upload SUB2API
for already-registered phone numbers.

Key insight: TLS 1.2 adapter prevents DECRYPTION_FAILED_OR_BAD_RECORD_MAC
(OpenSSL 3.x bug on Windows). Anti-fraud bypass is in correct headers.

Usage:
  python3 quick_fix.py +15550000001 +15550000002 +1555XXXXXXX
"""

import json, ssl, sys, time, uuid
from pathlib import Path

from requests.adapters import HTTPAdapter

sys.path.insert(0, str(Path(__file__).parent))

from sentinel import Sentinel
from auto_register import load_config, random_name, random_birthdate

AUTH = "https://auth.openai.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
SEC_CH_UA = '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"'
SEC_CH_UA_FULL = '"Google Chrome";v="141.0.7390.77", "Not?A_Brand";v="8.0.0.0", "Chromium";v="141.0.7390.77"'

def _make_trace_headers() -> dict:
    """Generate Datadog RUM trace headers matching real Chrome behavior.
    traceparent trace-id = 0000000000000000 + hex(x-datadog-trace-id)
    traceparent parent-id = hex(x-datadog-parent-id)"""
    import random as _random
    dd_trace_id = str(_random.getrandbits(64))
    dd_parent_id = str(_random.getrandbits(64))
    traceparent_trace_id = "0000000000000000" + format(int(dd_trace_id), "016x")
    traceparent_parent_id = format(int(dd_parent_id), "016x")
    return {
        "traceparent": f"00-{traceparent_trace_id}-{traceparent_parent_id}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": dd_parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": dd_trace_id,
    }

JSON_H = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": AUTH,
    "priority": "u=1, i",
    "user-agent": UA,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version": '"141.0.7390.77"',
    "sec-ch-ua-full-version-list": SEC_CH_UA_FULL,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"19.0.0"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

NAV_H = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": UA,
    "sec-ch-ua": SEC_CH_UA,
    "sec-ch-ua-arch": '"x86"',
    "sec-ch-ua-bitness": '"64"',
    "sec-ch-ua-full-version": '"141.0.7390.77"',
    "sec-ch-ua-full-version-list": SEC_CH_UA_FULL,
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-model": '""',
    "sec-ch-ua-platform": '"Windows"',
    "sec-ch-ua-platform-version": '"19.0.0"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


class _TLS12Adapter(HTTPAdapter):
    """Force TLS 1.2 to prevent DECRYPTION_FAILED_OR_BAD_RECORD_MAC
    (OpenSSL 3.x + TLS 1.3 cipher negotiation bug on Windows)."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def make_session(proxy):
    """Create requests.Session with TLS 1.2 workaround.
    No curl_cffi — pure requests, per user requirement."""
    import requests as req
    s = req.Session()
    s.mount('https://', _TLS12Adapter())
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.verify = False
    return s


def login_and_submit_profile(phone, password, config, proxy, contact_code=""):
    """Login → password/verify → create_account.
    Uses requests.Session (matches ChatGPTRegister when proxy is set).
    Key fix: loads /about-you page BEFORE create_account to establish CSRF cookie.
    contact_code: OTP code for contact_verification step (optional)."""

    did = str(uuid.uuid4())
    sentinel = Sentinel(did)

    s = make_session(proxy)

    print(f"  [1] visit chatgpt.com ...")
    for retry in range(5):
        try:
            r = s.get("https://chatgpt.com/auth/login",
                      headers={**NAV_H, "sec-fetch-site": "none"},
                      allow_redirects=True, timeout=20)
            if r.status_code < 500:
                break
            print(f"    retry {retry+1}: status={r.status_code}")
            time.sleep(3)
        except Exception as e:
            err_str = str(e)
            if "connect" in err_str.lower() or "timeout" in err_str.lower():
                print(f"    proxy connect retry {retry+1}: {type(e).__name__}")
            else:
                print(f"    retry {retry+1}: {type(e).__name__}: {err_str[:80]}")
            time.sleep(5)
    else:
        raise RuntimeError("Cannot reach chatgpt.com")

    print(f"  [2] csrf ...")
    csrf = ""
    for csrf_retry in range(3):
        r = s.get("https://chatgpt.com/api/auth/csrf",
                  headers={**JSON_H, "origin": "https://chatgpt.com"}, timeout=30)
        if r.status_code == 200:
            try:
                csrf = r.json().get("csrfToken", "")
            except Exception:
                pass
        if csrf:
            break
        print(f"    csrf retry {csrf_retry+1}: status={r.status_code}")
        time.sleep(1.5)
    if not csrf:
        raise RuntimeError("No CSRF token")

    print(f"  [3] signin ({phone}) ...")
    params = (
        f"prompt=login&screen_hint=login_or_signup"
        f"&login_hint={phone.replace('+', '%2B')}"
        f"&ext-oai-did={did}&auth_session_logging_id={uuid.uuid4()}"
    )
    r = s.post(f"https://chatgpt.com/api/auth/signin/openai?{params}",
        data={"callbackUrl": "/", "csrfToken": csrf, "json": "true"},
        headers={
            "accept": "application/json",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://chatgpt.com",
            "user-agent": UA,
        },
        allow_redirects=False, timeout=30)
    try:
        signin_data = r.json()
    except Exception:
        raise RuntimeError(f"signin non-JSON response (status={r.status_code}): {r.text[:200]}")
    url = signin_data.get("url", "")
    if not url:
        raise RuntimeError(f"signin no url field (status={r.status_code}): {json.dumps(signin_data)[:200]}")

    print(f"  [4] jump_to_auth ...")
    r = s.get(url,
              headers={**NAV_H, "referer": "https://chatgpt.com", "sec-fetch-site": "cross-site"},
              allow_redirects=False, timeout=30)
    loc = r.headers.get("Location", "")
    if not loc:
        # Sometimes the response is a 200 page directly (no redirect)
        print(f"      auth: no Location header (status={r.status_code}), using current page")
    else:
        print(f"      auth: {loc}")
        r = s.get(loc,
                  headers={**NAV_H, "referer": AUTH, "sec-fetch-site": "same-origin"},
                  allow_redirects=True, timeout=30)

    # Password verify
    print(f"  [5] POST /api/accounts/password/verify ...")
    st = sentinel.get(s, "password_verify")
    pw_headers = {**JSON_H, "referer": f"{AUTH}/log-in/password",
                  "oai-device-id": did, "OpenAI-Sentinel-Token": st}
    pw_headers.update(_make_trace_headers())
    r = s.post(f"{AUTH}/api/accounts/password/verify",
        json={"password": password},
        headers=pw_headers,
        timeout=30)
    pw_data = r.json()
    page_type = (pw_data.get("page") or {}).get("type", "")
    cont = pw_data.get("continue_url", "")
    print(f"      page_type: {page_type}, continue_url: {'yes' if cont else 'no'}")

    # Handle pages that aren't "about_you" (e.g. "external_url" for accounts with existing profile)
    if page_type == "external_url" and cont:
        print(f"      external_url continue, following OAuth callback directly...")
        r = s.get(cont,
                  headers={**NAV_H, "referer": AUTH, "sec-fetch-site": "cross-site"},
                  allow_redirects=True, timeout=30)
        # Try multiple cookie names
        token = (s.cookies.get("__Secure-next-auth.session-token", "")
                 or s.cookies.get("next-auth.session-token", "")
                 or s.cookies.get("session-token", ""))
        # Also check all cookies for any session-related ones
        if not token:
            for c in s.cookies:
                if "session" in c.name.lower() and c.value:
                    token = c.value
                    break
        print(f"      session_token: {'YES' if token else 'NO'}")
        # Get access token from session
        at = ""
        try:
            r2 = s.get("https://chatgpt.com/api/auth/session", headers=JSON_H, timeout=30)
            at = r2.json().get("accessToken", "")
        except:
            pass
        print(f"      access_token: {'YES' if at else 'NO'}")
        s.close()
        return {
            "ok": True, "phone": phone, "password": password,
            "name": "", "birthdate": "",
            "session_token": token, "access_token": at,
        }

    # Handle contact_verification (phone OTP challenge)
    if page_type == "contact_verification" and cont:
        print(f"      contact_verification, need phone OTP...")
        # Follow continue_url to trigger OTP send (may already be sent)
        try:
            r = s.get(cont,
                      headers={**NAV_H, "referer": f"{AUTH}/log-in/password"},
                      allow_redirects=True, timeout=30)
            print(f"      OTP trigger: status={r.status_code}")
        except Exception as e:
            print(f"      OTP trigger warning: {e}")

        if not contact_code:
            # Try interactive input
            try:
                contact_code = input("  [?] Enter phone OTP code: ").strip()
            except (EOFError, OSError):
                pass

        if not contact_code:
            s.close()
            return {"ok": False, "phone": phone, "error": "contact_verification_need_code"}

        print(f"  [5a] POST /api/accounts/phone-otp/validate (code={contact_code}) ...")
        st_otp = sentinel.get(s, "authorize_continue")
        otp_headers = {**JSON_H, "referer": f"{AUTH}/contact-verification",
                       "oai-device-id": did, "OpenAI-Sentinel-Token": st_otp}
        otp_headers.update(_make_trace_headers())
        r = s.post(f"{AUTH}/api/accounts/phone-otp/validate",
            json={"code": contact_code},
            headers=otp_headers,
            timeout=30)
        try:
            otp_data = r.json()
        except:
            otp_data = {}
        if not r.ok:
            err_detail = (otp_data.get("error") or {}).get("message", otp_data.get("error", r.text[:120]))
            print(f"      validate_otp: FAIL status={r.status_code} err={err_detail}")
            s.close()
            return {"ok": False, "phone": phone, "error": f"contact_verification_otp_failed: {err_detail}"}
        page_type = (otp_data.get("page") or {}).get("type", "")
        cont = otp_data.get("continue_url", "")
        print(f"      validate_otp: status={r.status_code} page={page_type} continue={'yes' if cont else 'no'}")

        if page_type == "external_url" and cont:
            # Similar to external_url branch above
            print(f"      OTP OK, following OAuth callback...")
            r = s.get(cont,
                      headers={**NAV_H, "referer": AUTH, "sec-fetch-site": "cross-site"},
                      allow_redirects=True, timeout=30)
            token = (s.cookies.get("__Secure-next-auth.session-token", "")
                     or s.cookies.get("next-auth.session-token", "")
                     or s.cookies.get("session-token", ""))
            if not token:
                for c in s.cookies:
                    if "session" in c.name.lower() and c.value:
                        token = c.value
                        break
            at = ""
            try:
                r2 = s.get("https://chatgpt.com/api/auth/session", headers=JSON_H, timeout=30)
                at = r2.json().get("accessToken", "")
            except:
                pass
            s.close()
            return {
                "ok": True, "phone": phone, "password": password,
                "name": "", "birthdate": "",
                "session_token": token, "access_token": at,
            }
        # After OTP, should be at about_you or have continue_url
        # (the existing about-you/create_account flow handles the rest)

    if page_type != "about_you" and not cont:
        raise RuntimeError(f"password_verify unexpected: {json.dumps(pw_data, default=str)[:300]}")

    # Load /about-you page to get CSRF cookie (browser does this)
    print(f"  [5b] Load /about-you page ...")
    r = s.get(f"{AUTH}/about-you",
              headers={**NAV_H, "referer": f"{AUTH}/log-in/password"},
              allow_redirects=True, timeout=30)
    csrf_cookie = s.cookies.get("__Host-next-auth.csrf-token", "")
    print(f"      csrf_cookie: {'yes' if csrf_cookie else 'no'}")

    # Establish session: make XHR probes like a real browser (matching open-reg-auto pattern)
    print(f"  [5c] Establishing session (XHR probes) ...")
    probe_urls = [
        f"{AUTH}/api/auth/session",
        f"{AUTH}/api/client_auth_session_dump",
    ]
    for probe_url in probe_urls:
        try:
            ph = {**JSON_H, "referer": f"{AUTH}/about-you"}
            ph.update(_make_trace_headers())
            s.get(probe_url, headers=ph, timeout=15)
        except Exception:
            pass

    # create_account with retries
    print(f"  [6] POST /api/accounts/create_account (submitting profile) ...")
    callback_url = ""
    last_name = ""
    last_bd = ""

    for attempt in range(1, 8):
        name = random_name()
        bd = random_birthdate()

        # Reload /about-you page on retry to refresh CSRF cookie and session state
        if attempt > 1:
            try:
                s.get(f"{AUTH}/about-you",
                      headers={**NAV_H, "referer": f"{AUTH}/log-in/password"},
                      allow_redirects=True, timeout=30)
            except:
                pass

        # Generate fresh sentinel token each attempt
        st2 = ""
        so_token = ""
        try:
            raw = sentinel.get(s, "oauth_create_account")
            st_data = json.loads(raw)
            so_token = st_data.pop("_so_token", "")
            st2 = json.dumps(st_data)  # stripped of _so_token
        except Exception as e:
            st2 = ""

        if attempt == 1:
            csrf_token = s.cookies.get("__Host-next-auth.csrf-token", "")
            print(f"      [debug] sentinel_len={len(st2)} so_token={'yes' if so_token else 'no'}")

        headers = {
            **JSON_H,
            "referer": f"{AUTH}/about-you",
            "oai-device-id": did,
            "OpenAI-Sentinel-Token": st2,
        }
        if so_token:
            headers["OpenAI-Sentinel-SO-Token"] = so_token
        headers.update(_make_trace_headers())

        r = s.post(f"{AUTH}/api/accounts/create_account",
            json={"name": name, "birthdate": bd},
            headers=headers, timeout=30)

        result = {}
        if "application/json" in r.headers.get("content-type", ""):
            try:
                result = r.json()
            except:
                pass

        if not r.ok:
            err_obj = result.get("error", {}) if isinstance(result.get("error"), dict) else {}
            err_msg = err_obj.get("message", str(result.get("error", "")))
            err_code = err_obj.get("code", "")
            status = f"status={r.status_code} code={err_code} err={err_msg[:60]}"
        else:
            callback_url = result.get("continue_url", "")
            err_obj = result.get("error", {}) if isinstance(result.get("error"), dict) else {}
            err_msg = err_obj.get("message", str(result.get("error", "")))
            err_code = err_obj.get("code", "")
            status = "OK" if callback_url else f"code={err_code} err={err_msg[:60]}"

        print(f"      [{attempt}] {name} bdate={bd} → {status}")

        if callback_url:
            last_name = name
            last_bd = bd
            break

        # Check both the error code field AND message text for registration_disallowed
        is_disallowed = (
            err_code == "registration_disallowed"
            or "registration_disallowed" in err_msg.lower()
            or "registration_disallowed" in str(result.get("error", "")).lower()
        )

        if not is_disallowed:
            # Different error - might not be retryable
            if attempt >= 4:
                break

        time.sleep(1.5)

    if not callback_url:
        s.close()
        return {"ok": False, "phone": phone, "error": "create_account_all_attempts_failed"}

    # OAuth callback → session token
    print(f"  [7] OAuth callback ...")
    r = s.get(callback_url,
              headers={**NAV_H, "referer": AUTH, "sec-fetch-site": "cross-site"},
              allow_redirects=True, timeout=30)
    token = s.cookies.get("__Secure-next-auth.session-token", "")
    print(f"      session_token: {'YES' if token else 'NO'}")

    # Access token
    print(f"  [8] GET /api/auth/session ...")
    r = s.get("https://chatgpt.com/api/auth/session", headers=JSON_H, timeout=30)
    try:
        at = r.json().get("accessToken", "")
    except:
        at = ""
    print(f"      access_token: {'YES' if at else 'NO'}")

    s.close()

    return {
        "ok": True,
        "phone": phone,
        "password": password,
        "name": last_name,
        "birthdate": last_bd,
        "session_token": token,
        "access_token": at,
    }


def bind_email_and_upload(result, config):
    """Phase 2: bind email and upload to SUB2API."""
    from openai_oauth import OpenAI_OAuth
    from openai_bind_email import run_second_half

    sub = config.get("sub2api", {})
    if not (sub.get("url") and sub.get("email") and (result.get("session_token") or result.get("access_token"))):
        print(f"  [!] No SUB2API config or no tokens, skipping")
        return False

    mail_provider = (config.get("mail_provider") or "").strip().lower()
    icloud_cookies = {}
    phase_email = config.get("bind_email", "")

    if mail_provider == "outlook" and not phase_email:
        from outlook_mail import reserve_next_outlook
        outlook_account = reserve_next_outlook(
            config.get("outlook_pool") or "outlook.txt",
            config.get("outlook_used") or "outlook_used.txt",
        )
        phase_email = outlook_account.email
        print(f"  Outlook email: {phase_email}")
    elif mail_provider == "icloud":
        # Read iCloud cookies
        path = config.get("icloud_cookies", "")
        if path and Path(path).exists():
            icloud_cookies = json.loads(Path(path).read_text(encoding="utf-8"))
        if not phase_email and config.get("icloud", {}).get("user"):
            phase_email = config["icloud"]["user"]

    if not phase_email:
        print(f"  [!] No bind email available")
        return False

    # Query group ID
    group_id = 1
    group_name = sub.get("group") or "CHATGPT"
    try:
        import requests as _r
        from openai_oauth import _json_or_raise
        sub_base = sub["url"].rstrip("/")
        r = _r.post(f"{sub_base}/api/v1/auth/login",
                    json={"email": sub["email"], "password": sub.get("pwd", "")},
                    timeout=15)
        admin_token = _json_or_raise(r, "SUB2API login")["data"]["access_token"]
        r2 = _r.get(f"{sub_base}/api/v1/admin/groups",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    timeout=15)
        groups = _json_or_raise(r2, "SUB2API groups").get("data", {}).get("items", [])
        for g in groups:
            if g.get("name") == group_name:
                group_id = int(g.get("id") or 1)
                print(f"  SUB2API group '{group_name}' ID={group_id}")
                break
    except Exception as e:
        print(f"  Group lookup failed: {e}, using ID=1")

    # Get OAuth URL
    print(f"  Getting OAuth URL from SUB2API ...")
    oauth_url, session_id, state = OpenAI_OAuth.get_oauth_url_from_sub2api(
        sub["url"], sub["email"], sub.get("pwd", ""), sub.get("proxy_id") or None,
    )

    # Run phase 2
    icfg = config.get("icloud", {})
    print(f"  Binding email {phase_email} and uploading ...")

    result2 = run_second_half(
        oauth_url=oauth_url,
        phone=result["phone"],
        password=result.get("password", ""),
        icloud_email=phase_email,
        icloud_cookies=icloud_cookies,
        sub2api_url=sub["url"],
        sub2api_email=sub["email"],
        sub2api_password=sub.get("pwd", ""),
        sub2api_proxy_id=sub.get("proxy_id", 0) or 0,
        proxy=config.get("proxy", ""),
        verbose=False,
        imap_user=icfg.get("user", ""),
        imap_password=icfg.get("pass", ""),
        sub2api_session_id=session_id,
        sub2api_state=state,
        outlook_pool=config.get("outlook_pool", ""),
    )

    if result2.get("ok"):
        account_id = result2.get("sub2api_account_id", "?")
        print(f"  Phase 2 OK! SUB2API account #{account_id}")
        return True
    else:
        print(f"  Phase 2 FAIL: {result2.get('error', '')}")
        return False


def main():
    if len(sys.argv) < 2:
        print(f"Usage: python3 quick_fix.py +57XXXXXXXX +57YYYYYYYY ...")
        print(f"       python3 quick_fix.py +57XXXXXXXX=OTPCODE  (for contact_verification)")
        sys.exit(1)

    raw_args = sys.argv[1:]
    # Parse phone=code syntax
    phones = []
    codes = {}
    for arg in raw_args:
        if "=" in arg:
            phone, code = arg.split("=", 1)
            phones.append(phone)
            codes[phone] = code
        else:
            phones.append(arg)

    config = load_config()
    proxy = config.get("proxy", "")
    password = config.get("register", {}).get("password", "yishao.gpt666")

    print(f"Targets: {phones}")
    print(f"Password: {password}")
    print(f"Proxy: {proxy or 'direct'}")
    print()

    results = []
    for phone in phones:
        contact_code = codes.get(phone, "")
        print(f"\n{'='*60}")
        print(f" Processing: {phone}")
        if contact_code:
            print(f" OTP code: {contact_code}")
        print(f"{'='*60}")

        try:
            # Step 1: Login and submit profile
            result = login_and_submit_profile(phone, password, config, proxy, contact_code=contact_code)
            results.append(result)

            if not result.get("ok"):
                print(f"\n  [FAIL] Profile submission failed: {result.get('error', '')}")
                continue

            print(f"\n  [OK] Profile submitted! name={result.get('name')}")

            # Step 2: Bind email and upload to SUB2API
            print(f"\n  --- Phase 2: Bind email + SUB2API ---")

            # Need at least access_token for Phase 2
            if not result.get("access_token"):
                print(f"  [FAIL] No access token, cannot proceed to Phase 2")
                continue

            bind_email_and_upload(result, config)

        except Exception as e:
            print(f"\n  [FAIL] Exception: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            results.append({"ok": False, "phone": phone, "error": str(e)})

    # Summary
    print(f"\n\n{'='*60}")
    print(f" SUMMARY")
    print(f"{'='*60}")
    ok_profiles = sum(1 for r in results if r.get("ok"))
    print(f"Profile submitted: {ok_profiles}/{len(phones)}")
    for r in results:
        status = "OK" if r.get("ok") else "FAIL"
        name = r.get("name", "?")
        tok = "YES" if r.get("session_token") else "NO"
        at = "YES" if r.get("access_token") else "NO"
        print(f"  {r.get('phone','?')}: {status} name={name} session={tok} access={at}")

    # Save
    out_path = Path(__file__).parent / "mark" / f"quick_fix_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
