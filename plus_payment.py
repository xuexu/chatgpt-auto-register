"""
ChatGPT Plus 支付链路 — 双路线支持

  PayPal 路线（纯协议，无需浏览器）：
    ① generate_plus_link(access_token) → Stripe Checkout URL
    ② complete_paypal_checkout_protocol(checkout_url) → 成功/失败

  GoPay 路线（需要浏览器 + GoPay 账号）：
    ① generate_plus_link(access_token) → Stripe Checkout URL
    ② grab_midtrans_url(cashier_url) → Midtrans snap URL (浏览器)
    ③ GoPayPayment.pay(midtrans_url) → 14 步 Midtrans API 付款
"""

import re
import time
from typing import Optional, Callable
from curl_cffi import requests as cffi_requests

PAYMENT_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
_MIDTRANS_URL_RE = re.compile(
    r"https?://app\.midtrans\.com/snap/v[34]/redirection/[0-9a-f-]{36}",
    re.IGNORECASE,
)


def generate_plus_link(
    access_token: str,
    cookies: str = "",
    country: str = "ID",
    currency: str = "IDR",
    proxy: str = "",
) -> str:
    """
    生成 ChatGPT Plus 支付链接（纯协议，无需浏览器）。

    Args:
        access_token: ChatGPT access_token (Bearer token)
        cookies: 可选的 cookie 字符串
        country: 国家代码，默认 ID (印尼)
        currency: 货币代码，默认 IDR
        proxy: 代理 URL

    Returns:
        Stripe Checkout URL (cashier_url)

    Raises:
        ValueError: API 未返回 checkout URL
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "oai-language": "zh-CN",
    }
    if cookies:
        headers["cookie"] = cookies
        # 提取 oai-device-id
        for part in cookies.split(";"):
            part = part.strip()
            if part.startswith("oai-device-id="):
                headers["oai-device-id"] = part.split("=", 1)[1]
                break

    payload = {
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/#pricing",
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "hosted",
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = cffi_requests.post(
                PAYMENT_CHECKOUT_URL,
                headers=headers,
                json=payload,
                proxies=proxies,
                timeout=30,
                impersonate="chrome110",
            )
            resp.raise_for_status()
            data = resp.json()
            url = data.get("cashier_url") or data.get("url") or ""
            if url:
                return url
            raise ValueError(
                data.get("detail", "API 未返回 checkout URL")
            )
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                wait = 2 ** attempt
                print(f"  [Plus] generate_plus_link 重试 {attempt}/3 ({wait}s)...")
                time.sleep(wait)

    raise last_exc or RuntimeError("generate_plus_link failed")


# ── PayPal 纯协议路线 ──

def complete_paypal_checkout_protocol(
    checkout_url: str,
    cookies_str: str = "",
    proxy: str = "",
    email: str = "",
    timeout: int = 180,
    log_fn = print,
    sms_pool: list = None,
    address: dict = None,
) -> dict:
    """
    纯协议 PayPal Checkout（无需浏览器）。
    
    调用 payment_protocol.run_protocol_checkout 执行完整的
    Stripe → PayPal → 授权 → 轮询 流程。
    
    Args:
        checkout_url: Stripe Checkout URL
        cookies_str: ChatGPT cookies 字符串
        proxy: 代理 URL
        email: PayPal 邮箱（用于注册/登录）
        timeout: 超时秒数
        log_fn: 日志回调
        sms_pool: SMS 号码池 [{"phone": "+123...", "relay_url": "..."}]
        address: 账单地址 {"line1":"...", "city":"...", "state":"...", "postal_code":"...", "country":"US"}
    
    Returns:
        {"ok": True/False, "status": "...", "final_url": "..."}
    """
    from payment_protocol import run_protocol_checkout
    
    return run_protocol_checkout(
        checkout_url=checkout_url,
        cookies_str=cookies_str,
        proxy=proxy,
        email=email,
        payment_method="paypal",
        timeout=timeout,
        log_fn=log_fn,
        cancel_check=None,
        turnstile_solver=None,
        sms_pool=sms_pool or [],
        address=address or {},
    )


# ── GoPay 路线（需要浏览器） ──


def grab_midtrans_url(
    cashier_url: str,
    proxy: str = "",
    headless: bool = True,
    timeout: int = 300,
    log: Callable[[str], None] = print,
) -> str:
    """
    用 Playwright 打开 Stripe Checkout，自动选 GoPay → 填账单 → 订阅，
    抓取跳转后的 Midtrans URL。

    Args:
        cashier_url: Stripe Checkout URL
        proxy: 代理 URL
        headless: 是否无头模式
        timeout: 超时秒数
        log: 日志回调

    Returns:
        Midtrans snap redirect URL

    Raises:
        RuntimeError: 超时或无法抓取
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "grab_midtrans_url 需要 playwright。安装: pip install playwright && playwright install chromium"
        )

    log(f"启动浏览器 (headless={headless})...")

    with sync_playwright() as p:
        launch_args = {"headless": headless}
        if proxy:
            launch_args["proxy"] = {"server": proxy}

        browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        page = context.new_page()

        midtrans_url = None

        def _on_navigation(frame):
            nonlocal midtrans_url
            url = frame.url
            if _MIDTRANS_URL_RE.search(url):
                midtrans_url = url
                log(f"捕获 Midtrans URL: {url[:80]}...")

        page.on("framenavigated", _on_navigation)

        try:
            log(f"打开 Checkout: {cashier_url[:80]}...")
            page.goto(cashier_url, wait_until="domcontentloaded", timeout=30000)
            log("页面已加载，等待 Midtrans 跳转...")

            # 等待 Midtrans URL 出现或超时
            deadline = time.time() + timeout
            while time.time() < deadline:
                if midtrans_url:
                    break

                # 尝试自动点击 GoPay 选项
                try:
                    # Stripe Checkout 中的 GoPay 按钮
                    gopay_btn = page.query_selector(
                        'button:has-text("GoPay"), '
                        '[data-testid="GoPay"], '
                        'text=GoPay'
                    )
                    if gopay_btn:
                        gopay_btn.click()
                        log("已选择 GoPay")
                except Exception:
                    pass

                # 尝试点击订阅按钮
                try:
                    subscribe_btn = page.query_selector(
                        'button:has-text("Subscribe"), '
                        'button:has-text("订阅"), '
                        '[data-testid="hosted-payment-submit-button"]'
                    )
                    if subscribe_btn and subscribe_btn.is_enabled():
                        subscribe_btn.click()
                        log("已点击订阅")
                except Exception:
                    pass

                time.sleep(3)

            if not midtrans_url:
                # 兜底：从页面 URL 直接提取
                current_url = page.url
                m = _MIDTRANS_URL_RE.search(current_url)
                if m:
                    midtrans_url = m.group(0)
                    log(f"从当前 URL 提取: {midtrans_url[:80]}...")

        finally:
            browser.close()

    if not midtrans_url:
        raise RuntimeError(f"超时 ({timeout}s)：未捕获到 Midtrans URL")

    log(f"Midtrans URL: {midtrans_url[:80]}...")
    return midtrans_url


