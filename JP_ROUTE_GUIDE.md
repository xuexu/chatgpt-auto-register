# 日本路线 (JP) ChatGPT Plus 支付详解

## 概述

日本路线通过 **日元结算 + 日本账单地址** 走 Stripe Checkout → PayPal/GoPay 完成 ChatGPT Plus 订阅。

| 参数 | 日本 (JP) | 美国 (US) |
|------|----------|----------|
| 货币 | JPY (日元) | USD (美元) |
| Plus 月费 | ¥3,000 | $20 |
| 账单地址来源 | `meiguodizhi.com/jp-address` | `meiguodizhi.com/` |
| 地址格式 | 〒xxx-xxxx 东京都... | 123 Main St, NY... |
| 邮编 | 7位数字 (〒xxx-xxxx) | 5位数字 (ZIP) |
| 电话号码 | +81 开头 | +1 开头 |
| Stripe 地域 | 日本 Stripe | 美国 Stripe |
| PayPal 注册 | 日本 PayPal 域 | 美国 PayPal 域 |

---

## 配置

### Web GUI

在 Plus 升级卡片中：

```
支付方式:    PayPal 协议路线 (纯协议)
账单地址地区:  日本 (JP)
国家:          JP
货币:          JPY
```

### config.json

```json
{
  "plus": {
    "country": "JP",
    "currency": "JPY",
    "address_region": "JP",
    "headless": true
  }
}
```

### API 调用

```python
from plus_payment import generate_plus_link, complete_paypal_checkout_protocol

# ① 生成日本区支付链接
checkout_url = generate_plus_link(
    access_token=chatgpt_token,
    country="JP",
    currency="JPY",
)

# ② 纯协议 PayPal 付款（日本路线）
result = complete_paypal_checkout_protocol(
    checkout_url=checkout_url,
    cookies_str=cookies,
    proxy="socks5h://127.0.0.1:10808",
    email="your@email.com",
    address_region="JP",          # ★ 日本地址
    sms_pool=sms_numbers,          # 可选 SMS 中继池
)
```

---

## 账单地址获取

### 地址 API

**文件**: `payment.py` — `fetch_billing_address()` + `_BILLING_ADDRESS_REGION_PATHS`

```python
_BILLING_ADDRESS_REGION_PATHS = {
    "US": "/",           # meiguodizhi.com/
    "JP": "/jp-address", # meiguodizhi.com/jp-address
}
```

MEIGUODIZHI_ADDRESS_URL: `meiguodizhi.com/api/v1/dz`

日本地址 POST 体：
```json
{"path": "/jp-address", "method": "address"}
```

返回字段（与 US 完全对齐）：
```json
{
  "name": "山田 太郎",
  "line1": "東京都渋谷区神南1-2-3",
  "line2": "渋谷ビル 401",
  "city": "渋谷区",
  "state": "東京都",
  "postal_code": "150-0041",
  "country": "JP",
  "phone": "03-1234-5678",
  "card_number": "485954...",
  "card_expiry": "12/28",
  "card_cvc": "123"
}
```

> ⚠️ 卡号默认改用本地生成的 Luhn-valid VISA（`payment_protocol.py` — `_generate_fake_visa_card()`），因为 meiguodizhi 远端卡已被大量使用，PayPal 风控拒付。

---

## Stripe Checkout 日本区差异

### 货币解析

**文件**: `payment.py` — `_resolve_currency()` + `_COUNTRY_CURRENCY_MAP`

```python
_COUNTRY_CURRENCY_MAP = {
    "US": "USD",
    "JP": "JPY",
    "ID": "IDR",
}
```

传 `country=JP` 自动解析为 `currency=JPY`。

### Stripe 日本域

日本路线 Stripe 会：
1. 展示日元价格（¥3,000/月）
2. 要求日本格式账单地址（〒邮编 + 都道府县 + 市区町村）
3. 电话号码格式 +81
4. 可能触发日本本地风控规则

