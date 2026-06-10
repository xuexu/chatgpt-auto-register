#!/usr/bin/env python3
"""ChatGPT Auto Register - Web GUI (Flask + SSE)"""

import copy, json, os, queue, sys, threading, time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)

from flask import Flask, request, jsonify, Response, send_file

app = Flask(__name__)
sys.path.insert(0, str(Path(__file__).parent))
from smsbower import SmsBower
import auto_register as ar
from outlook_mail import (
    OutlookMailClient,
    get_outlook_account,
    load_outlook_accounts,
    mark_outlook_status,
)
from outlook_manager import _read_used as _read_outlook_used

_STATE_LOCK = threading.RLock()


def _bounded_int(value, default=1, minimum=1, maximum=99):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(v, maximum))


def _stop_requested():
    with _STATE_LOCK:
        return _state["stop"]


def _empty_stats():
    return {
        "current_success": 0,
        "current_fail": 0,
        "total_success": 0,
        "total_fail": 0,
    }


_state = {
    "running": False, "stop": False, "results": [],
    "stats": _empty_stats(),
    "config": ar.load_config(), "log_queue": queue.Queue(),
    "log_lines": [], "log_cursor": 0,
    "code_queue": queue.Queue(),
    "pause_queue": queue.Queue(),
    "_paused": False, "_need_code": False,
    "_code_queues": {},
    "_code_waiting": {},
}

# 鈹€鈹€ 閭鍘婚噸 鈹€鈹€
def _ensure_stats():
    stats = _state.get("stats")
    if not isinstance(stats, dict):
        stats = _empty_stats()
        _state["stats"] = stats
        return stats
    for key, default in _empty_stats().items():
        try:
            stats[key] = int(stats.get(key, default))
        except (TypeError, ValueError):
            stats[key] = default
    return stats


def _reset_current_stats():
    with _STATE_LOCK:
        stats = _ensure_stats()
        stats["current_success"] = 0
        stats["current_fail"] = 0


def _record_result(result: dict):
    with _STATE_LOCK:
        _state["results"].append(result)
        stats = _ensure_stats()
        if result.get("ok"):
            stats["current_success"] += 1
            stats["total_success"] += 1
        else:
            stats["current_fail"] += 1
            stats["total_fail"] += 1


def _status_payload():
    with _STATE_LOCK:
        results = [_sanitize_result(r) for r in _state["results"]]
        stats = dict(_ensure_stats())
        running = _state["running"]
    return {"running": running, "results": results, "stats": stats}


_email_blacklist = set()
_claimed_emails = set()
_bl_file = Path(__file__).parent / "email_blacklist.json"
_bl_lock = threading.Lock()
_cl_lock = threading.Lock()

_OUTLOOK_STATUS_LABELS = {
    "success": "已注册成功",
    "bad": "坏号",
    "verify_failed": "验证失败",
    "reserved": "已预留",
    "register_failed": "注册失败",
    "unused": "未使用",
}
_OUTLOOK_SUMMARY_KEYS = [
    "unused",
    "reserved",
    "success",
    "register_failed",
    "verify_failed",
    "bad",
]

def _load_email_blacklist():
    global _email_blacklist
    if _bl_file.exists():
        try:
            _email_blacklist = set(json.loads(_bl_file.read_text(encoding="utf-8")))
        except Exception:
            pass