# ── PayURL 云端生成支付链接（自带代理，无需本地代理）──

PAYURL_API = "https://payurl.ark2.cn/api/checkout"


def generate_plus_link_via_payurl(
    access_token: str,
    api_key: str = "",
    timeout: int = 60,
    log_fn = print,
) -> str:
    """
    通过 PayURL 云端 API 生成 ChatGPT Plus 支付长链接。

    云端自带日本代理，无需本地配置代理。直接传 access_token 即可。

    Args:
        access_token: ChatGPT access_token (Bearer token)
        api_key: PayURL API Key
        timeout: 请求超时秒数
        log_fn: 日志回调

    Returns:
        Stripe Checkout URL (pay.openai.com 长链接)

    Raises:
        RuntimeError: API 返回错误或超时
    """
    import requests as _requests

    log_fn("[PayURL] 请求云端生成支付链接...")
    try:
        resp = _requests.post(
            PAYURL_API,
            json={"api_key": api_key, "token": access_token},
            timeout=timeout,
        )
        data = resp.json()

        if resp.status_code != 200 or data.get("error"):
            err = data.get("error", f"HTTP {resp.status_code}")
            raise RuntimeError(f"PayURL API 错误: {err}")

        url = data.get("url") or data.get("openai_payurl") or data.get("chatgpt_checkout_url") or ""
        if not url:
            raise RuntimeError("PayURL 未返回支付链接")

        log_fn(f"[PayURL] 成功生成 (proxy={data.get('default_proxy_label', 'unknown')})")
        return url

    except _requests.exceptions.Timeout:
        raise RuntimeError(f"PayURL API 超时 ({timeout}s)")
    except _requests.exceptions.RequestException as e:
        raise RuntimeError(f"PayURL API 请求失败: {e}")


if __name__ == "__main__":
    print("ChatGPT Plus 支付链路模块")
    print()
    print("用法:")
    print("  from plus_payment import generate_plus_link, grab_midtrans_url")
    print()
    print("  # 步骤①：生成支付链接（本地协议）")
    print("  url = generate_plus_link(access_token='eyJ...')")
    print()
    print("  # 步骤①：生成支付链接（PayURL 云端，自带代理）")
    print("  url = generate_plus_link_via_payurl(access_token='eyJ...', api_key='payurl_xxx')")
    print()
    print("  # 步骤②：浏览器捕获 Midtrans URL")
    print("  midtrans_url = grab_midtrans_url(url, headless=False)")