### 浏览器模式

日本路线浏览器步骤中：
- 自动选择日本语/日本地区
- 识别 `〒` 邮编字段
- 都道府县下拉框（東京都、大阪府等）

**文件**: `payment.py` L5455 — JP 地域 Pay 按钮识别：

```python
if str(identity.get("region") or "").upper() == "JP":
    # 日本 PayPal 域
    paypal_buttons = ["Japan", "日本", "JP"]
else:
    paypal_buttons = ["US", "United States", "美国"]
```

---

## PayPal 协议路线日本详情

### Pipeline 流程

```
proto_stage_stripe_checkout  →  Stripe 日本域 → 选 PayPal
proto_stage_paypal_approve   →  PayPal 日本域登录/注册
proto_stage_paypal_signup    →  日本号码 + 日本地址注册
proto_stage_paypal_authorize →  授权支付 ¥3,000
proto_stage_stripe_poll      →  轮询确认
```

### 日本地址字段映射

**文件**: `payment.py` L1771 — JP 地址 `state` 处理：

```python
if state_value and region.upper() == "JP":
    # 日本地址 state = 都道府県 (Tokyo, Osaka...)
    billing["state"] = state_value  # "東京都"
```

**邮编格式**：JP 邮编在 Stripe 中通常为 7 位数字 `xxx-xxxx`，自动去掉连字符处理。

### PayPal 日本域 OTP 验证

PayPal 日本 SignUp 时，如果触发 `PHONE_CONFIRMATION_REQUIRED`：
- 需要 +81 开头的日本手机号
- 通过 SMS 中继 (`sms_pool`) 接收验证码

SMS 中继格式：
```
+819012345678,https://example.invalid/api/text-relay/relay_token_xxx
```

---

## GoPay 路线日本

GoPay 是印尼专属支付方式，**日本路线不支持 GoPay**。

日本路线只能用 **PayPal**（协议或浏览器）。

---

## 常见问题

### Q: 日本路线和 US 路线价格差异？

| 路线 | 月费 | 年费 |
|------|------|------|
| US | $20/月 | $200/年 |
| JP | ¥3,000/月 | ¥30,000/年 |

按当前汇率（1 USD ≈ 150 JPY），JP 路线约 $20/月，价格几乎一致。

### Q: 地址 API 获取失败怎么办？

地址获取失败时，`complete_paypal_checkout_protocol` 会带空地址进入 pipeline。Stripe 可能要求手动填写，此时建议：
1. 检查 meiguodizhi API 是否可用
2. 手动准备日本地址填入 `address` 参数
3. 回退到浏览器模式（Camoufox）

### Q: 日本 PayPal 注册需要什么？

`_generate_paypal_signup_identity()` 自动生成：
- 日本姓名（罗马字，如 "Taro Yamada"）
- 日本地址（从 meiguodizhi 拉取）
- 日本手机号（如 +81 开头，从 sms_pool 获取）
- 随机 VISA 卡号（Luhn-valid）
- 随机邮箱（@gmail.com）

### Q: SMS 中继是什么？

短信中继是一个 HTTP API，用于接收 PayPal/GoPay 验证短信。格式：

```
POST {relay_url}
→ 返回最新短信文本

可从 yuecheng.shop 等短信中继服务商获取 relay URL。
```

---

## 配置示例

### 完整 config.json (日本 PayPal 路线)

```json
{
  "smsbower": {"api_key": "YOUR_KEY"},
  "proxy": "socks5h://127.0.0.1:10808",
  "plus": {
    "country": "JP",
    "currency": "JPY",
    "address_region": "JP",
    "paypal_email": "your@email.com",
    "sms_pool": [
      {
        "phone": "+819012345678",
        "phone_e164": "+819012345678",
        "relay_url": "https://example.invalid/api/text-relay/relay_token_xxx"
      }
    ]
  }
}
```