def _save_email_blacklist():
    with _bl_lock:
        try:
            _bl_file.write_text(json.dumps(sorted(_email_blacklist), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except Exception:
            pass

_load_email_blacklist()

# 鈹€鈹€ iCloud cookies 鏈湴鍌ㄥ瓨 鈹€鈹€
COOKIES_FILE = Path(__file__).parent / "icloud_cookies.json"

def _load_icloud_cookies():
    if COOKIES_FILE.exists():
        try:
            return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _iter_phase2_icloud_cookie_paths(config: dict):
    raw_path = (config or {}).get("icloud_cookies", "")
    if raw_path:
        path = Path(raw_path)
        yield path
        if not path.is_absolute():
            yield Path(__file__).parent / path
    yield COOKIES_FILE
    yield Path(__file__).parent / "cookies.json"


def _load_phase2_icloud_cookies(config: dict):
    for path in _iter_phase2_icloud_cookie_paths(config):
        if not path or not Path(path).exists():
            continue
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:
            continue
    try:
        from icloud_hme import extract_chrome_cookies

        cookies = extract_chrome_cookies()
        if cookies:
            return cookies
    except Exception:
        pass
    return {}

def _normalize_log_msg(msg) -> str:
    text = str(msg)
    replacements = {
        "\u6d63\u6b13\ue582": "余额",
        "\u9a9e\u8dfa\u5f42": "并发",
        "\u7efe\u8de8\u25bc": "线程",
        "\u6fb6\u8fab\u89e6": "失败",
        "\u5a09\u3125\u553d\u93b4\u612c\u59db": "注册成功",
        "\u5a09\u3125\u553d\u7039\u5c7e\u579a\u951b\u5c7d\u51e1\u93c6\u509a\u4ee0": "注册完成，已暂停",
        "\u6d93\u5a41\u7d36\u93b4\u612c\u59db": "上传成功",
        "\u9477\ue044\u59e9\u74ba\u5ba0\u7e43": "自动跳过",
        "\u9422\u3126\u57db\u95ab\u590b\u5ae8\u74ba\u5ba0\u7e43": "用户选择跳过",
        "\u7edb\u590a\u7ddf\u74d2\u546e\u6902\u951b\u5c83\u70e6\u6769": "等待超时，跳过",
        "\u95ad\ue1be\ue188": "邮箱",
        "\u7f01\u6226\u5056\u7ee0": "绑定邮箱",
        "\u95ab\u590a\u757e": "选定",
        "\u947e\u5cf0\u5f47": "获取",
        "\u5bb8\u53c9\u7223\u7481": "已标记",
        "\u5bb8\u63d2\u4ee0\u59dd\u3222\u74d1\u5bf0\u546e\u589c\u93c8\u54c4\u5f7f": "已停止等待手机号",
        "\u5a0c\u2103\u6e41\u9359\ue21c\u6564": "没有可用",
        "\u9352\u5b58\u5474": "取号",
        "\u7487\u8bf2\u5f47": "读取",
        "\u7d22\u5f15": "索引",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    # Common mojibake fragments from UTF-8 text decoded through legacy Chinese encodings.
    text = text.replace("鎷挎墜鏈哄彿", "拿手机号")
    text = text.replace("澶辫触", "失败")
    text = text.replace("鍚庨噸璇", "后重试")
    text = text.replace("绗", "第")
    text = text.replace("娆", "次")
    text = text.replace("鎵嬫満鍙", "手机号")
    text = text.replace("婵€娲籌D", "激活ID")
    text = text.replace("楠岃瘉鐮", "验证码")
    text = text.replace("娉ㄥ唽", "注册")
    text = text.replace("鍒涘缓璐︽埛", "创建账号")
    text = text.replace("鐧诲綍", "登录")
    text = text.replace("鑾峰彇", "获取")
    text = text.replace("鍒嗙粍", "分组")
    text = text.replace("涓婁紶", "上传")
    text = text.replace("璺宠繃", "跳过")
    text = text.replace("閭", "邮箱")
    text = text.replace("绾跨▼", "线程")
    text = text.replace("骞跺彂", "并发")
    return text

def _log(msg, tag="info", thread_id=None):
    ts = time.strftime("%H:%M:%S")
    item = {"msg": _normalize_log_msg(msg), "tag": tag, "time": ts}
    if thread_id is not None:
        item["thread"] = int(thread_id)
    _state["log_queue"].put(item)
    with _STATE_LOCK:
        _state["log_lines"].append(item)
        if len(_state["log_lines"]) > 2000:
            _state["log_lines"] = _state["log_lines"][-1500:]
@app.route("/")
def index():
    return Response(_HTML, mimetype="text/html; charset=utf-8")

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        d = request.json or {}
        cfg = ar.load_config()
        for k in ["api_key", "proxy", "country", "service", "password", "min_price", "max_price", "code_timeout",
                   "provider", "sms_timeout", "imap_user", "imap_pass", "sub2api_url", "sub2api_email",
                   "sub2api_pwd", "sub2api_group", "sub2api_proxy_id", "bind_email", "icloud_cookies",
                "email_provider", "mailmanage_key", "mailmanage_category", "mailmanage_keyword", "outlook_pool",
                "tempmail_base_url", "tempmail_jwt", "tempmail_site_password", "tempmail_admin_password",
                "tempmail_domain", "tempmail_name_prefix", "tempmail_pool", "tempmail_keyword",
                "debug_mode", "no_phase2", "phase2_auto_skip",
                "plus_method", "plus_email", "plus_phone", "plus_pin",
                "plus_country", "plus_currency"]:
            if k in d:
                if k == "api_key": cfg["smsbower"]["api_key"] = d[k]
                elif k in ("code_timeout", "sms_timeout"): cfg[k] = int(d[k]) if d[k] else 30
                elif k == "password": cfg["register"]["password"] = d[k]
                elif k in ("proxy", "country", "service", "min_price", "max_price", "provider"): cfg[k] = d[k]
                elif k == "imap_user": cfg["icloud"] = cfg.get("icloud", {}); cfg["icloud"]["user"] = d[k]
                elif k == "imap_pass": cfg["icloud"] = cfg.get("icloud", {}); cfg["icloud"]["pass"] = d[k]
                elif k == "sub2api_url": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["url"] = d[k]
                elif k == "sub2api_email": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["email"] = d[k]
                elif k == "sub2api_pwd": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["pwd"] = d[k]
                elif k == "bind_email": cfg["bind_email"] = d[k]
                elif k == "sub2api_group": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["group"] = d[k]
                elif k == "sub2api_proxy_id": cfg["sub2api"] = cfg.get("sub2api", {}); cfg["sub2api"]["proxy_id"] = int(d[k]) if d[k] else 0
                elif k == "icloud_cookies": cfg["icloud_cookies"] = d[k]
                elif k == "mailmanage_key": cfg["mailmanage"] = cfg.get("mailmanage", {}); cfg["mailmanage"]["api_key"] = d[k]
                elif k == "mailmanage_category": cfg["mailmanage"] = cfg.get("mailmanage", {}); cfg["mailmanage"]["category"] = d[k]
                elif k == "mailmanage_keyword": cfg["mailmanage"] = cfg.get("mailmanage", {}); cfg["mailmanage"]["keyword"] = d[k]
                elif k == "email_provider": cfg["email_provider"] = d[k]
                elif k == "outlook_pool": cfg[k] = d[k]
                elif k == "tempmail_base_url": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["base_url"] = d[k]
                elif k == "tempmail_jwt": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["jwt"] = d[k]
                elif k == "tempmail_site_password": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["site_password"] = d[k]
                elif k == "tempmail_admin_password": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["admin_password"] = d[k]
                elif k == "tempmail_domain": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["domain"] = d[k]
                elif k == "tempmail_name_prefix": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["name_prefix"] = d[k]
                elif k == "tempmail_pool": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["pool"] = d[k]
                elif k == "tempmail_keyword": cfg["tempmail"] = cfg.get("tempmail", {}); cfg["tempmail"]["keyword"] = d[k]
                elif k in ("plus_method", "plus_email", "plus_phone", "plus_pin", "plus_country", "plus_currency"):
                    cfg[k] = d[k]
                elif k == "debug_mode": cfg["debug_mode"] = d[k] == "1" or d[k] is True
                elif k == "no_phase2": cfg["no_phase2"] = d[k] == "1" or d[k] is True
                elif k == "phase2_auto_skip": cfg["phase2_auto_skip"] = d[k] == "1" or d[k] is True
        cfg.pop("phase2", None)
        _state["config"] = cfg
        _save_config_file(cfg)
        return jsonify({"ok": True, "config": _sanitize_config(cfg)})
    cfg = ar.load_config()
    _state["config"] = cfg
    return jsonify({"ok": True, "config": _sanitize_config(cfg)})

@app.route("/api/balance")
def api_balance():
    key = _state.get("config", {}).get("smsbower", {}).get("api_key", "")
    if not key: return jsonify({"ok": False, "error": "No API key"})
    try:
        return jsonify({"ok": True, "balance": SmsBower(key).balance()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/test-tempmail", methods=["POST"])
def api_test_tempmail():
    cfg = _state.get("config", {}) or ar.load_config()
    tm_config = cfg.get("tempmail", {})
    try:
        from tempmail_client import TempMailClient

        client = TempMailClient(
            base_url=tm_config.get("base_url", ""),
            jwt=tm_config.get("jwt", ""),
            site_password=tm_config.get("site_password", ""),
            admin_password=tm_config.get("admin_password", ""),
            domain=tm_config.get("domain", ""),
            name_prefix=tm_config.get("name_prefix", ""),
            pool=tm_config.get("pool", ""),
            verbose=False,
        )
        data = client.test_connection()
        return jsonify({"ok": True, **data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/test-sub2api", methods=["POST"])
def api_test_sub2api():
    cfg = _state.get("config", {}) or ar.load_config()
    sub = cfg.get("sub2api", {})
    if not sub.get("url") or not sub.get("email"):
        return jsonify({"ok": False, "error": "SUB2API 地址或管理邮箱未配置"})
    try:
        import requests as _r

        base_url = sub["url"].rstrip("/")
        login_resp = _r.post(
            f"{base_url}/api/v1/auth/login",
            json={"email": sub["email"], "password": sub.get("pwd", "")},
            timeout=15,
        )
        login_data = login_resp.json()
        if login_data.get("code") != 0:
            return jsonify({"ok": False, "error": f"SUB2API 登录失败: {login_data.get('message', '?')}"})
        token = login_data.get("data", {}).get("access_token")
        if not token:
            return jsonify({"ok": False, "error": "SUB2API 登录成功但未返回 access_token"})

        groups_count = None
        group_exists = None
        group_name = sub.get("group", "CHATGPT")
        try:
            group_resp = _r.get(
                f"{base_url}/api/v1/admin/groups",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            groups = group_resp.json().get("data", {}).get("items", [])
            groups_count = len(groups)
            group_exists = any(g.get("name") == group_name for g in groups)
        except Exception:
            pass

        return jsonify({
            "ok": True,
            "email": sub["email"],
            "groups_count": groups_count,
            "group": group_name,
            "group_exists": group_exists,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/api/smsbower-countries")
def api_smsbower_countries():
    cfg = _state.get("config", {}) or {}
    key = cfg.get("smsbower", {}).get("api_key", "")
    service = request.args.get("service") or cfg.get("service") or "dr"
    if not key:
        return jsonify({"ok": False, "error": "No API key"}), 400
    try:
        rows = SmsBower(key).list_country_prices(service=service)
        return jsonify({"ok": True, "service": service, "items": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/icloud-cookies", methods=["GET", "POST"])
def api_icloud_cookies():
    if request.method == "POST":
        d = request.json or {}
        raw = d.get("cookies", "")
        if not raw.strip():
            return jsonify({"ok": False, "error": "cookies 涓虹┖"})
        try:
            cookies = json.loads(raw)
        except json.JSONDecodeError as e:
            return jsonify({"ok": False, "error": f"JSON 瑙ｆ瀽澶辫触: {e}"})
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        _log(f"iCloud cookies 宸蹭繚瀛?({len(str(cookies))} bytes)", "success")
        return jsonify({"ok": True, "size": len(str(cookies))})
    if COOKIES_FILE.exists():
        try:
            cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
            return jsonify({"ok": True, "loaded": True, "size": len(str(cookies)),
                            "preview": str(cookies)[:200]})
        except Exception:
            return jsonify({"ok": True, "loaded": False, "error": "cookies file exists but parse failed"})
    return jsonify({"ok": True, "loaded": False})


@app.route("/api/plus-upgrade", methods=["POST"])
def api_plus_upgrade():
    """瑙﹀彂 ChatGPT Plus 鍗囩骇"""
    d = request.json or {}
    access_token = d.get("access_token", "")
    session_token = d.get("session_token", "")
    if not access_token and not session_token:
        return jsonify({"ok": False, "error": "闇€瑕?access_token 鎴?session_token"})

    cfg = _state["config"]
    plus_cfg = cfg.get("plus", {})

    def _run_upgrade():
        from plus_payment import generate_plus_link, grab_midtrans_url
        from gopay_pay import GoPayPayment
        import gopay_register

        _log("=== Phase 3: Plus 鍗囩骇 ===", "success")

        _log("[Plus] 鐢熸垚鏀粯閾炬帴...", "info")
        try:
            cashier_url = generate_plus_link(
                access_token=access_token,
                cookies=cfg.get("cookies", ""),
                country=plus_cfg.get("country", "ID"),
                currency=plus_cfg.get("currency", "IDR"),
                proxy=cfg.get("proxy", ""),
            )
            _log(f"[Plus] Cashier URL: {cashier_url[:60]}...", "success")
        except Exception as e:
            _log(f"[Plus] 鐢熸垚鏀粯閾炬帴澶辫触: {e}", "error")
            return

        _log("[Plus] 娴忚鍣ㄦ姄鍙?Midtrans URL...", "info")
        try:
            midtrans_url = grab_midtrans_url(
                cashier_url,
                proxy=cfg.get("proxy", ""),
                headless=plus_cfg.get("headless", True),
            )
            _log(f"[Plus] Midtrans: {midtrans_url[:60]}...", "success")
        except Exception as e:
            _log(f"[Plus] 娴忚鍣ㄦ姄鍙栧け璐? {e}", "error")
            return

        payment_method = d.get("plus_method", "gopay")

        if payment_method == "paypal":
            _log("[Plus] PayPal 鍗忚璺嚎...", "info")
            try:
                from plus_payment import complete_paypal_checkout_protocol
                result = complete_paypal_checkout_protocol(
                    checkout_url=cashier_url,
                    cookies_str=cfg.get("cookies", ""),
                    proxy=cfg.get("proxy", ""),
                    email=d.get("plus_email", ""),
                    log_fn=lambda m: _log(f"[Plus] {m}", "info"),
                )
                if result.get("ok"):
                    _log(f"[Plus] PayPal 浠樻鎴愬姛!", "success")
                else:
                    _log(f"[Plus] PayPal 澶辫触: {result.get('error')}", "error")
            except Exception as e:
                _log(f"[Plus] PayPal 寮傚父: {e}", "error")
            return

        gopay_phone = plus_cfg.get("gopay_phone", "")
        gopay_pin = plus_cfg.get("gopay_pin", "")
        if not gopay_phone or not gopay_pin:
            _log("[Plus] 闇€瑕侀厤缃?GoPay 鎵嬫満鍙峰拰 PIN", "error")
            return

        _log(f"[Plus] GoPay 浠樻 {gopay_phone}...", "info")

        def wait_otp(phone, timeout):
            _log(f"[Plus] 绛夊緟 OTP ({phone}, {timeout}s)...", "warn")
            try:
                sms = SmsBower(cfg["smsbower"]["api_key"])
                code = sms.wait_code(timeout=timeout, interval=3)
                return code
            except Exception:
                return None

        try:
            payment = GoPayPayment(proxy=cfg.get("proxy", ""))
            result = payment.pay(
                midtrans_url=midtrans_url,
                phone=gopay_phone.lstrip("+").lstrip("62"),
                country_code="62",
                pin=gopay_pin,
                wait_otp=wait_otp,
            )
            if result.get("success"):
                _log(f"[Plus] 浠樻鎴愬姛! status={result.get('transaction_status')}", "success")
            else:
                _log(f"[Plus] 浠樻澶辫触: {result.get('detail')}", "error")
        except Exception as e:
            _log(f"[Plus] 浠樻寮傚父: {e}", "error")

    threading.Thread(target=_run_upgrade, daemon=True).start()
    return jsonify({"ok": True, "message": "Plus upgrade started"})


@app.route("/api/start", methods=["POST"])
def api_start():
    if _state["running"]: return jsonify({"ok": False, "error": "Already running"})
    d = request.json or {}
    with _STATE_LOCK:
        _state["running"] = True
        _state["stop"] = False
        _state["results"] = []
    _reset_current_stats()
    cfg = _state["config"]
    concurrency = max(1, min(int(d.get("concurrency", 1)), 10))
    threading.Thread(target=_run, args=(cfg, int(d.get("count", 1)), int(d.get("retries", 2)), concurrency), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/stop", methods=["POST"])
def api_stop():
    _state["stop"] = True
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify(_status_payload())

@app.route("/api/download")
def api_download():
    safe = [{k: v for k, v in r.items() if k != "access_token"}
            for r in _state["results"] if r.get("ok")]
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = Path(__file__).parent / f"results_{ts}.json"
    path.write_text(json.dumps(safe, indent=2, ensure_ascii=False), encoding="utf-8")
    return send_file(path, as_attachment=True, download_name=path.name)

@app.route("/api/submit-code", methods=["POST"])
def api_submit_code():
    d = request.json or {}
    code = d.get("code", "").strip()
    tid = d.get("thread_id", "")
    if code and len(code) >= 4:
        if tid and tid in _state.get("_code_queues", {}):
            _state["_code_queues"][tid].put(code)
        else:
            _state["code_queue"].put(code)
            for k, q in _state.get("_code_queues", {}).items():
                q.put(code)
                break
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "too short"})

@app.route("/api/waiting-code")
def api_waiting_code():
    waiting = _state.get("_code_waiting", {})
    if waiting:
        tid = list(waiting.keys())[0]
        return jsonify({"waiting": True, "thread_id": tid, "hint": waiting[tid]})
    return jsonify({"waiting": not _state["code_queue"].empty() or _state.get("_need_code", False)})

@app.route("/api/proxies")
def api_proxies():
    sub = _state["config"].get("sub2api", {})
    if not sub.get("url") or not sub.get("email"):
        return jsonify({"ok": False, "items": []})
    try:
        import requests as _r
        r = _r.post(f"{sub['url']}/api/v1/auth/login",
            json={"email": sub["email"], "password": sub.get("pwd", "")}, timeout=15)
        data = r.json()
        if data.get("code") != 0:
            return jsonify({"ok": False, "items": []})
        token = data["data"]["access_token"]
        r = _r.get(f"{sub['url']}/api/v1/admin/proxies",
            headers={"Authorization": f"Bearer {token}"}, timeout=15)
        pdata = r.json()
        items = pdata.get("data", {}).get("items", [])
        result = [{"id": p.get("id"), "name": p.get("name") or f"{p.get('host','')}:{p.get('port','')}"} for p in items]
        return jsonify({"ok": True, "items": result})
    except Exception as e:
        return jsonify({"ok": False, "items": [], "error": str(e)})

@app.route("/api/waiting-pause")
def api_waiting_pause():
    return jsonify({
        "paused": _state.get("_paused", False),
        "phase2_retry": _state.get("_phase2_retry", False),
    })

@app.route("/api/continue", methods=["POST"])
def api_continue():
    _state["pause_queue"].put("continue")
    _state["_paused"] = False
    return jsonify({"ok": True})

@app.route("/api/skip-phase2", methods=["POST"])
def api_skip_phase2():
    _state["_phase2_retry"] = False
    _state["_paused"] = False
    _state["pause_queue"].put("skip")
    return jsonify({"ok": True})

@app.route("/api/log-since/<int:cursor>")
def api_log_since(cursor):
    lines = _state["log_lines"][cursor:]
    return jsonify({"lines": lines, "cursor": len(_state["log_lines"])})

# ---- Results list API ----
@app.route("/api/results-list")
def api_results_list():
    source = request.args.get("source", "files")
    results_dir = Path(__file__).parent / "results"
    if not results_dir.exists():
        return jsonify({"ok": True, "items": []})
    items = []

    if source == "all":
        all_path = results_dir / "_all.json"
        if not all_path.exists():
            return jsonify({"ok": True, "items": []})
        try:
            all_data = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:
            return jsonify({"ok": True, "items": []})
        for idx, data in enumerate(all_data):
            if not data.get("ok"):
                continue
            items.append({
                "index": idx,
                "phone": data.get("phone", "?"),
                "name": data.get("name", ""),
                "has_phase2": bool(data.get("sub2api_id")),
                "sub2api_id": data.get("sub2api_id", ""),
            })
    else:
        for f in sorted(results_dir.iterdir(), key=lambda x: x.name):
            if f.suffix != ".json" or f.name == "_all.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not data.get("ok"):
                continue
            items.append({
                "filename": f.name,
                "phone": data.get("phone", "?"),
                "name": data.get("name", ""),
                "has_phase2": bool(data.get("sub2api_id")),
                "sub2api_id": data.get("sub2api_id", ""),
            })
    return jsonify({"ok": True, "items": items})


def _current_config() -> dict:
    cfg = _state.get("config")
    if isinstance(cfg, dict):
        return cfg
    cfg = ar.load_config()
    _state["config"] = cfg
    return cfg


def _outlook_results_dir() -> Path:
    return Path(__file__).parent / "results"


def _outlook_pool_source(config: dict) -> str:
    return (config or {}).get("outlook_pool") or "outlook.txt"


def _outlook_used_source(config: dict) -> str:
    return (config or {}).get("outlook_used") or "outlook_used.txt"


def _parse_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y/%m/%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _parse_result_file_time(path: Path) -> tuple[float, str]:
    stem_parts = path.stem.rsplit("_", 2)
    if len(stem_parts) >= 3:
        label = f"{stem_parts[-2]}_{stem_parts[-1]}"
        try:
            ts = datetime.strptime(label, "%Y%m%d_%H%M%S")
            return ts.timestamp(), ts.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    try:
        ts = path.stat().st_mtime
        return ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0.0, ""


def _extract_result_time(data: dict, fallback_ts: float = 0.0) -> tuple[float, str]:
    for key in ("last_result_time", "created_at", "updated_at", "saved_at", "time", "timestamp"):
        value = data.get(key, "")
        if isinstance(value, (int, float)) and value:
            ts = float(value)
            return ts, datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        ts = _parse_timestamp(value)
        if ts:
            return ts, str(value)
    if fallback_ts:
        return fallback_ts, datetime.fromtimestamp(fallback_ts).strftime("%Y-%m-%d %H:%M:%S")
    return 0.0, ""


def _load_outlook_result_records() -> list[dict]:
    results_dir = _outlook_results_dir()
    if not results_dir.exists():
        return []

    records = []
    all_path = results_dir / "_all.json"
    if all_path.exists():
        try:
            all_data = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:
            all_data = []
        if isinstance(all_data, list):
            for idx, row in enumerate(all_data, 1):
                if not isinstance(row, dict):
                    continue
                fallback_ts = float(idx)
                recorded_at, time_label = _extract_result_time(row, fallback_ts)
                records.append(
                    {
                        "ok": bool(row.get("ok")),
                        "phone": str(row.get("phone", "") or ""),
                        "sub2api_id": str(row.get("sub2api_id", "") or ""),
                        "bind_email": str(row.get("bind_email", "") or "").strip(),
                        "recorded_at": recorded_at,
                        "time_label": time_label,
                        "data": row,
                    }
                )

    for path in sorted(results_dir.glob("*.json")):
        if path.name == "_all.json":
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        fallback_ts, file_label = _parse_result_file_time(path)
        recorded_at, time_label = _extract_result_time(row, fallback_ts)
        records.append(
            {
                "ok": bool(row.get("ok")),
                "phone": str(row.get("phone", "") or ""),
                "sub2api_id": str(row.get("sub2api_id", "") or ""),
                "bind_email": str(row.get("bind_email", "") or "").strip(),
                "recorded_at": recorded_at,
                "time_label": time_label or file_label,
                "data": row,
            }
        )
    return records


def _classify_outlook_status(has_success_result: bool, last_event_status: str, has_result: bool) -> str:
    status = (last_event_status or "").strip().lower()
    if has_success_result:
        return "success"
    if status == "bad":
        return "bad"
    if status == "verify_failed":
        return "verify_failed"
    if status == "reserved":
        return "reserved"
    if has_result:
        return "register_failed"
    return "unused"


def _build_outlook_pool_entries(config: dict | None = None) -> list[dict]:
    config = config or _current_config()
    accounts = load_outlook_accounts(_outlook_pool_source(config))
    latest_statuses, events = _read_outlook_used(_outlook_used_source(config))
    latest_events = {}
    for event_time, email, status in events:
        latest_events[email.lower()] = {
            "time": event_time or "",
            "status": (status or "").strip().lower(),
        }

    latest_results = {}
    has_results = set()
    success_results = set()
    for record in _load_outlook_result_records():
        bind_email = record["bind_email"].lower()
        if not bind_email:
            continue
        has_results.add(bind_email)
        if record["sub2api_id"]:
            success_results.add(bind_email)
        current = latest_results.get(bind_email)
        if current is None or record["recorded_at"] >= current["recorded_at"]:
            latest_results[bind_email] = record

    current_bind = (config.get("bind_email") or "").strip().lower()
    entries = []
    for account in accounts:
        email = account.email
        key = email.lower()
        event = latest_events.get(key, {})
        result = latest_results.get(key)
        status = _classify_outlook_status(
            has_success_result=key in success_results,
            last_event_status=event.get("status", latest_statuses.get(key, "")),
            has_result=key in has_results,
        )
        sort_ts = _parse_timestamp(event.get("time", "")) or float((result or {}).get("recorded_at", 0.0))
        entries.append(
            {
                "email": email,
                "status": status,
                "status_label": _OUTLOOK_STATUS_LABELS[status],
                "last_event_time": event.get("time", ""),
                "last_event_status": event.get("status", latest_statuses.get(key, "")),
                "has_result": key in has_results,
                "result_ok": bool((result or {}).get("ok")),
                "phone": (result or {}).get("phone", ""),
                "sub2api_id": (result or {}).get("sub2api_id", ""),
                "bind_email": (result or {}).get("bind_email", ""),
                "last_result_time": (result or {}).get("time_label", ""),
                "can_assign": status not in ("bad", "success"),
                "can_mark_bad": True,
                "can_mark_verify_failed": True,
                "can_mark_reserved": True,
                "is_current_bind": key == current_bind,
                "_sort_ts": sort_ts,
            }
        )
    return entries


def _sanitize_outlook_entry(entry: dict) -> dict:
    clean = dict(entry or {})
    clean.pop("_sort_ts", None)
    return clean


def _outlook_list_sort_key(entry: dict):
    status = entry.get("status", "")
    bucket = 2
    if status == "unused":
        bucket = 0
    elif status == "reserved":
        bucket = 1
    return (
        bucket,
        -float(entry.get("_sort_ts", 0.0) or 0.0),
        str(entry.get("email", "")).lower(),
    )


def _find_outlook_pool_entry(email: str, entries: list[dict]) -> dict | None:
    target = (email or "").strip().lower()
    if not target:
        return None
    for entry in entries:
        if entry.get("email", "").lower() == target:
            return entry
    return None


@app.route("/api/outlook-pool/summary")
def api_outlook_pool_summary():
    try:
        entries = _build_outlook_pool_entries(_current_config())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    counts = {key: 0 for key in _OUTLOOK_SUMMARY_KEYS}
    for entry in entries:
        counts[entry["status"]] += 1
    cfg = _current_config()
    return jsonify(
        {
            "ok": True,
            "total": len(entries),
            "counts": counts,
            "current_bind_email": cfg.get("bind_email", ""),
            "email_provider": cfg.get("email_provider", ""),
        }
    )


@app.route("/api/outlook-pool/list")
def api_outlook_pool_list():
    status = (request.args.get("status", "all") or "all").strip().lower()
    query = (request.args.get("q", "") or "").strip().lower()
    page = _bounded_int(request.args.get("page"), default=1, minimum=1, maximum=9999)
    page_size = _bounded_int(request.args.get("page_size"), default=20, minimum=1, maximum=100)
    try:
        entries = _build_outlook_pool_entries(_current_config())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    rows = []
    for entry in entries:
        if status != "all" and entry["status"] != status:
            continue
        if query:
            haystack = "\n".join(
                [
                    entry.get("email", ""),
                    entry.get("phone", ""),
                    entry.get("bind_email", ""),
                    entry.get("sub2api_id", ""),
                ]
            ).lower()
            if query not in haystack:
                continue
        rows.append(entry)

    rows.sort(key=_outlook_list_sort_key)
    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    cfg = _current_config()
    return jsonify(
        {
            "ok": True,
            "items": [_sanitize_outlook_entry(entry) for entry in rows[start:end]],
            "total": total,
            "page": page,
            "page_size": page_size,
            "current_bind_email": cfg.get("bind_email", ""),
        }
    )


@app.route("/api/outlook-pool/detail")
def api_outlook_pool_detail():
    email = (request.args.get("email", "") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email is required"}), 400
    try:
        entries = _build_outlook_pool_entries(_current_config())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    entry = _find_outlook_pool_entry(email, entries)
    if not entry:
        return jsonify({"ok": False, "error": "email not found in outlook pool"}), 404
    cfg = _current_config()
    return jsonify(
        {
            "ok": True,
            "entry": _sanitize_outlook_entry(entry),
            "current_bind_email": cfg.get("bind_email", ""),
        }
    )


@app.route("/api/outlook-pool/messages")
def api_outlook_pool_messages():
    email = (request.args.get("email", "") or "").strip()
    if not email:
        return jsonify({"ok": False, "error": "email is required"}), 400
    cfg = _current_config()
    limit = _bounded_int(request.args.get("limit"), default=20, minimum=1, maximum=50)
    include_body = str(request.args.get("include_body", "1")).lower() not in ("0", "false", "no")
    try:
        account = get_outlook_account(email, _outlook_pool_source(cfg))
        client = OutlookMailClient(
            account,
            verbose=False,
            proxy=cfg.get("proxy", ""),
            prefer_imap=True,
        )
        items = client.list_recent_messages(limit=limit, include_body=include_body)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "email": account.email, "items": items})


@app.route("/api/outlook-pool/action", methods=["POST"])
def api_outlook_pool_action():
    data = request.json or {}
    action = (data.get("action", "") or "").strip()
    cfg = dict(_current_config())
    try:
        entries = _build_outlook_pool_entries(cfg)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if action == "mark_status":
        email = (data.get("email", "") or "").strip()
        status = (data.get("status", "") or "").strip().lower()
        if status not in ("reserved", "verify_failed", "bad"):
            return jsonify({"ok": False, "error": "invalid status"}), 400
        entry = _find_outlook_pool_entry(email, entries)
        if not entry:
            return jsonify({"ok": False, "error": "email not found in outlook pool"}), 404
        mark_outlook_status(entry["email"], status, _outlook_used_source(cfg))
        refreshed = _find_outlook_pool_entry(entry["email"], _build_outlook_pool_entries(cfg))
        return jsonify(
            {
                "ok": True,
                "action": action,
                "email": entry["email"],
                "entry": _sanitize_outlook_entry(refreshed),
                "current_bind_email": cfg.get("bind_email", ""),
            }
        )

    if action == "assign_for_run":
        email = (data.get("email", "") or "").strip()
        entry = _find_outlook_pool_entry(email, entries)
        if not entry:
            return jsonify({"ok": False, "error": "email not found in outlook pool"}), 404
        if not entry.get("can_assign"):
            return jsonify({"ok": False, "error": "selected email cannot be assigned"}), 400
        cfg["bind_email"] = entry["email"]
        cfg["email_provider"] = "outlook"
        _state["config"] = cfg
        _save_config_file(cfg)
        mark_outlook_status(entry["email"], "reserved", _outlook_used_source(cfg))
        refreshed = _find_outlook_pool_entry(entry["email"], _build_outlook_pool_entries(cfg))
        return jsonify(
            {
                "ok": True,
                "action": action,
                "email": entry["email"],
                "entry": _sanitize_outlook_entry(refreshed),
                "current_bind_email": cfg.get("bind_email", ""),
            }
        )

    if action == "reserve_next_unused":
        target = None
        for entry in sorted(entries, key=_outlook_list_sort_key):
            if entry["status"] == "unused":
                target = entry
                break
        if not target:
            return jsonify({"ok": False, "error": "no unused outlook account available"}), 400
        cfg["bind_email"] = target["email"]
        cfg["email_provider"] = "outlook"
        _state["config"] = cfg
        _save_config_file(cfg)
        mark_outlook_status(target["email"], "reserved", _outlook_used_source(cfg))
        refreshed = _find_outlook_pool_entry(target["email"], _build_outlook_pool_entries(cfg))
        return jsonify(
            {
                "ok": True,
                "action": action,
                "email": target["email"],
                "entry": _sanitize_outlook_entry(refreshed),
                "current_bind_email": cfg.get("bind_email", ""),
            }
        )

    return jsonify({"ok": False, "error": "unsupported action"}), 400

def _phase2_for_result(result: dict, config: dict, thread_tag: str = "", thread_id=None) -> dict:
    """瀵瑰崟涓凡娉ㄥ唽璐﹀彿鎵ц Phase 2 (OAuth + 缁戦偖绠?+ 涓婁紶 SUB2API)"""
    import requests as _r, urllib.parse as _up
    sub = config.get("sub2api", {})
    mm_config = config.get("mailmanage", {})
    tlog = lambda msg, tag="info": _log(msg, tag, thread_id=thread_id)

    _log(f"  [1/4] 鐧诲綍 SUB2API ...", "info")
    r = _r.post(f"{sub['url']}/api/v1/auth/login",
        json={"email": sub["email"], "password": sub.get("pwd", "")}, timeout=15)
    login_data = r.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API鐧诲綍澶辫触: {login_data.get('message','?')}")
    admin_token = login_data["data"]["access_token"]

    _log(f"  [2/4] 鑾峰彇 OAuth URL ...", "info")
    r = _r.post(f"{sub['url']}/api/v1/admin/openai/generate-auth-url",
        json={"redirect_uri": "http://localhost:1455/auth/callback"},
        headers={"Authorization": f"Bearer {admin_token}"}, timeout=60)
    oauth_data = r.json()
    if oauth_data.get("code") != 0:
        raise RuntimeError(f"鑾峰彇OAuth URL澶辫触: {oauth_data.get('message','?')}")
    oauth_url = oauth_data["data"]["auth_url"]
    session_id = oauth_data["data"]["session_id"]
    oauth_state = _up.parse_qs(_up.urlparse(oauth_url).query).get("state", [""])[0]

    group_id = 1
    group_name = config.get("sub2api", {}).get("group", "CHATGPT")
    try:
        r = _r.get(f"{sub['url']}/api/v1/admin/groups",
            headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
        groups = r.json().get("data", {}).get("items", [])
        for g in groups:
            if g.get("name") == group_name:
                group_id = g.get("id", 1)
                _log(f"  [2/4] 鍒嗙粍: {group_name} (ID={group_id})", "info")
                break
        else:
            _log(f"  [2/4] 鏈壘鍒板垎缁?{group_name}, 浣跨敤 ID=1", "warn")
    except Exception as e:
        _log(f"  [2/4] 鍒嗙粍鏌ヨ澶辫触: {e}, 浣跨敤 ID=1", "warn")

    bind_email = config.get("bind_email", "")
    if not bind_email:
        raise RuntimeError("bind_email is not configured")

    _log(f"  [3/4] OAuth娴佺▼: 鐧诲綍->缁戦偖绠?>楠岃瘉->鍚屾剰->code ...", "info")
    from openai_bind_email import run_second_half

    def _wait_for_web_code(hint: str) -> str:
        _log(f"  {thread_tag} [?] 绛夊緟杈撳叆楠岃瘉鐮? {hint}", "warn")
        tid = thread_tag or "main"
        tq = queue.Queue()
        _state.setdefault("_code_queues", {})[tid] = tq
        _state.setdefault("_code_waiting", {})[tid] = hint
        try:
            return tq.get(timeout=120)
        except queue.Empty:
            _log(f"  {thread_tag} [?] verification code input timed out", "error")
            return ""
        finally:
            _state.get("_code_waiting", {}).pop(tid, None)
            _state.get("_code_queues", {}).pop(tid, None)

    icloud_cookies = _load_phase2_icloud_cookies(config)

    result2 = run_second_half(
        oauth_url=oauth_url,
        phone=result["phone"],
        password=result["password"],
        icloud_email=bind_email or "",
        icloud_cookies=icloud_cookies,
        sub2api_url=sub["url"],
        sub2api_email=sub["email"],
        sub2api_password=sub.get("pwd", ""),
        proxy=config.get("proxy", ""),
        verbose=True,
        sub2api_session_id=session_id,
        sub2api_state=oauth_state,
        outlook_pool=config.get("outlook_pool", ""),
        tempmail_config=config.get("tempmail", {}) if config.get("email_provider") == "tempmail" else None,
        sub2api_proxy_id=int(config.get("sub2api", {}).get("proxy_id", 0) or 0),
    )
    return result2

@app.route("/api/batch-phase2", methods=["POST"])
def api_batch_phase2():
    if _state["running"]:
        return jsonify({"ok": False, "error": "task already running"})
    d = request.json or {}
    source = d.get("source", "files")
    files = d.get("files", [])
    if not files:
        return jsonify({"ok": False, "error": "鏈€夋嫨鏂囦欢"})
    email = d.get("email", "").strip()
    concurrency = max(1, min(int(d.get("concurrency", 1)), 10))
    _state["stop"] = False
    cfg = _state["config"]
    threading.Thread(target=_run_batch_phase2, args=(files, cfg, email, source, concurrency), daemon=True).start()
    return jsonify({"ok": True})

def _run_batch_phase2(files: list, config: dict, email: str = "", source: str = "files", concurrency: int = 1):
    """Run Phase 2 for existing results."""
    if email:
        config["bind_email"] = email
        _log(f"[琛ヨ窇] 浣跨敤鎸囧畾閭: {email}", "info")

    email_provider = config.get("email_provider", "")
    mm_config = config.get("mailmanage", {})
    mm = None
    ic = None

    if email_provider == "mailmanage" and mm_config.get("api_key") and not email:
        try:
            from mailmanage_client import MailManageClient
            mm = MailManageClient(
                api_key=mm_config["api_key"],
                base_url=mm_config.get("base_url", ""),
                verbose=False,
            )
            _log("[batch] MailManage client initialized", "info")
        except Exception as e:
            _log(f"[琛ヨ窇] MailManage 鍒濆鍖栧け璐? {e}", "error")
    elif not email:
        c = _load_phase2_icloud_cookies(config)
        if c:
            try:
                from icloud_hme import ICloudHME
                ic = ICloudHME(c, verbose=False)
                _log("[batch] iCloud HME initialized", "info")
            except Exception as e:
                _log(f"[琛ヨ窇] iCloud 鍒濆鍖栧け璐? {e}", "error")

    _state["running"] = True
    _state["_phase2_retry"] = False
    old_stdout = sys.stdout
    sys.stdout = _LogWriter(_log)
    results_dir = Path(__file__).parent / "results"

    sub = config.get("sub2api", {})
    if not sub.get("url") or not sub.get("email"):
        _log("[batch] please configure SUB2API url and email first", "error")
        _state["running"] = False
        sys.stdout = old_stdout
        return

    is_multi = concurrency > 1
    if is_multi:
        _log(f"[琛ヨ窇] 寮€濮嬫壒閲?Phase 2, 鍏?{len(files)} 涓处鍙? 骞惰 {concurrency} 绾跨▼", "info")
    else:
        _log(f"[batch] start Phase 2 for {len(files)} items", "info")

    all_data = None
    if source == "all":
        all_path = results_dir / "_all.json"
        if all_path.exists():
            try:
                all_data = json.loads(all_path.read_text(encoding="utf-8"))
            except Exception as e:
                _log(f"[琛ヨ窇] 璇诲彇 _all.json 澶辫触: {e}", "error")

    _lock = threading.Lock()
    _counter = {"ok": 0, "fail": 0}
    _file_q = queue.Queue()
    for f in files:
        _file_q.put(f)

    def _batch_worker(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        while not _state["stop"]:
            try:
                fname = _file_q.get_nowait()
            except queue.Empty:
                return

            if source == "all":
                try:
                    idx = int(fname)
                    if all_data is None or idx >= len(all_data):
                        _log(f"[琛ヨ窇] {tag} 绱㈠紩 {idx} 瓒呭嚭鑼冨洿", "error")
                        continue
                    result = all_data[idx]
                except (ValueError, IndexError, TypeError) as e:
                    _log(f"[琛ヨ窇] {tag} 鏃犳晥绱㈠紩: {fname} ({e})", "error")
                    continue
            else:
                fpath = results_dir / fname
                if not fpath.exists():
                    _log(f"[琛ヨ窇] {tag} 鏂囦欢涓嶅瓨鍦? {fname}", "error")
                    continue
                try:
                    result = json.loads(fpath.read_text(encoding="utf-8"))
                except Exception as e:
                    _log(f"[琛ヨ窇] {tag} 璇诲彇澶辫触: {fname} ({e})", "error")
                    continue

            if not result.get("ok"):
                _log(f"[琛ヨ窇] {tag} 璺宠繃澶辫触璁板綍: {result.get('phone','?')}", "warn")
                continue

            if result.get("sub2api_id"):
                _log(f"[琛ヨ窇] {tag} 璺宠繃宸蹭笂浼? {result.get('phone','?')}", "info")
                continue

            phone = result.get("phone", "?")
            used_email = email or ""

            if not used_email and mm is not None:
                try:
                    used_email = mm.get_available_email(category=mm_config.get("category", "free"))
                    _log(f"[琛ヨ窇] {tag} [{phone}] MailManage 鍙栧彿: {used_email}", "info")
                except Exception as e:
                    _log(f"[琛ヨ窇] {tag} [{phone}] MailManage 鍙栧彿澶辫触: {e}", "error")
            elif not used_email and ic is not None:
                try:
                    used_email = ic.reuse_or_create_alias()
                    _log(f"[琛ヨ窇] {tag} [{phone}] iCloud 鍒悕: {used_email}", "info")
                except Exception as e:
                    _log(f"[琛ヨ窇] {tag} [{phone}] iCloud 鍒涘缓澶辫触: {e}", "error")
            elif not used_email:
                _log(f"[琛ヨ窇] {tag} [{phone}] 鏃犲彲鐢ㄧ殑閭鎻愪緵鍟? 璺宠繃", "error")
                with _lock:
                    _counter["fail"] += 1
                continue

            thread_cfg = copy.deepcopy(config)
            thread_cfg["bind_email"] = used_email
            _log(f"[琛ヨ窇] {tag} [{phone}] 寮€濮?Phase 2 (閭: {used_email}) ...", "info")

            try:
                oauth_result = _phase2_for_result(result, thread_cfg, tag)
            except Exception as e:
                oauth_result = {"ok": False, "error": str(e)}

            if oauth_result.get("ok"):
                with _lock:
                    _counter["ok"] += 1
                result["sub2api_id"] = oauth_result.get("sub2api_account_id", "")
                result["bind_email"] = used_email
                with _lock:
                    if source == "all" and all_data is not None:
                        idx = int(fname)
                        all_data[idx] = result
                        (results_dir / "_all.json").write_text(
                            json.dumps(all_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    elif source == "files":
                        fpath.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                _log(f"[琛ヨ窇] {tag} [{phone}] 鎴愬姛! sub2api_id={result.get('sub2api_id','?')}", "success")
                if mm is not None and used_email:
                    try:
                        mm.mark_used(used_email)
                        _log(f"[琛ヨ窇] {tag} [{phone}] MailManage 宸叉爣璁? {used_email}", "info")
                    except Exception as e:
                        _log(f"[琛ヨ窇] {tag} [{phone}] 鏍囪澶辫触: {e}", "warn")
            else:
                with _lock:
                    _counter["fail"] += 1
                _log(f"[琛ヨ窇] {tag} [{phone}] 澶辫触: {oauth_result.get('error','?')}", "error")

    try:
        threads = []
        for i in range(concurrency):
            t = threading.Thread(target=_batch_worker, args=(i + 1,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    finally:
        sys.stdout = old_stdout

    ok_count = _counter["ok"]
    fail_count = _counter["fail"]
    _state["running"] = False
    _log(f"[琛ヨ窇] 瀹屾垚: {ok_count}鎴愬姛 / {fail_count}澶辫触", "success" if ok_count > 0 else "warn")

# ---- Registration runner ----
class _LogWriter:
    def __init__(self, log_fn):
        self._log = log_fn
        self._buf = ""
    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            idx = self._buf.index("\n")
            line = self._buf[:idx]
            self._buf = self._buf[idx+1:]
            line = line.strip()
            if line:
                self._log(line, "info")
    def flush(self):
        if self._buf.strip():
            self._log(self._buf.strip(), "info")
            self._buf = ""

def _run(config, count, retries, concurrency=1):
    old_stdout = sys.stdout
    sys.stdout = _LogWriter(_log)
    _state["_phase2_retry"] = False

    key = config.get("smsbower", {}).get("api_key", "")
    sms = SmsBower(key)
    try:
        _log(f"余额: {sms.balance()}", "info")
    except: pass

    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    is_multi = concurrency > 1
    if is_multi:
        _log(f"寮€濮嬫敞鍐? 鐩爣{count}涓? 姝ラ閲嶈瘯{retries}娆?姝? 骞惰{concurrency}绾跨▼", "success")
    else:
        _log(f"start registration: target={count} retries={retries}", "success")

    sub = config.get("sub2api", {})
    bind_email = config.get("bind_email", "")
    email_provider = config.get("email_provider", "")
    mm_config = config.get("mailmanage", {})
    mm = None
    debug_mode = config.get("debug_mode", False) and not is_multi

    if email_provider == "mailmanage" and mm_config.get("api_key"):
        try:
            from mailmanage_client import MailManageClient
            mm = MailManageClient(
                api_key=mm_config["api_key"],
                base_url=mm_config.get("base_url", ""),
                verbose=False,
            )
        except Exception as e:
            _log(f"MailManage 鍒濆鍖栧け璐? {e}", "error")

    ic = None
    if not bind_email and mm is None and not config.get("no_phase2") and sub.get("url"):
        try:
            c = _load_phase2_icloud_cookies(config)
            _log(f"iCloud cookies: {'loaded' if c else 'missing'}", "info")
            if c:
                from icloud_hme import ICloudHME
                ic = ICloudHME(c, verbose=False)
        except Exception as e:
            _log(f"iCloud鍒濆鍖栧け璐? {e}", "error")

    max_attempts = count * 15
    condition = threading.Condition()
    counters = {"ok": 0, "attempt": 0, "active": 0}
    _log(f"start run: target={count} concurrency={concurrency} retries={retries}", "success")

    def claim_attempt():
        with condition:
            while True:
                if _state["stop"] or counters["ok"] >= count or counters["attempt"] >= max_attempts:
                    return None
                if counters["ok"] + counters["active"] < count:
                    counters["attempt"] += 1
                    counters["active"] += 1
                    return counters["attempt"], counters["ok"]
                condition.wait(timeout=0.5)

    def finish_registration(success):
        with condition:
            counters["active"] = max(0, counters["active"] - 1)
            if success:
                counters["ok"] += 1
            condition.notify_all()

    def _worker(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        thread_sms = SmsBower(key)
        while True:
            claimed = claim_attempt()
            if not claimed:
                return
            attempt_num, ok_so_far = claimed
            _log(f"{tag} attempt {attempt_num} [{ok_so_far}/{count}]", "info", thread_id=thread_id)
            thread_cfg = copy.deepcopy(config)
            try:
                result = ar.register_one(thread_sms, thread_cfg, verbose=True, step_retries=retries,
                                         create_account_max_retries=20,
                                         max_price=config.get("max_price", ""),
                                         provider_ids=config.get("provider", ""),
                                         stop_requested=_stop_requested)
            except ar.StopRequested:
                _log(f"{tag} 宸插仠姝㈢瓑寰呮墜鏈哄彿", "warn", thread_id=thread_id)
                finish_registration(False)
                return
            except Exception as e:
                result = {"ok": False, "phone": "?", "error": str(e)}
            if not result.get("ok") and thread_sms.activation_id:
                try:
                    thread_sms.cancel()
                except Exception:
                    pass
            _state["results"].append(result)

            if not result["ok"]:
                _log(f"{tag} 澶辫触: {result.get('phone','?')} {result.get('error','')}", "error")
                finish_registration(False)
                continue

            finish_registration(True)
            ok_num = counters["ok"]
            sys.stdout.flush()
            _log(f"{tag} 娉ㄥ唽鎴愬姛: {result['phone']} [{ok_num}/{count}]", "success", thread_id=thread_id)

            # ---- Debug mode: pause (single-thread only) ----
            if debug_mode:
                _log("=" * 40, "warn")
                _log(f"DEBUG - 娉ㄥ唽瀹屾垚锛屽凡鏆傚仠", "warn")
                _log(f"Phone: {result['phone']}", "success")
                _log(f"Password: {result['password']}", "success")
                _log(f"Session Token: {result.get('session_token','')}", "success")
                _log("=" * 40, "warn")
                _state["_paused"] = True
                try:
                    _state["pause_queue"].get(timeout=300)
                except queue.Empty:
                    _log("DEBUG - timed out, auto continue", "warn")
                _state["_paused"] = False

            # Phase 2
            phase2_ok = True
            if not config.get("no_phase2") and sub.get("url") and sub.get("email") and result.get("session_token"):
                if not bind_email:
                    new_email = ""
                    if mm is not None:
                        try:
                            new_email = mm.get_available_email(category=mm_config.get("category", "free"))
                            _log(f"{tag} MailManage 閫夊畾: {new_email}", "success")
                        except Exception as e:
                            _log(f"{tag} MailManage 鑾峰彇閭澶辫触: {e}", "error")
                    elif config.get("mail_provider") == "outlook":
                        try:
                            from outlook_mail import reserve_next_outlook
                            outlook_account = reserve_next_outlook(
                                config.get("outlook_pool") or "outlook.txt",
                                config.get("outlook_used") or "outlook_used.txt",
                            )
                            new_email = outlook_account.email
                            _log(f"{tag} Outlook閭: {new_email}", "success")
                        except Exception as e:
                            _log(f"{tag} Outlook鑾峰彇閭澶辫触: {e}", "error")
                    elif ic is not None:
                        try:
                            new_email = ic.reuse_or_create_alias()
                            _log(f"{tag} 鏂癷Cloud鍒悕: {new_email}", "success")
                        except Exception as e:
                            _log(f"{tag} iCloud鍒涘缓鍒悕澶辫触: {e}", "error")
                    if new_email:
                        thread_cfg["bind_email"] = new_email
                    elif not thread_cfg.get("bind_email"):
                        _log(f"{tag} 鏃犲彲鐢ㄧ殑閭鎻愪緵鍟? 璺宠繃Phase2", "error")
                        _save_result(results_dir, result, thread_cfg)
                        continue

                _log(f"{tag} === Phase 2: OAuth + 缁戦偖绠?+ 涓婁紶 (閭: {thread_cfg.get('bind_email','?')}) ===", "info")
                phase2_ok = False
                while not phase2_ok and not _state["stop"]:

                    def _do_phase2_once():
                        import requests as _r, urllib.parse as _up
                        _log(f"{tag}   [1/4] 鐧诲綍 SUB2API ...", "info")
                        r = _r.post(f"{sub['url']}/api/v1/auth/login",
                            json={"email": sub["email"], "password": sub.get("pwd", "")}, timeout=15)
                        login_data = r.json()
                        if login_data.get("code") != 0:
                            raise RuntimeError(f"SUB2API鐧诲綍澶辫触: {login_data.get('message','?')}")
                        admin_token = login_data["data"]["access_token"]

                        _log(f"{tag}   [2/4] 鑾峰彇 OAuth URL ...", "info")
                        r = _r.post(f"{sub['url']}/api/v1/admin/openai/generate-auth-url",
                            json={"redirect_uri": "http://localhost:1455/auth/callback"},
                            headers={"Authorization": f"Bearer {admin_token}"}, timeout=60)
                        oauth_data = r.json()
                        if oauth_data.get("code") != 0:
                            raise RuntimeError(f"鑾峰彇OAuth URL澶辫触: {oauth_data.get('message','?')}")
                        oauth_url = oauth_data["data"]["auth_url"]
                        session_id = oauth_data["data"]["session_id"]
                        oauth_state = _up.parse_qs(_up.urlparse(oauth_url).query).get("state", [""])[0]

                        group_id = 1
                        group_name = thread_cfg.get("sub2api", {}).get("group", "CHATGPT")
                        try:
                            r = _r.get(f"{sub['url']}/api/v1/admin/groups",
                                headers={"Authorization": f"Bearer {admin_token}"}, timeout=15)
                            groups = r.json().get("data", {}).get("items", [])
                            for g in groups:
                                if g.get("name") == group_name:
                                    group_id = g.get("id", 1)
                                    break
                        except Exception:
                            pass

                        _log(f"{tag}   [3/4] OAuth娴佺▼ ...", "info")
                        from openai_bind_email import run_second_half

                        def _wait_for_web_code(hint: str) -> str:
                            _log(f"{tag}   [?] 绛夊緟杈撳叆楠岃瘉鐮? {hint}", "warn")
                            tid = tag or "main"
                            tq = queue.Queue()
                            _state.setdefault("_code_queues", {})[tid] = tq
                            _state.setdefault("_code_waiting", {})[tid] = hint
                            try:
                                return tq.get(timeout=120)
                            except queue.Empty:
                                _log(f"{tag}   [?] verification code input timed out", "error")
                                return ""
                            finally:
                                _state.get("_code_waiting", {}).pop(tid, None)
                                _state.get("_code_queues", {}).pop(tid, None)

                        icloud_cookies = _load_phase2_icloud_cookies(thread_cfg)

                        result2 = run_second_half(
                            oauth_url=oauth_url,
                            phone=result["phone"],
                            password=result["password"],
                            icloud_email=thread_cfg.get("bind_email", "") or "",
                            icloud_cookies=icloud_cookies,
                            sub2api_url=sub["url"],
                            sub2api_email=sub["email"],
                            sub2api_password=sub.get("pwd", ""),
                            proxy=thread_cfg.get("proxy", ""),
                            verbose=True,
                            sub2api_session_id=session_id,
                            sub2api_state=oauth_state,
                            outlook_pool=thread_cfg.get("outlook_pool", ""),
                            sub2api_proxy_id=int(thread_cfg.get("sub2api", {}).get("proxy_id", 0) or 0),
                        )
                        return result2

                    try:
                        oauth_result = _do_phase2_once()
                    except Exception as e:
                        oauth_result = {"ok": False, "error": str(e)}

                    if oauth_result.get("ok"):
                        phase2_ok = True
                        aid = oauth_result.get("sub2api_account_id", "?")
                        _log(f"{tag}   [4/4] 涓婁紶鎴愬姛! SUB2API id={aid}", "success")
                        result["sub2api_id"] = aid
                    else:
                        _log(f"{tag}   [4/4] Phase2澶辫触: {oauth_result.get('error','?')}", "warn")
                        if is_multi or config.get("phase2_auto_skip"):
                            _log(f"{tag}   鑷姩璺宠繃Phase2", "warn")
                            break
                        _state["_paused"] = True
                        _state["_phase2_retry"] = True
                        _log("  [?] 鐐?閲嶈瘯'閲嶆柊璧癙hase2, 鐐?璺宠繃'鏀惧純涓婁紶", "warn")
                        try:
                            action = _state["pause_queue"].get(timeout=600)
                            if action == "skip":
                                _log("  [?] 鐢ㄦ埛閫夋嫨璺宠繃Phase2", "warn")
                                break
                        except queue.Empty:
                            _log("  [?] 瓒呮椂, 璺宠繃Phase2", "warn")
                            break
                        finally:
                            _state["_paused"] = False

            _save_result(results_dir, result, thread_cfg)
            if mm is not None and thread_cfg.get("bind_email") and phase2_ok:
                try:
                    mm.mark_used(thread_cfg["bind_email"])
                    _log(f"{tag} MailManage 宸叉爣璁? {thread_cfg['bind_email']}", "info")
                except: pass

    try:
        threads = []
        for i in range(concurrency):
            t = threading.Thread(target=_worker, args=(i + 1,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    finally:
        sys.stdout = old_stdout

    ok_count = counters["ok"]
    _state["running"] = False
    tag = "success" if ok_count >= count else "warn"
    _log(f"瀹屾垚: {ok_count}/{count}", tag)

# ---- Helpers ----
def _save_config_file(cfg: dict):
    path = Path(__file__).parent / "config.json"
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def _save_result(results_dir: Path, result: dict, config: dict):
    if not result.get("ok"):
        return
    safe = dict(result)
    safe["bind_email"] = config.get("bind_email", "")
    ts = time.strftime("%Y%m%d_%H%M%S")
    phone = result.get("phone", "unknown").replace("+", "")
    path = results_dir / f"{phone}_{ts}.json"
    path.write_text(json.dumps(safe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    all_path = results_dir / "_all.json"
    all_results = []
    if all_path.exists():
        try:
            all_results = json.loads(all_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    all_results.append(safe)
    all_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def _sanitize_config(cfg):
    return copy.deepcopy(cfg)

def _sanitize_result(r):
    r2 = dict(r)
    for k in ["session_token", "access_token"]:
        if r2.get(k): r2[k] = r2[k][:30] + "..."
    return r2

class _LogWriter:
    def __init__(self, log_fn):
        self._log = log_fn
        self._lock = threading.RLock()
        self._buffers = {}
        self._thread_map = {}

    def bind_thread(self, thread_id):
        ident = threading.get_ident()
        with self._lock:
            self._thread_map[ident] = thread_id
            self._buffers.setdefault(ident, "")

    def unbind_thread(self):
        ident = threading.get_ident()
        self.flush()
        with self._lock:
            self._thread_map.pop(ident, None)
            self._buffers.pop(ident, None)

    def _state_for_current_thread(self):
        ident = threading.get_ident()
        with self._lock:
            self._buffers.setdefault(ident, "")
            return ident, self._buffers[ident], self._thread_map.get(ident)

    def write(self, s):
        if not s:
            return 0
        ident, buf, thread_id = self._state_for_current_thread()
        buf += str(s)
        lines = []
        while "\n" in buf:
            idx = buf.index("\n")
            line = buf[:idx].strip()
            buf = buf[idx + 1:]
            if line:
                lines.append(line)
        with self._lock:
            self._buffers[ident] = buf
        for line in lines:
            self._log(line, "info", thread_id=thread_id)
        return len(str(s))

    def flush(self):
        ident, buf, thread_id = self._state_for_current_thread()
        line = buf.strip()
        if line:
            self._log(line, "info", thread_id=thread_id)
        with self._lock:
            self._buffers[ident] = ""


def _phase2_for_result(result: dict, config: dict, thread_tag: str = "", thread_id=None) -> dict:
    import requests as _r
    import urllib.parse as _up

    sub = config.get("sub2api", {})
    tlog = lambda msg, tag="info": _log(msg, tag, thread_id=thread_id)

    tlog(f"{thread_tag} [1/4] 登录 SUB2API ...".strip(), "info")
    login_resp = _r.post(
        f"{sub['url']}/api/v1/auth/login",
        json={"email": sub["email"], "password": sub.get("pwd", "")},
        timeout=15,
    )
    login_data = login_resp.json()
    if login_data.get("code") != 0:
        raise RuntimeError(f"SUB2API 登录失败: {login_data.get('message', '?')}")
    admin_token = login_data["data"]["access_token"]

    tlog(f"{thread_tag} [2/4] 获取 OAuth URL ...".strip(), "info")
    auth_resp = _r.post(
        f"{sub['url']}/api/v1/admin/openai/generate-auth-url",
        json={"redirect_uri": "http://localhost:1455/auth/callback"},
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=60,
    )
    auth_data = auth_resp.json()
    if auth_data.get("code") != 0:
        raise RuntimeError(f"获取 OAuth URL 失败: {auth_data.get('message', '?')}")
    oauth_url = auth_data["data"]["auth_url"]
    session_id = auth_data["data"]["session_id"]
    oauth_state = _up.parse_qs(_up.urlparse(oauth_url).query).get("state", [""])[0]

    group_name = config.get("sub2api", {}).get("group", "CHATGPT")
    try:
        group_resp = _r.get(
            f"{sub['url']}/api/v1/admin/groups",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=15,
        )
        groups = group_resp.json().get("data", {}).get("items", [])
        for group in groups:
            if group.get("name") == group_name:
                tlog(f"{thread_tag} [2/4] 分组: {group_name} (ID={group.get('id', 1)})".strip(), "info")
                break
        else:
            tlog(f"{thread_tag} [2/4] 未找到分组 {group_name}, 使用默认分组".strip(), "warn")
    except Exception as e:
        tlog(f"{thread_tag} [2/4] 查询分组失败: {e}".strip(), "warn")

    bind_email = (config.get("bind_email") or "").strip()
    if not bind_email:
        raise RuntimeError("bind_email is not configured")

    tlog(f"{thread_tag} [3/4] OAuth 流程: 登录 -> 绑定邮箱 -> 验证 -> 同意 -> code ...".strip(), "info")
    from openai_bind_email import run_second_half

    icloud_cookies = _load_phase2_icloud_cookies(config)
    return run_second_half(
        oauth_url=oauth_url,
        phone=result["phone"],
        password=result["password"],
        icloud_email=bind_email,
        icloud_cookies=icloud_cookies,
        sub2api_url=sub["url"],
        sub2api_email=sub["email"],
        sub2api_password=sub.get("pwd", ""),
        proxy=config.get("proxy", ""),
        verbose=True,
        sub2api_session_id=session_id,
        sub2api_state=oauth_state,
        outlook_pool=config.get("outlook_pool", ""),
        tempmail_config=config.get("tempmail", {}) if config.get("email_provider") == "tempmail" else None,
        sub2api_proxy_id=int(config.get("sub2api", {}).get("proxy_id", 0) or 0),
    )


def _run_batch_phase2(files: list, config: dict, email: str = "", source: str = "files", concurrency: int = 1):
    run_config = copy.deepcopy(config)
    if email:
        run_config["bind_email"] = email
        _log(f"[琛ヨ窇] 浣跨敤鎸囧畾閭: {email}", "info")

    email_provider = run_config.get("email_provider", "")
    mm_config = run_config.get("mailmanage", {})
    tm_config = run_config.get("tempmail", {})
    mm = None
    tm = None
    ic = None
    provider_lock = threading.Lock()
    use_outlook = run_config.get("mail_provider") == "outlook" or email_provider == "outlook"
    use_tempmail = email_provider == "tempmail"

    if email_provider == "mailmanage" and mm_config.get("api_key") and not email:
        try:
            from mailmanage_client import MailManageClient
            mm = MailManageClient(
                api_key=mm_config["api_key"],
                base_url=mm_config.get("base_url", ""),
                verbose=False,
            )
            _log("[batch] MailManage client initialized", "info")
        except Exception as e:
            _log(f"[琛ヨ窇] MailManage 鍒濆鍖栧け璐? {e}", "error")
    elif email_provider == "tempmail" and not email:
        try:
            from tempmail_client import TempMailClient
            tm = TempMailClient(
                base_url=tm_config.get("base_url", ""),
                jwt=tm_config.get("jwt", ""),
                site_password=tm_config.get("site_password", ""),
                admin_password=tm_config.get("admin_password", ""),
                domain=tm_config.get("domain", ""),
                name_prefix=tm_config.get("name_prefix", ""),
                pool=tm_config.get("pool", ""),
                verbose=False,
            )
            _log("[batch] TempMail client initialized", "info")
        except Exception as e:
            _log(f"[补跑] TempMail 初始化失败: {e}", "error")
    elif not email and not use_outlook and not use_tempmail:
        cookies = _load_phase2_icloud_cookies(run_config)
        if cookies:
            try:
                from icloud_hme import ICloudHME
                ic = ICloudHME(cookies, verbose=False)
                _log("[batch] iCloud HME initialized", "info")
            except Exception as e:
                _log(f"[琛ヨ窇] iCloud 鍒濆鍖栧け璐? {e}", "error")

    with _STATE_LOCK:
        _state["running"] = True
        _state["_phase2_retry"] = False

    old_stdout = sys.stdout
    log_writer = _LogWriter(_log)
    sys.stdout = log_writer
    results_dir = Path(__file__).parent / "results"

    sub = run_config.get("sub2api", {})
    if not sub.get("url") or not sub.get("email"):
        _log("[batch] please configure SUB2API url and email first", "error")
        with _STATE_LOCK:
            _state["running"] = False
        sys.stdout = old_stdout
        return

    is_multi = concurrency > 1
    summary = f"[batch] start Phase 2 for {len(files)} items"
    if is_multi:
        summary += f", 骞跺彂 {concurrency} 绾跨▼"
    _log(summary, "info")

    all_data = None
    if source == "all":
        all_path = results_dir / "_all.json"
        if all_path.exists():
            try:
                all_data = json.loads(all_path.read_text(encoding="utf-8"))
            except Exception as e:
                _log(f"[琛ヨ窇] 璇诲彇 _all.json 澶辫触: {e}", "error")

    counters = {"ok": 0, "fail": 0}
    counter_lock = threading.Lock()
    file_queue = queue.Queue()
    for item in files:
        file_queue.put(item)

    def _reserve_email(thread_id, phone):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        if email:
            return email
        if mm is not None:
            try:
                with provider_lock:
                    picked = mm.get_available_email(category=mm_config.get("category", "free"))
                tlog(f"[琛ヨ窇] {tag} [{phone}] MailManage 鍙栧彿: {picked}", "info")
                return picked
            except Exception as e:
                tlog(f"[琛ヨ窇] {tag} [{phone}] MailManage 鍙栧彿澶辫触: {e}", "error")
                return ""
        if tm is not None:
            try:
                with provider_lock:
                    account = tm.create_address()
                tlog(f"[补跑] {tag} [{phone}] TempMail 创建邮箱: {account.email}", "info")
                return account
            except Exception as e:
                tlog(f"[补跑] {tag} [{phone}] TempMail 创建邮箱失败: {e}", "error")
                return ""
        if use_outlook:
            try:
                from outlook_mail import reserve_next_outlook

                with provider_lock:
                    outlook_account = reserve_next_outlook(
                        run_config.get("outlook_pool") or "outlook.txt",
                        run_config.get("outlook_used") or "outlook_used.txt",
                    )
                tlog(f"[鐞涖儴绐嘳 {tag} [{phone}] Outlook 闁喚顔? {outlook_account.email}", "info")
                return outlook_account.email
            except Exception as e:
                tlog(f"[鐞涖儴绐嘳 {tag} [{phone}] Outlook 閸欐牕褰挎径杈Е: {e}", "error")
                return ""
        if ic is not None:
            try:
                with provider_lock:
                    picked = ic.reuse_or_create_alias()
                tlog(f"[琛ヨ窇] {tag} [{phone}] iCloud 鍒悕: {picked}", "info")
                return picked
            except Exception as e:
                tlog(f"[琛ヨ窇] {tag} [{phone}] iCloud 鍙栧彿澶辫触: {e}", "error")
                return ""
        return ""

    def _batch_worker(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        log_writer.bind_thread(thread_id)
        try:
            while not _state["stop"]:
                try:
                    fname = file_queue.get_nowait()
                except queue.Empty:
                    return

                fpath = None
                if source == "all":
                    try:
                        idx = int(fname)
                        if all_data is None or idx >= len(all_data):
                            tlog(f"[琛ヨ窇] {tag} 绱㈠紩瓒呭嚭鑼冨洿: {fname}", "error")
                            continue
                        result = dict(all_data[idx])
                    except (ValueError, IndexError, TypeError) as e:
                        tlog(f"[琛ヨ窇] {tag} 鏃犳晥绱㈠紩: {fname} ({e})", "error")
                        continue
                else:
                    fpath = results_dir / fname
                    if not fpath.exists():
                        tlog(f"[琛ヨ窇] {tag} 鏂囦欢涓嶅瓨鍦? {fname}", "error")
                        continue
                    try:
                        result = json.loads(fpath.read_text(encoding="utf-8"))
                    except Exception as e:
                        tlog(f"[琛ヨ窇] {tag} 璇诲彇澶辫触: {fname} ({e})", "error")
                        continue

                if not result.get("ok"):
                    tlog(f"[琛ヨ窇] {tag} 璺宠繃澶辫触璁板綍: {result.get('phone', '?')}", "warn")
                    continue
                if result.get("sub2api_id"):
                    tlog(f"[琛ヨ窇] {tag} 璺宠繃宸插畬鎴愯褰? {result.get('phone', '?')}", "info")
                    continue

                phone = result.get("phone", "?")
                reserved_email = _reserve_email(thread_id, phone)
                tempmail_account = reserved_email if hasattr(reserved_email, "jwt") else None
                used_email = tempmail_account.email if tempmail_account else reserved_email
                if not used_email:
                    tlog(f"[琛ヨ窇] {tag} [{phone}] 娌℃湁鍙敤閭, 璺宠繃", "error")
                    with counter_lock:
                        counters["fail"] += 1
                    continue

                thread_cfg = copy.deepcopy(run_config)
                thread_cfg["bind_email"] = used_email
                if tempmail_account:
                    thread_cfg["tempmail"] = dict(thread_cfg.get("tempmail", {}))
                    thread_cfg["tempmail"]["base_url"] = tempmail_account.base_url
                    thread_cfg["tempmail"]["jwt"] = tempmail_account.jwt
                    thread_cfg["tempmail"]["pool"] = ""
                    thread_cfg["tempmail"]["site_password"] = tempmail_account.site_password
                tlog(f"[琛ヨ窇] {tag} [{phone}] 寮€濮?Phase 2 (閭: {used_email}) ...", "info")

                try:
                    oauth_result = _phase2_for_result(result, thread_cfg, tag, thread_id=thread_id)
                except Exception as e:
                    oauth_result = {"ok": False, "error": str(e)}

                if oauth_result.get("ok"):
                    with counter_lock:
                        counters["ok"] += 1
                    result["sub2api_id"] = oauth_result.get("sub2api_account_id", "")
                    result["bind_email"] = used_email
                    with counter_lock:
                        if source == "all" and all_data is not None:
                            all_data[int(fname)] = result
                            (results_dir / "_all.json").write_text(
                                json.dumps(all_data, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8",
                            )
                        elif fpath is not None:
                            fpath.write_text(
                                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8",
                            )
                    tlog(f"[琛ヨ窇] {tag} [{phone}] 鎴愬姛, sub2api_id={result.get('sub2api_id', '?')}", "success")
                    if mm is not None:
                        try:
                            with provider_lock:
                                mm.mark_used(used_email)
                            tlog(f"[琛ヨ窇] {tag} [{phone}] MailManage 宸叉爣璁? {used_email}", "info")
                        except Exception as e:
                            tlog(f"[琛ヨ窇] {tag} [{phone}] 鏍囪澶辫触: {e}", "warn")
                    if tm is not None:
                        try:
                            with provider_lock:
                                tm.mark_used(used_email)
                            tlog(f"[补跑] {tag} [{phone}] TempMail 已标记: {used_email}", "info")
                        except Exception as e:
                            tlog(f"[补跑] {tag} [{phone}] TempMail 标记失败: {e}", "warn")
                else:
                    with counter_lock:
                        counters["fail"] += 1
                    tlog(f"[琛ヨ窇] {tag} [{phone}] 澶辫触: {oauth_result.get('error', '?')}", "error")
        finally:
            log_writer.unbind_thread()

    try:
        threads = []
        for i in range(concurrency):
            thread = threading.Thread(target=_batch_worker, args=(i + 1,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
    finally:
        sys.stdout = old_stdout

    with _STATE_LOCK:
        _state["running"] = False
    _log(
        f"[琛ヨ窇] 瀹屾垚: {counters['ok']} 鎴愬姛 / {counters['fail']} 澶辫触",
        "success" if counters["ok"] > 0 else "warn",
    )


def _run(config, count, retries, concurrency=1):
    run_config = copy.deepcopy(config)
    old_stdout = sys.stdout
    log_writer = _LogWriter(_log)
    sys.stdout = log_writer
    with _STATE_LOCK:
        _state["_phase2_retry"] = False

    key = run_config.get("smsbower", {}).get("api_key", "")
    sms = SmsBower(key)
    try:
        _log(f"余额: {sms.balance()}", "info")
    except Exception:
        pass

    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)
    is_multi = concurrency > 1
    start_msg = f"start registration: target={count} retries={retries}"
    if is_multi:
        start_msg += f", 并发 {concurrency} 线程"
    _log(start_msg, "success")

    sub = run_config.get("sub2api", {})
    bind_email = run_config.get("bind_email", "")
    email_provider = run_config.get("email_provider", "")
    mm_config = run_config.get("mailmanage", {})
    tm_config = run_config.get("tempmail", {})
    mm = None
    tm = None
    provider_lock = threading.Lock()
    debug_mode = run_config.get("debug_mode", False) and not is_multi
    use_outlook = run_config.get("mail_provider") == "outlook" or email_provider == "outlook"
    use_tempmail = email_provider == "tempmail"

    if email_provider == "mailmanage" and mm_config.get("api_key"):
        try:
            from mailmanage_client import MailManageClient
            mm = MailManageClient(
                api_key=mm_config["api_key"],
                base_url=mm_config.get("base_url", ""),
                verbose=False,
            )
        except Exception as e:
            _log(f"MailManage 初始化失败: {e}", "error")
    elif email_provider == "tempmail":
        try:
            from tempmail_client import TempMailClient
            tm = TempMailClient(
                base_url=tm_config.get("base_url", ""),
                jwt=tm_config.get("jwt", ""),
                site_password=tm_config.get("site_password", ""),
                admin_password=tm_config.get("admin_password", ""),
                domain=tm_config.get("domain", ""),
                name_prefix=tm_config.get("name_prefix", ""),
                pool=tm_config.get("pool", ""),
                verbose=False,
            )
        except Exception as e:
            _log(f"TempMail 初始化失败: {e}", "error")

    ic = None
    if not bind_email and mm is None and tm is None and not use_outlook and not use_tempmail and not run_config.get("no_phase2") and sub.get("url"):
        try:
            cookies = _load_phase2_icloud_cookies(run_config)
            _log(f"iCloud cookies: {'loaded' if cookies else 'missing'}", "info")
            if cookies:
                from icloud_hme import ICloudHME
                ic = ICloudHME(cookies, verbose=False)
        except Exception as e:
            _log(f"iCloud 初始化失败: {e}", "error")

    max_attempts = count * 15
    condition = threading.Condition()
    counters = {"ok": 0, "attempt": 0, "active": 0}

    def claim_attempt():
        with condition:
            while True:
                if _state["stop"] or counters["ok"] >= count or counters["attempt"] >= max_attempts:
                    return None
                if counters["ok"] + counters["active"] < count:
                    counters["attempt"] += 1
                    counters["active"] += 1
                    return counters["attempt"], counters["ok"]
                condition.wait(timeout=0.5)

    def finish_registration(success):
        with condition:
            counters["active"] = max(0, counters["active"] - 1)
            if success:
                counters["ok"] += 1
            condition.notify_all()

    def reserve_phase2_email(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        if mm is not None:
            try:
                with provider_lock:
                    email_value = mm.get_available_email(category=mm_config.get("category", "free"))
                tlog(f"{tag} MailManage 选定: {email_value}", "success")
                return email_value
            except Exception as e:
                tlog(f"{tag} MailManage 获取邮箱失败: {e}", "error")
                return ""
        if tm is not None:
            try:
                with provider_lock:
                    account = tm.create_address()
                tlog(f"{tag} TempMail 创建邮箱: {account.email}", "success")
                return account
            except Exception as e:
                tlog(f"{tag} TempMail 创建邮箱失败: {e}", "error")
                return ""
        if use_outlook:
            try:
                from outlook_mail import reserve_next_outlook
                with provider_lock:
                    outlook_account = reserve_next_outlook(
                        run_config.get("outlook_pool") or "outlook.txt",
                        run_config.get("outlook_used") or "outlook_used.txt",
                    )
                tlog(f"{tag} Outlook 邮箱: {outlook_account.email}", "success")
                return outlook_account.email
            except Exception as e:
                tlog(f"{tag} Outlook 获取邮箱失败: {e}", "error")
                return ""
        if ic is not None:
            try:
                with provider_lock:
                    email_value = ic.reuse_or_create_alias()
                tlog(f"{tag} 新 iCloud 别名: {email_value}", "success")
                return email_value
            except Exception as e:
                tlog(f"{tag} iCloud 别名失败: {e}", "error")
                return ""
        return ""

    def _worker(thread_id):
        tag = f"[T{thread_id}]" if is_multi else ""
        tlog = lambda msg, level="info": _log(msg, level, thread_id=thread_id)
        thread_sms = SmsBower(key)
        log_writer.bind_thread(thread_id)
        try:
            while True:
                claimed = claim_attempt()
                if not claimed:
                    return
                attempt_num, ok_so_far = claimed
                tlog(f"{tag} attempt {attempt_num} [{ok_so_far}/{count}]", "info")
                thread_cfg = copy.deepcopy(run_config)
                try:
                    result = ar.register_one(
                        thread_sms,
                        thread_cfg,
                        verbose=True,
                        step_retries=retries,
                        create_account_max_retries=20,
                        min_price=run_config.get("min_price", ""),
                        max_price=run_config.get("max_price", ""),
                        provider_ids=run_config.get("provider", ""),
                        stop_requested=_stop_requested,
                    )
                except ar.StopRequested:
                    tlog(f"{tag} 已停止等待手机号", "warn")
                    finish_registration(False)
                    return
                except Exception as e:
                    result = {"ok": False, "phone": "?", "error": str(e)}

                if not result.get("ok") and thread_sms.activation_id:
                    try:
                        thread_sms.cancel()
                    except Exception:
                        pass

                _record_result(result)

                if not result.get("ok"):
                    tlog(f"{tag} 失败: {result.get('phone', '?')} {result.get('error', '')}", "error")
                    finish_registration(False)
                    continue

                finish_registration(True)
                ok_num = counters["ok"]
                sys.stdout.flush()
                tlog(f"{tag} 注册成功: {result['phone']} [{ok_num}/{count}]", "success")

                if debug_mode:
                    tlog("=" * 40, "warn")
                    tlog("DEBUG - 注册完成，已暂停", "warn")
                    tlog(f"Phone: {result['phone']}", "success")
                    tlog(f"Password: {result['password']}", "success")
                    tlog(f"Session Token: {result.get('session_token', '')}", "success")
                    tlog("=" * 40, "warn")
                    _state["_paused"] = True
                    try:
                        _state["pause_queue"].get(timeout=300)
                    except queue.Empty:
                        tlog("DEBUG - timed out, auto continue", "warn")
                    _state["_paused"] = False

                phase2_ok = True
                if (
                    not run_config.get("no_phase2")
                    and sub.get("url")
                    and sub.get("email")
                    and result.get("session_token")
                ):
                    if use_tempmail or not bind_email:
                        reserved_email = reserve_phase2_email(thread_id)
                        tempmail_account = reserved_email if hasattr(reserved_email, "jwt") else None
                        new_email = tempmail_account.email if tempmail_account else reserved_email
                        if new_email:
                            thread_cfg["bind_email"] = new_email
                            if tempmail_account:
                                thread_cfg["tempmail"] = dict(thread_cfg.get("tempmail", {}))
                                thread_cfg["tempmail"]["base_url"] = tempmail_account.base_url
                                thread_cfg["tempmail"]["jwt"] = tempmail_account.jwt
                                thread_cfg["tempmail"]["pool"] = ""
                                thread_cfg["tempmail"]["site_password"] = tempmail_account.site_password
                        elif not thread_cfg.get("bind_email"):
                            tlog(f"{tag} 没有可用邮箱，跳过 Phase 2", "error")
                            _save_result(results_dir, result, thread_cfg)
                            continue

                    tlog(
                        f"{tag} === Phase 2: OAuth + 绑定邮箱 + 上传 (邮箱: {thread_cfg.get('bind_email', '?')}) ===",
                        "info",
                    )
                    phase2_ok = False
                    while not phase2_ok and not _state["stop"]:
                        try:
                            oauth_result = _phase2_for_result(result, thread_cfg, tag, thread_id=thread_id)
                        except Exception as e:
                            oauth_result = {"ok": False, "error": str(e)}

                        if oauth_result.get("ok"):
                            phase2_ok = True
                            aid = oauth_result.get("sub2api_account_id", "?")
                            result["sub2api_id"] = aid
                            tlog(f"{tag}   [4/4] 上传成功: SUB2API id={aid}", "success")
                        else:
                            tlog(f"{tag}   [4/4] Phase 2 失败: {oauth_result.get('error', '?')}", "warn")
                            if is_multi or run_config.get("phase2_auto_skip"):
                                tlog(f"{tag}   自动跳过 Phase 2", "warn")
                                break
                            _state["_paused"] = True
                            _state["_phase2_retry"] = True
                            tlog("  [?] retry or skip Phase 2 from UI", "warn")
                            try:
                                action = _state["pause_queue"].get(timeout=600)
                                if action == "skip":
                                    tlog("  [?] 用户选择跳过 Phase 2", "warn")
                                    break
                            except queue.Empty:
                                tlog("  [?] 等待超时，跳过 Phase 2", "warn")
                                break
                            finally:
                                _state["_paused"] = False

                _save_result(results_dir, result, thread_cfg)
                if mm is not None and thread_cfg.get("bind_email") and phase2_ok:
                    try:
                        with provider_lock:
                            mm.mark_used(thread_cfg["bind_email"])
                        tlog(f"{tag} MailManage 已标记: {thread_cfg['bind_email']}", "info")
                    except Exception:
                        pass
                if tm is not None and thread_cfg.get("bind_email") and phase2_ok:
                    try:
                        with provider_lock:
                            tm.mark_used(thread_cfg["bind_email"])
                        tlog(f"{tag} TempMail 已标记: {thread_cfg['bind_email']}", "info")
                    except Exception:
                        pass
        finally:
            log_writer.unbind_thread()

    try:
        threads = []
        for i in range(concurrency):
            thread = threading.Thread(target=_worker, args=(i + 1,), daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
    finally:
        sys.stdout = old_stdout

    with _STATE_LOCK:
        _state["running"] = False
    _log(f"完成: {counters['ok']}/{count}", "success" if counters["ok"] >= count else "warn")


def start_gui(host="0.0.0.0", port=7777):
    print(f"http://127.0.0.1:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)



_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChatGPT Auto Register</title>
<style>
/* 鈹€鈹€ RigsHub Design System 鈹€鈹€ */
:root{color-scheme:light;--paper:#f3efe4;--paper-dim:#e8e2d4;--ink:#0f0e0c;--ink-soft:#5c564e;--ink-faint:#9a938a;--rule:rgba(15,14,12,0.12);--rule-strong:rgba(15,14,12,0.22);--red:#b7392d;--green:#1f8b4c;--mono:"Geist Mono","SFMono-Regular",Consolas,monospace;--serif:Newsreader,"Iowan Old Style",Georgia,serif;--sans:"Noto Sans SC","Geist","PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-family:var(--sans);font-synthesis:none;text-rendering:geometricPrecision;-webkit-font-smoothing:antialiased}
*{box-sizing:border-box;margin:0;padding:0}
html{min-width:900px;height:100%;background:var(--paper)}
body{height:100%;min-height:100vh;overflow:hidden;color:var(--ink);font-size:14px;background:radial-gradient(circle at 20% 14%,rgba(183,57,45,0.035),transparent 28%),radial-gradient(circle at 74% 48%,rgba(15,14,12,0.035),transparent 34%),linear-gradient(90deg,rgba(15,14,12,0.025) 1px,transparent 1px),linear-gradient(rgba(15,14,12,0.025) 1px,transparent 1px),var(--paper);background-size:auto,auto,72px 72px,72px 72px,auto}
body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:0.28;background-image:radial-gradient(circle,rgba(15,14,12,0.16) 0 0.55px,transparent 0.7px),radial-gradient(circle,rgba(183,57,45,0.12) 0 0.45px,transparent 0.65px);background-size:5px 5px,11px 11px;mix-blend-mode:multiply}
button{cursor:pointer;color:inherit;font:inherit}
input,select,textarea{font:inherit}
.manuscript{position:relative;display:grid;grid-template-rows:auto minmax(0,1fr);height:100vh;min-height:0;overflow:hidden;padding:28px 48px 24px}
.nav{display:flex;align-items:center;gap:24px;width:100%;margin:0 auto;padding-bottom:14px;border-bottom:2px solid var(--red)}
.brand{display:inline-flex;align-items:center;gap:10px;min-width:300px;color:inherit;background:none;border:0;cursor:pointer}
.brand-mark{font-size:22px}
.brand-name{font-family:var(--serif);font-size:26px;letter-spacing:0}
.brand-meta{color:var(--ink-faint);font-family:var(--mono);font-size:11px;letter-spacing:0.28em;text-transform:uppercase}
#status-msg{min-width:52px;letter-spacing:0;text-transform:none;font-family:var(--sans);font-size:13px;color:var(--ink-soft)}
.nav-links{display:flex;align-items:center;justify-content:flex-end;gap:24px;width:100%;color:var(--ink-soft);font-size:13px}
.nav-action{background:none;border:0;padding:4px 8px;font-size:13px;color:var(--ink-soft)}
.nav-action.active{color:var(--ink);border-bottom:1px solid var(--ink)}
.nav-action:hover{color:var(--ink)}
.corner{position:fixed;width:22px;height:22px;pointer-events:none;opacity:0.3}
.corner::before,.corner::after{content:"";position:absolute;background:var(--ink-faint)}
.corner::before{top:10px;left:0;width:22px;height:1px}
.corner::after{top:0;left:10px;width:1px;height:22px}
.corner-tl{top:22px;left:22px}.corner-tr{top:22px;right:22px}
.corner-bl{bottom:22px;left:22px}.corner-br{bottom:22px;right:22px}
.content{overflow-y:auto;overflow-x:hidden;padding:24px 0;max-width:1400px;width:100%;margin:0 auto}
.stats{display:flex;gap:16px;margin-bottom:20px}
.stat{flex:1;padding:20px;background:rgba(255,255,255,0.55);border:1px solid var(--rule);text-align:center}
.stat .num{font-size:32px;font-family:var(--serif);color:var(--ink)}
.stat .lbl{font-size:11px;color:var(--ink-faint);margin-top:4px;text-transform:uppercase;letter-spacing:0.12em}
.row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.col{flex:1;min-width:340px}
.card{background:rgba(255,255,255,0.55);border:1px solid var(--rule);padding:20px;margin-bottom:16px}
.card h2{font-family:var(--serif);font-size:18px;color:var(--ink);margin-bottom:14px;font-weight:500}
label{display:block;font-size:11px;color:var(--ink-faint);margin-top:10px;text-transform:uppercase;letter-spacing:0.08em}
input,select,textarea{width:100%;padding:8px 10px;margin:3px 0;background:rgba(255,255,255,0.7);border:1px solid var(--rule);color:var(--ink);font-size:13px}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--ink-faint)}
.btn-primary{padding:8px 20px;background:var(--ink);color:var(--paper);border:none;font-size:13px;cursor:pointer}
.btn-primary:hover:not(:disabled){background:var(--ink-soft)}
.btn-danger{padding:8px 20px;background:var(--red);color:#fff;border:none;font-size:13px;cursor:pointer}
.btn-danger:hover:not(:disabled){opacity:0.85}
.btn-neutral{padding:6px 14px;background:transparent;color:var(--ink-soft);border:1px solid var(--rule);font-size:12px;cursor:pointer}
.btn-neutral:hover:not(:disabled){border-color:var(--ink-faint);color:var(--ink)}
button:disabled{opacity:0.4;cursor:not-allowed}
.log{background:var(--ink);color:#d4d4d4;padding:16px;max-height:450px;overflow-y:auto;font:12px/1.6 var(--mono)}
.log .info{color:#6a9fd8}.log .success{color:#4ec9b0}
.log .error{color:#f44747}.log .warn{color:#ce9178}
.log .time{color:#666;margin-right:8px}
.log-tabs{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.log-tab.active{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.log-toolbar{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--ink-faint);margin-top:8px}
.floating-log{position:fixed;left:24px;right:24px;bottom:18px;z-index:110;margin:0;background:rgba(255,255,255,0.96);backdrop-filter:blur(8px);box-shadow:0 16px 48px rgba(15,14,12,0.18)}
.floating-log h2{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:8px}
.floating-log .log{height:42vh;max-height:520px;min-height:180px}
.floating-log.collapsed{display:none}
.log-float-toggle{position:fixed;right:24px;bottom:18px;z-index:111;box-shadow:0 8px 28px rgba(15,14,12,0.16);background:rgba(255,255,255,0.96)}
.toast{position:fixed;top:16px;right:16px;padding:10px 20px;font-size:13px;z-index:999;opacity:0;transition:opacity .3s}
.toast.show{opacity:1}
.toast-ok{background:#c8e6c9;color:var(--green)}
.toast-err{background:#ffcdd2;color:var(--red)}
.floating-panel{display:none;position:fixed;bottom:16px;right:16px;padding:12px 16px;background:rgba(255,255,255,0.95);border:1px solid var(--rule-strong);z-index:99}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--rule);border-top-color:var(--ink);border-radius:50%;animation:s .6s linear infinite;margin-right:4px}
@keyframes s{to{transform:rotate(360deg)}}
.worker-status{font-size:11px;color:var(--ink-faint);margin-top:8px;text-align:center}
.view{display:block}
.outlook-pool-shell{display:grid;grid-template-columns:minmax(340px,420px) minmax(0,1fr);gap:16px;align-items:start}
.outlook-pool-current{font-size:12px;color:var(--ink-soft);margin-bottom:12px}
.outlook-pool-import{display:grid;grid-template-columns:minmax(0,1fr) 150px;gap:12px;align-items:start}
.outlook-pool-import textarea{min-height:112px;resize:vertical;font:12px/1.6 var(--mono)}
.outlook-pool-import-actions{display:flex;flex-direction:column;gap:8px}
.outlook-pool-import-hint{margin-bottom:10px;font-size:12px;color:var(--ink-faint)}
.outlook-pool-file-name{font-size:11px;color:var(--ink-faint);word-break:break-all}
.outlook-pool-list{border:1px solid var(--rule);background:rgba(255,255,255,0.35);max-height:640px;overflow-y:auto}
.outlook-pool-row{display:block;width:100%;padding:12px 14px;border:0;border-bottom:1px solid var(--rule);background:none;text-align:left}
.outlook-pool-row:hover{background:rgba(15,14,12,0.04)}
.outlook-pool-row.active{background:rgba(15,14,12,0.08)}
.outlook-pool-row:last-child{border-bottom:0}
.outlook-pool-row .title{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink)}
.outlook-pool-row .meta{margin-top:6px;font-size:11px;color:var(--ink-faint);display:flex;flex-wrap:wrap;gap:10px}
.outlook-pool-pill{display:inline-flex;align-items:center;padding:2px 8px;border:1px solid var(--rule);font-size:11px;color:var(--ink-soft);background:rgba(255,255,255,0.4)}
.outlook-pool-pager{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:12px;color:var(--ink-faint)}
.outlook-pool-empty{padding:24px;color:var(--ink-faint);text-align:center}
.outlook-pool-detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.outlook-pool-detail-item{padding:10px 12px;border:1px solid var(--rule);background:rgba(255,255,255,0.35)}
.outlook-pool-detail-item .k{display:block;font-size:11px;color:var(--ink-faint);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px}
.outlook-pool-detail-item .v{font-size:13px;color:var(--ink);word-break:break-all}
.outlook-pool-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.outlook-pool-messages{display:grid;grid-template-columns:minmax(280px,360px) minmax(0,1fr);gap:12px}
.outlook-pool-mail-list{border:1px solid var(--rule);background:rgba(255,255,255,0.35);max-height:420px;overflow-y:auto}
.outlook-pool-mail-row{display:block;width:100%;padding:10px 12px;border:0;border-bottom:1px solid var(--rule);background:none;text-align:left}
.outlook-pool-mail-row:hover{background:rgba(15,14,12,0.04)}
.outlook-pool-mail-row.active{background:rgba(15,14,12,0.08)}
.outlook-pool-mail-row:last-child{border-bottom:0}
.outlook-pool-mail-row .subj{font-size:13px;color:var(--ink)}
.outlook-pool-mail-row .meta{margin-top:4px;font-size:11px;color:var(--ink-faint)}
.outlook-pool-mail-body{min-height:320px;max-height:420px;overflow:auto;padding:12px;border:1px solid var(--rule);background:rgba(255,255,255,0.35);white-space:pre-wrap;font:12px/1.7 var(--mono);color:var(--ink)}
.country-picker{display:grid;grid-template-columns:minmax(0,1fr) 118px 118px;gap:8px}
.country-hint{margin-top:4px;font-size:11px;color:var(--ink-faint)}
.country-option{font-family:var(--sans)}
.country-popup{display:none;position:fixed;z-index:120;top:96px;left:50%;transform:translateX(-50%);width:min(760px,calc(100vw - 80px));max-height:72vh;padding:14px;background:rgba(255,255,255,0.98);border:1px solid var(--rule-strong);box-shadow:0 16px 48px rgba(15,14,12,0.16)}
.country-popup-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.country-popup-title{font-family:var(--serif);font-size:18px;color:var(--ink)}
.country-popup-body{max-height:52vh;overflow:auto;border:1px solid var(--rule);background:rgba(255,255,255,0.45)}
.country-popup-table{width:100%;border-collapse:collapse;font-size:12px}
.country-popup-table th,.country-popup-table td{padding:8px 10px;border-bottom:1px solid var(--rule);text-align:left}
.country-popup-table th{position:sticky;top:0;background:var(--paper);color:var(--ink-soft);font-weight:400}
.country-popup-table tr{cursor:pointer}
.country-popup-table tbody tr:hover{background:rgba(15,14,12,0.05)}
.country-popup-code{font-family:var(--mono);color:var(--ink)}
.country-popup-empty{padding:24px;color:var(--ink-faint);text-align:center}

</style></head><body>
<div class="manuscript">
  <span class="corner corner-tl"></span><span class="corner corner-tr"></span>
  <span class="corner corner-bl"></span><span class="corner corner-br"></span>

  <header class="nav">
    <button class="brand" type="button">
      <span class="brand-mark">&#9881;</span>
      <span class="brand-name">ChatGPT Register</span>
    </button>
    <span class="brand-meta" id="status-msg">就绪</span>
    <nav class="nav-links">
      <button class="nav-action" onclick="downloadResults()">下载结果</button>
      <span class="nav-divider"></span>
      <span class="nav-action" id="balance">-</span>
      <span class="brand-meta">SMSBower</span>
    </nav>
  </header>

  <div class="content">
    <div class="stats">
      <div class="stat"><span class="num" id="ok-count">0</span><span class="lbl">本次成功</span></div>
      <div class="stat"><span class="num" id="fail-count">0</span><span class="lbl">本次失败</span></div>
      <div class="stat"><span class="num" id="total-ok-count">0</span><span class="lbl">累计成功</span></div>
      <div class="stat"><span class="num" id="total-fail-count">0</span><span class="lbl">累计失败</span></div>
    </div>

    <div class="row">
      <div class="col">
        <div class="card"><h2>注册配置</h2>
          <label>SMSBower Key</label><input id="api_key" placeholder="your-smsbower-key">
          <label>代理</label><input id="proxy" placeholder="socks5h://127.0.0.1:10808">
          <label>国家代码</label>
          <div class="country-picker">
            <input id="country" value="151" list="country-options" onchange="syncCountryHint()" oninput="syncCountryHint()" placeholder="151 或 73,151,6">
            <button class="btn-neutral" type="button" onclick="loadSmsbowerCountries(true)">刷新列表</button>
            <button class="btn-neutral" type="button" onclick="toggleCountryPopup()">国家列表</button>
          </div>
          <datalist id="country-options"></datalist>
          <div class="country-hint" id="country-hint">支持多个国家代码，用逗号/空格分隔；单个国家连续失败 3 次后自动切换</div>
          <div class="country-popup" id="country-popup">
            <div class="country-popup-head">
              <div class="country-popup-title">SMSBower dr 国家列表</div>
              <button class="btn-neutral" type="button" onclick="toggleCountryPopup(false)">隐藏</button>
            </div>
            <input id="country-popup-filter" placeholder="搜索国家名 / 代码 / ISO" oninput="renderCountryPopup()">
            <div class="country-popup-body" id="country-popup-body"></div>
          </div>
          <label>最低价格</label><input id="min_price" placeholder="留空=不限">
          <label>最高价格</label><input id="max_price" placeholder="留空=不限">
          <label>密码</label><input id="password" placeholder="留空=随机">
          <div class="row" style="margin-top:10px">
            <div style="flex:1"><label>目标数量</label><input id="count" value="1" type="number" min="1" max="99"></div>
            <div style="flex:1"><label>并发线程</label><input id="concurrency" value="1" type="number" min="1" max="10"></div>
            <div style="flex:1"><label>步骤重试</label><input id="retries" value="2" type="number" min="0" max="10"></div>
          </div>
          <div style="margin-top:12px;display:flex;gap:8px">
            <button class="btn-primary" id="btn-start" onclick="startReg()" style="flex:1">开始注册</button>
            <button class="btn-danger" id="btn-stop" onclick="stopReg()" disabled>停止</button>
          </div>
          <div class="worker-status" id="worker-status"></div>
        </div>
      </div>
      <div class="col">
        <div class="card"><h2>Phase 2: 邮箱 &amp; SUB2API</h2>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;text-transform:none;letter-spacing:0;margin-top:0">
            <input type="checkbox" id="no_phase2" style="width:auto;margin:0"> 不跑 Phase 2
          </label>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;text-transform:none;letter-spacing:0">
            <input type="checkbox" id="phase2_auto_skip" style="width:auto;margin:0"> Phase 2 失败自动跳过
          </label>
          <label>邮箱提供方</label>
          <select id="email_provider" onchange="toggleEmailProviderFields()">
            <option value="">iCloud</option>
            <option value="mailmanage">MailManage</option>
            <option value="outlook">Outlook</option>
            <option value="tempmail">tempmail</option>
          </select>
          <div id="mm-group" style="display:none">
            <label>MailManage Key</label><input id="mailmanage_key">
            <label>分类</label><input id="mailmanage_category" value="safe">
            <label>关键词</label><input id="mailmanage_keyword" value="gpt">
          </div>
          <div id="outlook-group" style="display:none">
            <label>Outlook 账号池</label>
            <textarea id="outlook_pool" rows="6" style="font-family:var(--mono);font-size:11px" placeholder="email----password----client_id----refresh_token"></textarea>
          </div>
          <div id="tempmail-group" style="display:none">
            <label>tempmail API 地址</label><input id="tempmail_base_url" placeholder="https://mail.example.com">
            <label>Address JWT</label><input id="tempmail_jwt" placeholder="单邮箱 JWT；使用池时可留空">
            <label>站点密码</label><input id="tempmail_site_password" type="password" placeholder="可选 x-custom-auth">
            <label>Admin 密码</label><input id="tempmail_admin_password" type="password" placeholder="用于 /admin/new_address；为空则使用 /api/new_address">
            <label>邮箱域名</label><input id="tempmail_domain" placeholder="example.com；留空使用服务默认域名">
            <label>邮箱名前缀</label><input id="tempmail_name_prefix" value="" placeholder="可选；留空使用英文人名随机">
            <label>关键词</label><input id="tempmail_keyword" value="openai">
            <label>tempmail 池</label>
            <textarea id="tempmail_pool" rows="5" style="font-family:var(--mono);font-size:11px" placeholder="jwt 或 email----jwt 或 base_url----email----jwt----site_password"></textarea>
            <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
              <button class="btn-neutral" type="button" onclick="testTempmail()">测试 tempmail</button>
              <span id="tempmail_test_status" style="font-size:12px;color:var(--ink-faint)"></span>
            </div>
          </div>
          <div id="icloud-group">
            <label>iCloud 邮箱 (IMAP)</label><input id="imap_user" placeholder="xxx@icloud.com">
            <label>Apple 专用密码</label><input id="imap_pass" type="password">
          </div>
          <label>SUB2API 地址</label><input id="sub2api_url">
          <label>管理邮箱</label><input id="sub2api_email">
          <label>管理密码</label><input id="sub2api_pwd" type="password">
          <label>目标分组</label><input id="sub2api_group" value="CHATGPT">
          <div style="display:flex;align-items:center;gap:10px;margin-top:8px">
            <button class="btn-neutral" type="button" onclick="testSub2api()">测试 SUB2API</button>
            <span id="sub2api_test_status" style="font-size:12px;color:var(--ink-faint)"></span>
          </div>
          <label>绑定邮箱</label><input id="bind_email">
          <label>iCloud Cookies 路径</label><input id="icloud_cookies" placeholder="cookies.json">
        </div>
      </div>
    </div>

    <div class="card"><h2>Plus 升级</h2>
      <label>支付方式</label>
      <select id="plus_method" onchange="togglePlusFields()">
        <option value="paypal">PayPal 协议线路 (纯协议)</option>
        <option value="gopay">GoPay (印尼手机号 + PIN)</option>
      </select>
      <div id="plus-paypal-group">
        <label>PayPal 邮箱 (用于注册)</label><input id="plus_email" placeholder="your@email.com">
      </div>
      <div id="plus-gopay-group" style="display:none">
        <label>GoPay 手机号</label><input id="plus_phone" placeholder="+6281234567890">
        <label>GoPay PIN</label><input id="plus_pin" type="password" placeholder="6 digits">
      </div>
      <label>国家</label><input id="plus_country" value="ID">
      <label>货币</label><input id="plus_currency" value="IDR">
      <div style="margin-top:10px">
        <button class="btn-primary" onclick="upgradePlus()" style="width:100%">开通 Plus</button>
      </div>
    </div>

    <div class="card"><h2>iCloud Cookies 导入</h2>
      <textarea id="cookies_input" rows="5" style="font-family:var(--mono);font-size:11px" placeholder='[{"name":"X-APPLE-WEB...", ...}]'></textarea>
      <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
        <button class="btn-neutral" onclick="importCookies()">导入 Cookies</button>
        <span id="cookies_status" style="font-size:11px;color:var(--ink-faint)"></span>
      </div>
    </div>

    <div class="card floating-log" id="floating-log"><h2><span>运行日志</span><button class="btn-neutral" type="button" onclick="toggleFloatingLog(false)">隐藏</button></h2>
      <div class="log-toolbar">
        <label style="display:flex;align-items:center;gap:4px;cursor:pointer;margin:0;text-transform:none;letter-spacing:0;font-size:12px">
          <input type="checkbox" id="auto-scroll" checked style="width:auto;margin:0"> 自动滚动
        </label>
        <span style="flex:1"></span>
        <button class="btn-neutral" onclick="clearLog()">清空</button>
      </div>
      <div class="log-tabs" id="log-tabs">
        <button class="btn-neutral log-tab active" id="log-tab-all" type="button" onclick="setActiveLogTab('all')">全部</button>
      </div>
      <div class="log" id="log" style="margin-top:6px"><span class="info">等待启动...</span></div>
    </div>
  </div>
</div>

<button class="btn-neutral log-float-toggle" id="log-float-toggle" type="button" onclick="toggleFloatingLog(true)" style="display:none">显示日志</button>
<div class="toast" id="toast"></div>
<div id="code-panel" class="floating-panel" style="align-items:center;gap:8px">
  <span id="code-hint" style="color:var(--red);font-size:13px">验证码</span>
  <input id="bind-code-input" placeholder="6 digits" maxlength="6" style="width:120px;padding:4px 8px;font-size:14px">
  <button class="btn-primary" onclick="submitCode()" style="padding:4px 12px">提交</button>
</div>
<div id="pause-panel" class="floating-panel" style="align-items:center;gap:8px">
  <span id="pause-msg" style="color:var(--green);font-size:13px">暂停中</span>
  <button class="btn-primary" onclick="doContinue()" style="padding:4px 12px">继续</button>
  <button class="btn-danger" id="btn-skip-phase2" onclick="doSkipPhase2()" style="padding:4px 12px;display:none">跳过</button>
</div>
<script>
function G(id){return document.getElementById(id);}
function toast(msg,ok){var t=G('toast');t.textContent=msg;t.className='toast '+(ok?'toast-ok':'toast-err')+' show';setTimeout(function(){t.className='toast'},2500);}

function escapeHtml(s){
  return String(s||'').replace(/[&<>"]/g,function(ch){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch];
  });
}

var currentView='main';
// bootstrap markers: id="view-main" id="view-outlook-pool" id="outlook-pool-summary" id="outlook-pool-list" id="outlook-pool-detail" id="outlook-pool-messages"
var outlookPoolState={
  loaded:false,
  page:1,
  pageSize:20,
  total:0,
  status:'all',
  q:'',
  selectedEmail:'',
  items:[],
  messages:[],
  selectedMessageIndex:0
};

function bootstrapOutlookPoolView(){
  var content=document.querySelector('.content');
  if(content && !G('view-main')){
    var mainView=document.createElement('div');
    mainView.id='view-main';
    mainView.className='view';
    while(content.firstChild){
      mainView.appendChild(content.firstChild);
    }
    content.appendChild(mainView);
  }
  if(content && !G('view-outlook-pool')){
    var outlookView=document.createElement('div');
    outlookView.id='view-outlook-pool';
    outlookView.className='view';
    outlookView.style.display='none';
    outlookView.innerHTML=[
      '<div class="card">',
      '  <h2>Outlook 池导入</h2>',
      '  <div id="outlook-pool-current-bind" class="outlook-pool-current">当前绑定邮箱: -</div>',
      '  <div class="outlook-pool-import-hint">支持上传 txt 文件、直接粘贴多行 Outlook 账号，或填入账号池文件路径。</div>',
      '  <div class="outlook-pool-import">',
      '    <textarea id="outlook-pool-editor" placeholder="email----password----client_id----refresh_token&#10;email----password----client_id----refresh_token"></textarea>',
      '    <div class="outlook-pool-import-actions">',
      '      <input id="outlook-pool-file" type="file" accept=".txt,text/plain" style="display:none" onchange="importOutlookPoolFile()">',
      '      <button class="btn-neutral" type="button" onclick="chooseOutlookPoolFile()">选择 txt 文件</button>',
      '      <div id="outlook-pool-file-name" class="outlook-pool-file-name">未选择文件</div>',
      '      <button class="btn-primary" id="outlook-pool-save" type="button" onclick="saveOutlookPoolEditor()">保存并刷新</button>',
      '      <button class="btn-neutral" type="button" onclick="syncOutlookPoolEditor()">从当前配置载入</button>',
      '    </div>',
      '  </div>',
      '  <div class="stats" id="outlook-pool-summary"></div>',
      '</div>',
      '<div class="outlook-pool-shell">',
      '  <div class="card">',
      '    <h2>Outlook 池</h2>',
      '    <div class="row" style="margin-bottom:12px">',
      '      <div style="flex:1;min-width:140px">',
      '        <label>状态</label>',
      '        <select id="outlook-pool-filter" onchange="reloadOutlookPoolList(true)">',
      '          <option value="all">全部</option>',
      '          <option value="unused">未使用</option>',
      '          <option value="reserved">已预留</option>',
      '          <option value="success">已注册成功</option>',
      '          <option value="register_failed">注册失败</option>',
      '          <option value="verify_failed">验证失败</option>',
      '          <option value="bad">坏号</option>',
      '        </select>',
      '      </div>',
      '      <div style="flex:1;min-width:180px">',
      '        <label>搜索</label>',
      '        <input id="outlook-pool-query" placeholder="邮箱 / 手机号" onkeydown="if(event.key===&quot;Enter&quot;){reloadOutlookPoolList(true);}">',
      '      </div>',
      '    </div>',
      '    <div style="display:flex;gap:8px;margin-bottom:10px">',
      '      <button class="btn-neutral" type="button" onclick="reloadOutlookPoolList(true)">筛选</button>',
      '      <button class="btn-neutral" type="button" onclick="refreshOutlookPoolPage()">刷新</button>',
      '    </div>',
      '    <div id="outlook-pool-list" class="outlook-pool-list"><div class="outlook-pool-empty">等待加载池子数据...</div></div>',
      '    <div class="outlook-pool-pager">',
      '      <button class="btn-neutral" id="outlook-pool-prev" type="button" onclick="changeOutlookPoolPage(-1)">上一页</button>',
      '      <span id="outlook-pool-page-info">第 1 页</span>',
      '      <button class="btn-neutral" id="outlook-pool-next" type="button" onclick="changeOutlookPoolPage(1)">下一页</button>',
      '    </div>',
      '  </div>',
      '  <div>',
      '    <div class="card">',
      '      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">',
      '        <h2 style="margin-bottom:0;flex:1">详情</h2>',
      '        <button class="btn-neutral" type="button" onclick="refreshSelectedOutlookMessages()">刷新邮件</button>',
      '      </div>',
      '      <div id="outlook-pool-detail"><div class="outlook-pool-empty">请选择一个 Outlook 邮箱。</div></div>',
      '    </div>',
      '    <div class="card">',
      '      <h2>最近邮件</h2>',
      '      <div id="outlook-pool-messages" class="outlook-pool-messages">',
      '        <div id="outlook-pool-message-list" class="outlook-pool-mail-list"><div class="outlook-pool-empty">选择邮箱后加载邮件。</div></div>',
      '        <div id="outlook-pool-message-body" class="outlook-pool-mail-body">暂无邮件内容。</div>',
      '      </div>',
      '    </div>',
      '  </div>',
      '</div>'
    ].join('');
    content.appendChild(outlookView);
  }
  var nav=document.querySelector('.nav-links');
  if(nav && !G('nav-main')){
    nav.insertAdjacentHTML('afterbegin','<button class="nav-action active" id="nav-main" type="button" onclick="switchView(&quot;main&quot;)">主页</button><button class="nav-action" id="nav-outlook-pool" type="button" onclick="switchView(&quot;outlook-pool&quot;)">Outlook 池</button>');
  }
  var brand=document.querySelector('.brand');
  if(brand)brand.setAttribute('onclick',"switchView('main')");
}

function switchView(view){
  currentView=(view==='outlook-pool')?'outlook-pool':'main';
  if(G('view-main'))G('view-main').style.display=(currentView==='main'?'':'none');
  if(G('view-outlook-pool'))G('view-outlook-pool').style.display=(currentView==='outlook-pool'?'':'none');
  if(G('nav-main'))G('nav-main').classList.toggle('active',currentView==='main');
  if(G('nav-outlook-pool'))G('nav-outlook-pool').classList.toggle('active',currentView==='outlook-pool');
  if(currentView==='outlook-pool' && !outlookPoolState.loaded){
    loadOutlookPool();
  }
}

function loadOutlookPool(){
  outlookPoolState.loaded=true;
  loadOutlookPoolSummary();
  loadOutlookPoolList();
  if(outlookPoolState.selectedEmail){
    loadOutlookPoolDetail(outlookPoolState.selectedEmail);
    loadOutlookPoolMessages(outlookPoolState.selectedEmail);
  }
}

function chooseOutlookPoolFile(){
  if(G('outlook-pool-file'))G('outlook-pool-file').click();
}

function syncOutlookPoolEditor(value){
  var nextValue=value;
  var fromConfig=(nextValue===undefined);
  if(nextValue===undefined){
    nextValue=G('outlook_pool')?G('outlook_pool').value:'';
  }
  nextValue=nextValue||'';
  if(G('outlook_pool'))G('outlook_pool').value=nextValue;
  if(G('outlook-pool-editor'))G('outlook-pool-editor').value=nextValue;
  if(fromConfig && G('outlook-pool-file-name'))G('outlook-pool-file-name').textContent='当前配置';
}

function importOutlookPoolFile(){
  var input=G('outlook-pool-file');
  var file=input && input.files && input.files[0];
  if(!file){
    toast('请选择 txt 文件',false);
    return;
  }
  if(G('outlook-pool-file-name'))G('outlook-pool-file-name').textContent=file.name||'已选择文件';
  var reader=new FileReader();
  reader.onload=function(){
    syncOutlookPoolEditor(typeof reader.result==='string' ? reader.result : '');
    toast('文件内容已载入编辑器',true);
  };
  reader.onerror=function(){
    toast('读取文件失败',false);
  };
  reader.readAsText(file,'utf-8');
}

function saveOutlookPoolEditor(){
  var editor=G('outlook-pool-editor');
  var raw=editor?editor.value:'';
  syncOutlookPoolEditor(raw);
  return fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({outlook_pool:raw,email_provider:'outlook'})}).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'save failed');}
    if(G('email_provider'))G('email_provider').value='outlook';
    toggleEmailProviderFields();
    outlookPoolState.loaded=true;
    outlookPoolState.page=1;
    outlookPoolState.selectedEmail='';
    outlookPoolState.messages=[];
    outlookPoolState.selectedMessageIndex=0;
    renderOutlookPoolDetail(null);
    renderOutlookPoolMessages([]);
    loadOutlookPoolSummary();
    loadOutlookPoolList();
    toast('Outlook 池已保存',true);
    return j;
  }).catch(function(err){
    toast(String(err.message||err),false);
    return null;
  });
}

function refreshOutlookPoolPage(){
  loadOutlookPoolSummary();
  loadOutlookPoolList();
  if(outlookPoolState.selectedEmail){
    loadOutlookPoolDetail(outlookPoolState.selectedEmail);
  }
}

function renderOutlookPoolSummary(data){
  var counts=data.counts||{};
  var stats=[
    ['总数',data.total||0],
    ['未使用',counts.unused||0],
    ['已预留',counts.reserved||0],
    ['已注册成功',counts.success||0],
    ['注册失败',counts.register_failed||0],
    ['验证失败',counts.verify_failed||0],
    ['坏号',counts.bad||0]
  ];
  G('outlook-pool-summary').innerHTML=stats.map(function(item){
    return '<div class="stat"><span class="num">'+escapeHtml(String(item[1]))+'</span><span class="lbl">'+escapeHtml(item[0])+'</span></div>';
  }).join('');
  var bind=data.current_bind_email||'-';
  G('outlook-pool-current-bind').innerHTML='当前绑定邮箱: <strong>'+escapeHtml(bind)+'</strong>';
  if(G('bind_email'))G('bind_email').value=data.current_bind_email||'';
  if(G('email_provider') && data.email_provider){
    G('email_provider').value=data.email_provider;
    toggleEmailProviderFields();
  }
}

function loadOutlookPoolSummary(){
  return fetch('/api/outlook-pool/summary').then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'summary failed');}
    renderOutlookPoolSummary(j);
    return j;
  }).catch(function(err){
    if(G('outlook-pool-summary'))G('outlook-pool-summary').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    return null;
  });
}

function reloadOutlookPoolList(resetPage){
  if(G('outlook-pool-filter'))outlookPoolState.status=G('outlook-pool-filter').value||'all';
  if(G('outlook-pool-query'))outlookPoolState.q=(G('outlook-pool-query').value||'').trim();
  if(resetPage)outlookPoolState.page=1;
  loadOutlookPoolList();
}

function changeOutlookPoolPage(delta){
  outlookPoolState.page=Math.max(1,(outlookPoolState.page||1)+delta);
  loadOutlookPoolList();
}

function loadOutlookPoolList(){
  var params=new URLSearchParams({
    status:outlookPoolState.status||'all',
    q:outlookPoolState.q||'',
    page:String(outlookPoolState.page||1),
    page_size:String(outlookPoolState.pageSize||20)
  });
  if(G('outlook-pool-list'))G('outlook-pool-list').innerHTML='<div class="outlook-pool-empty">加载中...</div>';
  return fetch('/api/outlook-pool/list?'+params.toString()).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'list failed');}
    outlookPoolState.items=j.items||[];
    outlookPoolState.total=j.total||0;
    renderOutlookPoolList();
    return j;
  }).catch(function(err){
    if(G('outlook-pool-list'))G('outlook-pool-list').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    if(G('outlook-pool-page-info'))G('outlook-pool-page-info').textContent='加载失败';
    return null;
  });
}

function renderOutlookPoolList(){
  if(!G('outlook-pool-list'))return;
  if(!outlookPoolState.items.length){
    G('outlook-pool-list').innerHTML='<div class="outlook-pool-empty">没有匹配的 Outlook 邮箱。</div>';
  }else{
    G('outlook-pool-list').innerHTML=outlookPoolState.items.map(function(item){
      var active=item.email===outlookPoolState.selectedEmail?' active':'';
      var when=item.last_event_time||item.last_result_time||'-';
      var record=item.has_result?'有本地结果':'无本地结果';
      var current=item.is_current_bind?'<span class="outlook-pool-pill">当前绑定</span>':'';
      return '<button class="outlook-pool-row'+active+'" type="button" onclick="selectOutlookPoolEmail('+JSON.stringify(item.email)+')"><div class="title"><span>'+escapeHtml(item.email)+'</span><span class="outlook-pool-pill">'+escapeHtml(item.status_label||item.status||'')+'</span>'+current+'</div><div class="meta"><span>'+escapeHtml(when)+'</span><span>'+escapeHtml(record)+'</span></div></button>';
    }).join('');
  }
  var totalPages=Math.max(1,Math.ceil((outlookPoolState.total||0)/(outlookPoolState.pageSize||20)));
  if(outlookPoolState.page>totalPages){
    outlookPoolState.page=totalPages;
  }
  if(G('outlook-pool-page-info'))G('outlook-pool-page-info').textContent='第 '+outlookPoolState.page+' / '+totalPages+' 页';
  if(G('outlook-pool-prev'))G('outlook-pool-prev').disabled=outlookPoolState.page<=1;
  if(G('outlook-pool-next'))G('outlook-pool-next').disabled=outlookPoolState.page>=totalPages;
}

function selectOutlookPoolEmail(email){
  outlookPoolState.selectedEmail=email||'';
  renderOutlookPoolList();
  loadOutlookPoolDetail(outlookPoolState.selectedEmail);
  loadOutlookPoolMessages(outlookPoolState.selectedEmail);
}

function renderOutlookActionButton(cls,label,action,email,status,disabled){
  return '<button class="'+cls+'" type="button" onclick="actOnOutlookPool('+JSON.stringify(action)+','+JSON.stringify(email||'')+','+JSON.stringify(status||'')+')"'+(disabled?' disabled':'')+'>'+label+'</button>';
}

function renderOutlookPoolDetail(entry){
  if(!G('outlook-pool-detail'))return;
  if(!entry){
    G('outlook-pool-detail').innerHTML='<div class="outlook-pool-empty">请选择一个 Outlook 邮箱。</div>';
    return;
  }
  var eventLabel=entry.last_event_status||'-';
  var resultTime=entry.last_result_time||'-';
  var bindMark=entry.is_current_bind?'<span class="outlook-pool-pill">当前绑定</span>':'';
  G('outlook-pool-detail').innerHTML='<div class="outlook-pool-detail-grid"><div class="outlook-pool-detail-item"><span class="k">邮箱</span><div class="v">'+escapeHtml(entry.email||'')+' '+bindMark+'</div></div><div class="outlook-pool-detail-item"><span class="k">状态</span><div class="v">'+escapeHtml(entry.status_label||entry.status||'')+'</div></div><div class="outlook-pool-detail-item"><span class="k">最后事件</span><div class="v">'+escapeHtml(eventLabel)+'</div></div><div class="outlook-pool-detail-item"><span class="k">事件时间</span><div class="v">'+escapeHtml(entry.last_event_time||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">手机号</span><div class="v">'+escapeHtml(entry.phone||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">Sub2API ID</span><div class="v">'+escapeHtml(entry.sub2api_id||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">绑定邮箱</span><div class="v">'+escapeHtml(entry.bind_email||'-')+'</div></div><div class="outlook-pool-detail-item"><span class="k">结果时间</span><div class="v">'+escapeHtml(resultTime)+'</div></div></div><div class="outlook-pool-actions">'+renderOutlookActionButton('btn-danger','标记 bad','mark_status',entry.email,'bad',!entry.can_mark_bad)+renderOutlookActionButton('btn-neutral','标记 verify_failed','mark_status',entry.email,'verify_failed',!entry.can_mark_verify_failed)+renderOutlookActionButton('btn-neutral','标记 reserved','mark_status',entry.email,'reserved',!entry.can_mark_reserved)+renderOutlookActionButton('btn-primary','设为本次注册使用','assign_for_run',entry.email,'',!entry.can_assign)+renderOutlookActionButton('btn-primary','取下一个未使用','reserve_next_unused','','',!entry.can_assign)+'</div>';
}

function loadOutlookPoolDetail(email){
  if(!email){
    renderOutlookPoolDetail(null);
    return Promise.resolve(null);
  }
  return fetch('/api/outlook-pool/detail?email='+encodeURIComponent(email)).then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'detail failed');}
    renderOutlookPoolDetail(j.entry||null);
    return j;
  }).catch(function(err){
    if(G('outlook-pool-detail'))G('outlook-pool-detail').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    return null;
  });
}

function actOnOutlookPool(action,email,status){
  var payload={action:action};
  if(email)payload.email=email;
  if(status)payload.status=status;
  return fetch('/api/outlook-pool/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(r){return r.json().then(function(j){return {status:r.status,body:j};});}).then(function(resp){
    if(resp.status>=400 || !resp.body.ok){
      throw new Error(resp.body.error||'action failed');
    }
    if(resp.body.email){
      outlookPoolState.selectedEmail=resp.body.email;
    }
    loadOutlookPoolSummary();
    loadOutlookPoolList();
    loadOutlookPoolDetail(outlookPoolState.selectedEmail);
    loadOutlookPoolMessages(outlookPoolState.selectedEmail);
    if(resp.body.current_bind_email!==undefined && G('bind_email')){
      G('bind_email').value=resp.body.current_bind_email||'';
    }
    if(action==='assign_for_run'){
      toast('Assigned for current run',true);
    }else if(action==='reserve_next_unused'){
      toast('Reserved next unused mailbox',true);
    }else{
      toast('Status updated',true);
    }
    return resp.body;
  }).catch(function(err){
    toast(String(err.message||err),false);
    return null;
  });
}

function renderOutlookMessageList(){
  if(!G('outlook-pool-message-list'))return;
  if(!outlookPoolState.messages.length){
    G('outlook-pool-message-list').innerHTML='<div class="outlook-pool-empty">暂无邮件。</div>';
    return;
  }
  G('outlook-pool-message-list').innerHTML=outlookPoolState.messages.map(function(item,idx){
    var active=idx===outlookPoolState.selectedMessageIndex?' active':'';
    var preview=item.preview?'<div class="meta">'+escapeHtml(item.preview)+'</div>':'';
    return '<button class="outlook-pool-mail-row'+active+'" type="button" onclick="selectOutlookPoolMessage('+idx+')"><div class="subj">'+escapeHtml(item.subject||'(无主题)')+'</div><div class="meta">'+escapeHtml(item.from||'')+' - '+escapeHtml(item.date||'')+'</div>'+preview+'</button>';
  }).join('');
}

function renderOutlookMessageBody(){
  if(!G('outlook-pool-message-body'))return;
  var item=outlookPoolState.messages[outlookPoolState.selectedMessageIndex];
  if(!item){
    G('outlook-pool-message-body').textContent='暂无邮件内容。';
    return;
  }
  var text=(item.subject?item.subject+'\\n\\n':'')+(item.body||item.preview||'');
  G('outlook-pool-message-body').textContent=text||'暂无邮件内容。';
}

function renderOutlookPoolMessages(items){
  outlookPoolState.messages=items||[];
  if(outlookPoolState.selectedMessageIndex>=outlookPoolState.messages.length){
    outlookPoolState.selectedMessageIndex=0;
  }
  renderOutlookMessageList();
  renderOutlookMessageBody();
}

function selectOutlookPoolMessage(index){
  outlookPoolState.selectedMessageIndex=index||0;
  renderOutlookMessageList();
  renderOutlookMessageBody();
}

function loadOutlookPoolMessages(email){
  if(!email){
    renderOutlookPoolMessages([]);
    return Promise.resolve(null);
  }
  if(G('outlook-pool-message-list'))G('outlook-pool-message-list').innerHTML='<div class="outlook-pool-empty">邮件加载中...</div>';
  if(G('outlook-pool-message-body'))G('outlook-pool-message-body').textContent='邮件加载中...';
  return fetch('/api/outlook-pool/messages?email='+encodeURIComponent(email)+'&limit=20').then(function(r){return r.json();}).then(function(j){
    if(!j.ok){throw new Error(j.error||'messages failed');}
    outlookPoolState.selectedMessageIndex=0;
    renderOutlookPoolMessages(j.items||[]);
    return j;
  }).catch(function(err){
    if(G('outlook-pool-message-list'))G('outlook-pool-message-list').innerHTML='<div class="outlook-pool-empty">'+escapeHtml(String(err.message||err))+'</div>';
    if(G('outlook-pool-message-body'))G('outlook-pool-message-body').textContent='邮件加载失败。';
    return null;
  });
}

function refreshSelectedOutlookMessages(){
  if(!outlookPoolState.selectedEmail){
    toast('请先选择一个 Outlook 邮箱',false);
    return;
  }
  loadOutlookPoolMessages(outlookPoolState.selectedEmail);
}

bootstrapOutlookPoolView();
var logEl=G('log'),logTabsEl=G('log-tabs'),logCursor=0;
var allLogs=[],threadLogs={},activeLogTab='all';
var smsbowerCountries=[];
var zhRegionNames=(window.Intl&&Intl.DisplayNames)?new Intl.DisplayNames(['zh-CN'],{type:'region'}):null;

function ensureThreadTab(threadId){
  var tabId='log-tab-thread-'+threadId;
  if(G(tabId))return;
  var btn=document.createElement('button');
  btn.type='button';
  btn.id=tabId;
  btn.className='btn-neutral log-tab';
  btn.textContent='T'+threadId;
  btn.onclick=function(){setActiveLogTab(String(threadId));};
  logTabsEl.appendChild(btn);
}

function renderLogLine(item){
  return '<div class="'+escapeHtml(item.tag||'info')+'"><span class="time">'+escapeHtml(item.time||'')+'</span>'+escapeHtml(item.msg||'')+'</div>';
}

function renderLogPanel(){
  var items=activeLogTab==='all'?allLogs:(threadLogs[activeLogTab]||[]);
  if(!items.length){
    logEl.innerHTML='<span class="info">等待启动...</span>';
  }else{
    logEl.innerHTML=items.map(renderLogLine).join('');
  }
  if(G('auto-scroll').checked)logEl.scrollTop=logEl.scrollHeight;
}

function toggleFloatingLog(show){
  var panel=G('floating-log');
  var btn=G('log-float-toggle');
  if(!panel||!btn)return;
  var willShow=(show===undefined)?panel.classList.contains('collapsed'):!!show;
  panel.classList.toggle('collapsed',!willShow);
  btn.style.display=willShow?'none':'block';
  if(willShow)renderLogPanel();
}

function setActiveLogTab(tabId){
  activeLogTab=String(tabId||'all');
  Array.prototype.forEach.call(document.querySelectorAll('.log-tab'),function(btn){
    var isAll=btn.id==='log-tab-all'&&activeLogTab==='all';
    var isThread=btn.id==='log-tab-thread-'+activeLogTab;
    btn.classList.toggle('active',isAll||isThread);
  });
  renderLogPanel();
}

function pollLog(){
  fetch('/api/log-since/'+logCursor).then(function(r){return r.json()}).then(function(d){
    if(d.lines.length>0){
      d.lines.forEach(function(item){
        allLogs.push(item);
        if(item.thread!==undefined&&item.thread!==null){
          var key=String(item.thread);
          if(!threadLogs[key])threadLogs[key]=[];
          threadLogs[key].push(item);
          ensureThreadTab(key);
        }
      });
      renderLogPanel();
    }
    logCursor=d.cursor;
  });
}
setInterval(pollLog,800);

function saveConfig(){
    var d={api_key:G('api_key').value,proxy:G('proxy').value,country:G('country').value,
    password:G('password').value,min_price:G('min_price').value,max_price:G('max_price').value,
    sms_timeout:30,code_timeout:30,
    email_provider:G('email_provider').value,
    mailmanage_key:G('mailmanage_key').value,mailmanage_category:G('mailmanage_category').value,
    mailmanage_keyword:G('mailmanage_keyword').value,
    outlook_pool:G('outlook_pool').value,
    tempmail_base_url:G('tempmail_base_url').value,tempmail_jwt:G('tempmail_jwt').value,
    tempmail_site_password:G('tempmail_site_password').value,
    tempmail_admin_password:G('tempmail_admin_password').value,
    tempmail_domain:G('tempmail_domain').value,
    tempmail_name_prefix:G('tempmail_name_prefix').value,
    tempmail_pool:G('tempmail_pool').value,
    tempmail_keyword:G('tempmail_keyword').value,
    imap_user:G('imap_user').value,imap_pass:G('imap_pass').value,
    sub2api_url:G('sub2api_url').value,sub2api_email:G('sub2api_email').value,
    sub2api_pwd:G('sub2api_pwd').value,bind_email:G('bind_email').value,
    sub2api_group:G('sub2api_group').value,icloud_cookies:G('icloud_cookies').value,
    plus_method:G('plus_method').value,plus_email:G('plus_email').value,
    plus_phone:G('plus_phone').value,plus_pin:G('plus_pin').value,
    plus_country:G('plus_country').value,plus_currency:G('plus_currency').value,
    debug_mode:'0',no_phase2:G('no_phase2').checked?'1':'0',
    phase2_auto_skip:G('phase2_auto_skip').checked?'1':'0'};
  return fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(r){return r.json()}).then(function(j){toast('配置已保存',j.ok);return j;});
}

function setTestStatus(id,msg,ok){
  var el=G(id);
  if(!el)return;
  el.textContent=msg||'';
  el.style.color=ok?'#2e7d32':'#c62828';
}

function testTempmail(){
  setTestStatus('tempmail_test_status','测试中...',true);
  saveConfig().then(function(){
    return fetch('/api/test-tempmail',{method:'POST'});
  }).then(function(r){return r.json()}).then(function(j){
    if(j.ok){
      setTestStatus('tempmail_test_status','连接成功: '+(j.address||j.mode||''),true);
      toast('tempmail 连接成功',true);
    }else{
      setTestStatus('tempmail_test_status',j.error||'连接失败',false);
      toast(j.error||'tempmail 连接失败',false);
    }
  }).catch(function(e){
    setTestStatus('tempmail_test_status','网络错误',false);
    toast('tempmail 测试失败',false);
  });
}

function testSub2api(){
  setTestStatus('sub2api_test_status','测试中...',true);
  saveConfig().then(function(){
    return fetch('/api/test-sub2api',{method:'POST'});
  }).then(function(r){return r.json()}).then(function(j){
    if(j.ok){
      var groupInfo=j.group_exists===false?('，未找到分组 '+(j.group||'')):'';
      setTestStatus('sub2api_test_status','登录成功'+groupInfo,true);
      toast('SUB2API 连接成功',true);
    }else{
      setTestStatus('sub2api_test_status',j.error||'连接失败',false);
      toast(j.error||'SUB2API 连接失败',false);
    }
  }).catch(function(e){
    setTestStatus('sub2api_test_status','网络错误',false);
    toast('SUB2API 测试失败',false);
  });
}

function checkBalance(){
  fetch('/api/balance').then(function(r){return r.json()}).then(function(j){
    if(j.ok){G('balance').textContent=j.balance.replace('ACCESS_BALANCE:','');}
  }).catch(function(){});
}

function countryDisplayName(item){
  var iso=String(item.iso||'').toUpperCase();
  if(zhRegionNames&&iso.length===2){
    try{
      var zh=zhRegionNames.of(iso);
      if(zh)return zh;
    }catch(e){}
  }
  return item.title||iso||'未知国家';
}

function formatCountryPrice(value){
  if(value===undefined||value===null||value==='')return '-';
  var n=Number(value);
  if(!isFinite(n))return String(value);
  return n.toFixed(4).replace(/0+$/,'').replace(/\\.$/,'');
}

function renderCountryOptions(items){
  smsbowerCountries=items||[];
  var dl=G('country-options');
  if(!dl)return;
  dl.innerHTML=smsbowerCountries.map(function(item){
    var name=countryDisplayName(item);
    var code=String(item.code||'');
    var price=formatCountryPrice(item.min_price);
    var count=item.count?(' / 库存 '+item.count):'';
    return '<option class="country-option" value="'+escapeHtml(code)+'" label="'+escapeHtml(name+' / 代码 '+code+' / 最低价 '+price+count)+'"></option>';
  }).join('');
  syncCountryHint();
  renderCountryPopup();
}

function countryRowText(item){
  return [
    countryDisplayName(item),
    item.title||'',
    item.eng||'',
    item.chn||'',
    item.iso||'',
    item.code||'',
    item.activate_org_code||''
  ].join(' ').toLowerCase();
}

function addCountryCode(code){
  code=String(code||'').trim();
  if(!code)return;
  var input=G('country');
  var raw=String(input.value||'').trim();
  var codes=raw.replace(/，/g,',').replace(/;/g,',').split(/[,\\s]+/).filter(Boolean);
  if(codes.indexOf(code)<0)codes.push(code);
  input.value=codes.join(',');
  syncCountryHint();
  toast('已添加国家代码 '+code,true);
}

function renderCountryPopup(){
  var body=G('country-popup-body');
  if(!body)return;
  var filterEl=G('country-popup-filter');
  var q=String(filterEl?filterEl.value:'').trim().toLowerCase();
  var items=(smsbowerCountries||[]).filter(function(item){
    return !q||countryRowText(item).indexOf(q)>=0;
  });
  if(!items.length){
    body.innerHTML='<div class="country-popup-empty">'+(smsbowerCountries.length?'没有匹配国家':'暂无国家列表，请先点击刷新列表')+'</div>';
    return;
  }
  body.innerHTML='<table class="country-popup-table"><thead><tr><th>国家</th><th>代码</th><th>ISO</th><th>最低价</th><th>库存</th></tr></thead><tbody>'+
    items.map(function(item){
      var name=countryDisplayName(item);
      var code=String(item.code||'');
      var iso=String(item.iso||'').toUpperCase();
      var price=formatCountryPrice(item.min_price);
      var count=item.count||'';
      return '<tr onclick="addCountryCode(\\''+escapeHtml(code)+'\\')"><td>'+escapeHtml(name)+'</td><td class="country-popup-code">'+escapeHtml(code)+'</td><td>'+escapeHtml(iso)+'</td><td>'+escapeHtml(price)+'</td><td>'+escapeHtml(count)+'</td></tr>';
    }).join('')+'</tbody></table>';
}

function toggleCountryPopup(show){
  var popup=G('country-popup');
  if(!popup)return;
  var willShow=(show===undefined)?popup.style.display!=='block':!!show;
  popup.style.display=willShow?'block':'none';
  if(willShow){
    if(!smsbowerCountries.length)loadSmsbowerCountries(false);
    renderCountryPopup();
    setTimeout(function(){var f=G('country-popup-filter');if(f)f.focus();},0);
  }
}

function syncCountryHint(){
  var hint=G('country-hint');
  if(!hint)return;
  var raw=String(G('country').value||'').trim();
  var codes=raw.replace(/，/g,',').replace(/;/g,',').split(/[,\\s]+/).filter(Boolean);
  if(!codes.length){
    hint.textContent='支持多个国家代码，用逗号/空格分隔；单个国家连续失败 3 次后自动切换';
    return;
  }
  var parts=[];
  for(var ci=0;ci<codes.length;ci++){
    var code=codes[ci], item=null;
    for(var i=0;i<smsbowerCountries.length;i++){
      if(String(smsbowerCountries[i].code)===code){item=smsbowerCountries[i];break;}
    }
    if(item){
      parts.push(countryDisplayName(item)+'('+item.code+', '+formatCountryPrice(item.min_price)+')');
    }else{
      parts.push('代码 '+code+' 未匹配');
    }
  }
  if(!smsbowerCountries.length){
    hint.textContent='支持多个国家代码，用逗号/空格分隔；刷新后显示国家名和价格';
    return;
  }
  hint.textContent=parts.join(' → ')+'；每个国家连续失败 3 次后切换，最后一个失败后回到第一个';
}

function loadSmsbowerCountries(force){
  var hint=G('country-hint');
  if(hint)hint.textContent='正在拉取 SMSBower dr 国家价格列表...';
  if(force){
    saveConfig().catch(function(){});
  }
  fetch('/api/smsbower-countries?service=dr').then(function(r){return r.json()}).then(function(j){
    if(!j.ok){
      if(hint)hint.textContent='国家列表拉取失败: '+(j.error||'?');
      return;
    }
    renderCountryOptions(j.items||[]);
    if(hint&&!(j.items||[]).length)hint.textContent='SMSBower 未返回 dr 可用国家价格';
  }).catch(function(e){
    if(hint)hint.textContent='国家列表拉取失败';
  });
}

function startReg(){
  saveConfig().then(function(){
    G('btn-start').disabled=true;G('btn-stop').disabled=false;G('status-msg').innerHTML='<span class=spin></span>运行中';
    G('ok-count').textContent='0';G('fail-count').textContent='0';
    G('total-ok-count').textContent=G('total-ok-count').textContent||'0';
    G('total-fail-count').textContent=G('total-fail-count').textContent||'0';
    clearLog();
    var d={count:parseInt(G('count').value)||1,retries:parseInt(G('retries').value)||2,concurrency:parseInt(G('concurrency').value)||1};
    fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}).then(function(r){return r.json()}).then(function(j){if(!j.ok)toast(j.error,false);});
  });
}

function togglePlusFields(){
  var v=G('plus_method').value;
  G('plus-paypal-group').style.display=(v=='paypal'?'':'none');
  G('plus-gopay-group').style.display=(v=='gopay'?'':'none');
}

function upgradePlus(){
  var phone=G('plus_phone').value.trim();
  var pin=G('plus_pin').value.trim();
  if(!phone||!pin){toast('请填写 GoPay 手机号和 PIN',false);return;}
  saveConfig().then(function(){
    fetch('/api/plus-upgrade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      plus_method:G('plus_method').value,plus_phone:phone,plus_pin:pin,plus_email:G('plus_email').value,
      plus_country:G('plus_country').value,
      plus_currency:G('plus_currency').value
    })}).then(function(r){return r.json()}).then(function(j){
      if(j.ok) toast('Plus 升级已启动',true);
      else toast(j.error,false);
    });
  });
}

function stopReg(){
  G('btn-stop').disabled=true;
  fetch('/api/stop',{method:'POST'}).then(function(){toast('正在停止...',true);});
}

function downloadResults(){window.open('/api/download');}
function clearLog(){
  allLogs=[];threadLogs={};activeLogTab='all';
  logTabsEl.innerHTML=`<button class="btn-neutral log-tab active" id="log-tab-all" type="button" onclick="setActiveLogTab('all')">全部</button>`;
  renderLogPanel();
}

function submitCode(){
  var code=G('bind-code-input').value.trim();
  if(!code||code.length<4){toast('验证码太短',false);return;}
  var tid=G('code-hint').dataset.tid||'';
  fetch('/api/submit-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:code,thread_id:tid})})
    .then(function(r){return r.json()}).then(function(j){
      G('bind-code-input').value='';
      G('code-panel').style.display='none';
      if(j.ok)toast('验证码已提交',true);
      else toast(j.error,false);
    });
}

function doContinue(){
  fetch('/api/continue',{method:'POST'}).then(function(){
    G('pause-panel').style.display='none';
    toast('继续执行',true);
  });
}

function doSkipPhase2(){
  fetch('/api/skip-phase2',{method:'POST'}).then(function(){
    G('pause-panel').style.display='none';
    toast('已跳过 Phase 2',true);
  });
}

function importCookies(){
  var raw=G('cookies_input').value.trim();
  if(!raw){toast('请粘贴 Cookies JSON',false);return;}
  var btn=document.querySelectorAll('#cookies_input + div button')[0];
  btn.disabled=true;btn.textContent='导入中...';
  fetch('/api/icloud-cookies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookies:raw})})
    .then(function(r){return r.json()}).then(function(j){
      btn.disabled=false;btn.textContent='导入 Cookies';
      if(j.ok){G('cookies_status').textContent='已导入 ('+j.size+' bytes)';G('cookies_status').style.color='#2e7d32';toast('Cookies 已导入',true);G('cookies_input').value='';}
      else{G('cookies_status').textContent=j.error;G('cookies_status').style.color='#c62828';toast(j.error,false);}
    }).catch(function(){btn.disabled=false;btn.textContent='导入 Cookies';toast('网络错误',false);});
}

function loadCookiesStatus(){
  fetch('/api/icloud-cookies').then(function(r){return r.json()}).then(function(j){
    if(j.ok && j.loaded){G('cookies_status').textContent='已加载 ('+j.size+' bytes)';G('cookies_status').style.color='#2e7d32';}
    else{G('cookies_status').textContent='未导入';G('cookies_status').style.color='#8b6f4e';}
  });
}

function toggleEmailProviderFields(){
  var v=G('email_provider').value;
  G('mm-group').style.display=(v=='mailmanage'?'':'none');
  G('outlook-group').style.display=(v=='outlook'?'':'none');
  G('tempmail-group').style.display=(v=='tempmail'?'':'none');
  G('icloud-group').style.display=(v==''?'':'none');
}

function loadConfig(){
  fetch('/api/config').then(function(r){return r.json()}).then(function(j){
    if(!j.ok)return;
    var c=j.config;
    if(c.smsbower) G('api_key').value=c.smsbower.api_key||'';
    G('proxy').value=c.proxy||'';
    G('country').value=c.country||'151';
    G('min_price').value=c.min_price||'';
    G('max_price').value=c.max_price||'';
    if(c.register && c.register.password) G('password').value=c.register.password;
    if(c.icloud){G('imap_user').value=c.icloud.user||'';G('imap_pass').value=c.icloud.pass||'';}
    if(c.sub2api){G('sub2api_url').value=c.sub2api.url||'';G('sub2api_email').value=c.sub2api.email||'';G('sub2api_pwd').value=c.sub2api.pwd||'';G('sub2api_group').value=c.sub2api.group||'CHATGPT';}
    if(c.mailmanage){G('mailmanage_key').value=c.mailmanage.api_key||'';G('mailmanage_category').value=c.mailmanage.category||'safe';G('mailmanage_keyword').value=c.mailmanage.keyword||'gpt';}
    if(c.tempmail){G('tempmail_base_url').value=c.tempmail.base_url||'';G('tempmail_jwt').value=c.tempmail.jwt||'';G('tempmail_site_password').value=c.tempmail.site_password||'';G('tempmail_admin_password').value=c.tempmail.admin_password||'';G('tempmail_domain').value=c.tempmail.domain||'';G('tempmail_name_prefix').value=c.tempmail.name_prefix||'';G('tempmail_pool').value=c.tempmail.pool||'';G('tempmail_keyword').value=c.tempmail.keyword||'openai';}
    G('outlook_pool').value=c.outlook_pool||'';
    syncOutlookPoolEditor(c.outlook_pool||'');
    G('bind_email').value=c.bind_email||'';
    G('icloud_cookies').value=c.icloud_cookies||'';
    G('plus_method').value=c.plus_method||'gopay';
    G('plus_email').value=c.plus_email||'';
    G('plus_phone').value=c.plus_phone||'';
    G('plus_pin').value=c.plus_pin||'';
    G('plus_country').value=c.plus_country||'ID';
    G('plus_currency').value=c.plus_currency||'IDR';
    G('email_provider').value=c.email_provider||'';
    toggleEmailProviderFields();
    togglePlusFields();
    if(c.no_phase2) G('no_phase2').checked=true;
    if(c.phase2_auto_skip) G('phase2_auto_skip').checked=true;
    checkBalance();
    loadSmsbowerCountries(false);
  });
}

// 琛ヨ窇 Phase2 鐩稿叧 JS (淇濇寔鍘熸湁閫昏緫)
var _batchFiles = [];
function openBatchPanel(){
  G('batch-panel').style.display='flex';
  G('batch-list').innerHTML='<div style="text-align:center;color:#aaa;padding:20px"><span class=spin></span>加载中...</div>';
  _batchFiles = [];
  var src=G('batch-source').value;
  fetch('/api/results-list?source='+src).then(function(r){return r.json()}).then(function(j){
    if(!j.ok||!j.items.length){
      G('batch-list').innerHTML='<div style="text-align:center;color:#aaa;padding:20px">没有可补跑的账号</div>';
      G('batch-summary').textContent='0 个待处理';
      return;
    }
    _batchFiles = j.items;
    var html='',todo=0,done=0;
    j.items.forEach(function(item){
      var checked=!item.has_phase2?'checked':'';
      if(!item.has_phase2) todo++; else done++;
      var key=item.filename||item.index;
      html+='<label style="display:flex;align-items:center;gap:6px;padding:3px 4px;border-bottom:1px solid #f0e4d0;cursor:pointer">'+
        '<input type="checkbox" class="batch-cb" data-key="'+key+'" '+checked+' style="width:auto;margin:0">'+
        '<span style="flex:1">'+item.phone+'</span>'+
        '<span style="font-size:11px;color:'+(item.has_phase2?'#2e7d32':'#999')+'">'+(item.has_phase2?'已完成':'待处理')+'</span></label>';
    });
    G('batch-list').innerHTML=html;
    G('batch-summary').textContent=todo+' 个待处理, '+done+' 个已完成';
    G('batch-select-all').checked=true;
  }).catch(function(){G('batch-list').innerHTML='<div style="text-align:center;color:#c62828;padding:20px">加载失败</div>';});
}
function closeBatchPanel(){G('batch-panel').style.display='none';}
function toggleSelectAll(){var sel=G('batch-select-all').checked;document.querySelectorAll('.batch-cb').forEach(function(cb){cb.checked=sel;});}
function startBatchPhase2(){
  var files=[];document.querySelectorAll('.batch-cb:checked').forEach(function(cb){files.push(cb.dataset.key);});
  if(!files.length){toast('请至少选择一个账号',false);return;}
  var email=G('batch-email').value.trim(),src=G('batch-source').value,conc=parseInt(G('batch-concurrency').value)||1;
  G('btn-batch-start').disabled=true;G('batch-running').style.display='inline';
  fetch('/api/batch-phase2',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({files:files,email:email,source:src,concurrency:conc})})
    .then(function(r){return r.json()}).then(function(j){
      if(!j.ok){toast(j.error,false);G('btn-batch-start').disabled=false;G('batch-running').style.display='none';}
      else toast('已开始补跑 '+files.length+' 个账号',true);
    }).catch(function(){G('btn-batch-start').disabled=false;G('batch-running').style.display='none';});
  var pollId=setInterval(function(){fetch('/api/status').then(function(r){return r.json()}).then(function(j){
    if(!j.running){clearInterval(pollId);G('btn-batch-start').disabled=false;G('batch-running').style.display='none';toast('补跑完成',true);openBatchPanel();}
  });},2000);
}

function pollCodeNeed(){
  fetch('/api/waiting-code').then(function(r){return r.json()}).then(function(j){
    if(j.waiting){var hint=G('code-hint');hint.textContent=(j.thread_id||'')+' 验证码';hint.dataset.tid=j.thread_id||'';G('code-panel').style.display='flex';G('bind-code-input').focus();}
  });
}
setInterval(pollCodeNeed,2000);

function pollPause(){
  fetch('/api/waiting-pause').then(function(r){return r.json()}).then(function(j){
    if(j.paused){
      G('pause-panel').style.display='flex';
      if(j.phase2_retry){G('pause-msg').textContent='Phase 2 失败，是否重试？';G('btn-skip-phase2').style.display='inline-block';}
      else{G('pause-msg').textContent='调试暂停中';G('btn-skip-phase2').style.display='none';}
    }
  });
}
setInterval(pollPause,2000);

setInterval(function(){
  fetch('/api/status').then(function(r){return r.json()}).then(function(j){
    var running=j.running;
    G('btn-start').disabled=running;G('btn-stop').disabled=!running;
    if(!running)G('status-msg').textContent='就绪';
    var stats=j.stats||{};
    G('ok-count').textContent=stats.current_success||0;
    G('fail-count').textContent=stats.current_fail||0;
    G('total-ok-count').textContent=stats.total_success||0;
    G('total-fail-count').textContent=stats.total_fail||0;
  });
},2000);

loadCookiesStatus();
loadConfig();

</script></body></html>
"""

if __name__ == "__main__":
    start_gui()


