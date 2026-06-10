# ChatGPT Plus + GoPay 协议注册全流程接入文档

> 基于 [aBaiAutoplus](https://github.com/asz798838958/aBaiAutoplus) 逆向工程成果整理
>
> 上游：[lxf746/any-auto-register](https://github.com/lxf746/any-auto-register) 插件化注册框架

---

## 一、项目架构总览

### 1.1 目录结构

```
aBaiAutoplus-main/
├── main.py                          # FastAPI 入口，加载插件/provider/调度器
├── application/
│   └── gopay_pay_chatgpt.py         # ★ GoPay 付款 Plus 三步流水线编排器 (1007行)
├── api/
│   ├── task_commands.py             # POST /api/tasks/gopay-pay-chatgpt (L105-107)
│   └── tasks.py                     # 任务 CRUD
├── core/
│   ├── registration/
│   │   ├── flows.py                 # ProtocolMailboxFlow / BrowserRegistrationFlow / ProtocolOAuthFlow (141行)
│   │   ├── adapters.py              # ProtocolMailboxAdapter / BrowserRegistrationAdapter
│   │   ├── models.py                # RegistrationContext / RegistrationArtifacts / RegistrationResult
│   │   └── helpers.py               # build_otp_callback() / build_phone_callbacks()
│   └── db.py                        # AccountModel (SQLite/SQLModel)
├── platforms/
│   ├── chatgpt/
│   │   ├── plugin.py                # ChatGPTPlatform: protocol/headless/headed 三模式
│   │   └── payment.py               # ★ generate_plus_link() + select_gopay_and_grab_midtrans() (7758行)
│   ├── gopay/
│   │   ├── plugin.py                # GoPayPlatform: 注册 + SMS 渠道 (431行)
│   │   ├── sms_channel.py           # SmsPoolChannel / SmsActivateStyleChannel / patcher 函数
│   │   └── gopay-deploy/
│   │       └── app/src/opai/core/
│   │           ├── gopay_payment_protocol.py  # ★ GoPayPayment: 14步 Midtrans 付款管线 (485行)
│   │           └── gopay_protocol_worker.py   # GoPay 注册 worker
│   └── gopay-deploy/                # GoPay 独立部署包
└── gopay-auto-protocol/
    ├── gopay_protocol.py            # ★ GoPayProtocol: 纯协议注册引擎 (865行)
    │                                #   含 X-E1 签名器、DeviceProfile、PIN 令牌化
    ├── full_pure_signup_pin.py      # 完整注册+PIN 流水线 (722行)
    └── pure_pin_only.py             # CLI 入口: python pure_pin_only.py --pin 123456 (168行)
```

### 1.2 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | FastAPI (Python 3.11+) |
| 数据库 | SQLite + SQLModel |
| 前端 | React + Vite + Tailwind CSS + shadcn/ui |
| 桌面端 | Electron |
| HTTP 客户端 | curl_cffi (TLS 指纹) / tls_client / requests |
| 加密 | pycryptodome (AES/HMAC) |
| 浏览器自动化 | Playwright + Camoufox / BitBrowser |
| 容器化 | Docker + docker-compose |

---

## 二、GoPay 纯协议注册流程

### 2.1 入口

**文件**：`gopay-auto-protocol/pure_pin_only.py`（168行）

```bash
# CLI
python pure_pin_only.py --pin 123456 --proxy socks5h://127.0.0.1:10808 --provider herosms --api-key YOUR_KEY

# API 调用
from full_pure_signup_pin import run
result = run(pin="123456", proxy="...", provider="herosms", api_key="...")
```

### 2.2 核心引擎：GoPayProtocol

**文件**：`gopay-auto-protocol/gopay_protocol.py`（865行）

**类**：`GoPayProtocol`

**方法调用链**：

```
GoPayProtocol.__init__(device_profile, signer)
  → cvs_methods()          # 买号
  → cvs_initiate(phone)    # OTP 发送
  → cvs_verify(code)       # OTP 验证 → registration_token
  → signup(payload)        # 用户注册 → refresh_token
  → refresh_token(token)   # 换 access_token
  → pin_setup_token(pin)   # PIN 设置 (二次 CVS OTP)
```

### 2.3 完整注册步骤

#### 步骤 1：购买印尼号码

**文件**：`full_pure_signup_pin.py` — `poll_sms_code()` / `run()`

| 参数 | 值 |
|------|-----|
| 接码平台 | HeroSMS / SMSBower / SMSPool |
| service | `ni` (GoPay Indonesia) |
| country | `6` (Indonesia) |
| 号码前缀 | +62 |

#### 步骤 2：CVS OTP 发送

**文件**：`gopay_protocol.py` — `GoPayProtocol.cvs_initiate()`

```
POST /v7//customers/signup          ← 双斜杠绕过 WAF
Host: gopay.co.id
Headers:
  X-UniqueId: {uuid}
  X-E1: {signature}
  D1: {device_id}
  X-M1: {model}
  X-App-Version: 6.75.0
  Content-Type: application/json
Body: { phone, device_id, os="android" }
→ registration_token
```

#### 步骤 3：CVS OTP 验证

**文件**：`gopay_protocol.py` — `GoPayProtocol.cvs_verify()`

```
POST /v7/customers/signup           ← 单斜杠，带签名
Body: { registration_token, otp_code, device_id }
→ success + refresh_token
```

#### 步骤 4：Token 刷新

**文件**：`gopay_protocol.py` — `GoPayProtocol.refresh_token()`

```
POST /goto-auth/token
Body: { grant_type="refresh_token", refresh_token }
→ access_token (JWE)
```

#### 步骤 5：PIN 设置

**文件**：`gopay_protocol.py` — `GoPayProtocol.pin_setup_token()`

```
1. 再次 CVS OTP (同步骤 2-3，reuse activation_id)
2. POST /v7/customers/pin/setup
   Body: { pin_token, new_pin=tokenize_pin_aes_ecb(pin, pin_token) }
→ 成功
```

### 2.4 X-E1 签名机制

**文件**：`gopay_protocol.py` L50-230

| 类名 | 行号 | 说明 |
|------|------|------|
| `NullSigner` | ~90 | 空签名（调试用） |
| `CapturedSigner` | ~100 | 固定捕获值 |
| `AdbOracleSigner` | ~110 | 通过 ADB 调真机签名 |
| `PurePythonXESigner` | ~120 | 纯 Python HMAC-SHA256 |
| `EnhancedPythonXESigner` | ~170 | **默认**，HMAC-SHA256 + 额外混淆 |

**核心密钥**（L20）：
```python
DISPLAY_ENCODER_ENHANCED_KEY = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
```

**签名流程**：
```python
def sign(self, payload: str) -> str:
    h = hmac.new(self.key.encode(), payload.encode(), hashlib.sha256)
    return base64.b64encode(h.digest()).decode()
```

**签名方法**：HMAC-SHA256(ENHANCED_KEY, minjson(payload)) → Base64

### 2.5 设备指纹

**文件**：`gopay_protocol.py` — `DeviceProfile` 类（L240-340）

```python
@dataclass
class DeviceProfile:
    device_id: str       # random UUID v4
    d1: str              # 随机设备标识
    x_unique_id: str     # X-UniqueId 请求头
    x_m1: str            # 设备型号 (X-M1)
    x_session_id: str    # 会话 ID
```

### 2.6 PIN 令牌化（AES 加密）

**文件**：`gopay_protocol.py` — `tokenize_pin_aes_ecb()`（L58-77）

```python
def tokenize_pin_aes_ecb(pin: str, pin_token: str) -> str:
    """
    AES/ECB/PKCS5Padding
    Key = pin_token 重复填充至 16 字节
    Input = pin (6 digits + PKCS5 pad → 16 bytes)
    Output = Base64(ciphertext)
    """
    from Crypto.Cipher import AES
    key = (pin_token * ((16 // len(pin_token)) + 1))[:16].encode()
    plain = pkcs7_pad(pin.encode(), 16)
    cipher = AES.new(key, AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(plain)).decode()
```

### 2.7 SMS 渠道支持

**文件**：`platforms/gopay/sms_channel.py`（~360行）

| 类 | 行号 | 说明 | API 端点 |
|----|------|------|---------|
| `SmsPoolChannel` | 55-175 | SMSPool REST API | `https://api.smspool.net` |
| `SmsActivateStyleChannel` | 180-260 | HeroSMS/SMSBower 通用 | `handler_api.php` 兼容 |

**关键函数**：

| 函数 | 行号 | 说明 |
|------|------|------|
| `get_number()` | 85-120 | 购买号码，返回 `(activation_id, phone)` |
| `wait_code()` | 130-160 | 轮询验证码，最多 300s |
| `request_another()` | 165-180 | `setStatus=3` 续接 OTP |
| `done()` | 185-195 | `setStatus=6` 确认完成 |
| `cancel()` | 200-210 | `setStatus=8` 取消激活 |

**SMSBower 集成**（patcher 模式）：
```python
# platforms/gopay/sms_channel.py L325-360
def patch_worker_with_smsbower(api_key: str, country: str = "6"):
    """覆盖 gopay_protocol_worker 中的 SMS 函数为 SMSBower 实现"""
```

**跨步骤复用**：一个 HeroSMS activation_id 可复用于 3 次 OTP：
1. 注册 OTP（`setStatus=1`）
2. PIN 设置 OTP（`setStatus=3` 续接）
3. 付款验证 OTP（`setStatus=3` 续接）
有效期 20 分钟，由 `PhoneTTLGuard` 守护。

---

## 三、ChatGPT Plus 升级全流程

### 3.1 编排器入口

**文件**：`application/gopay_pay_chatgpt.py`（1007行）

**API**：`POST /api/tasks/gopay-pay-chatgpt`

**类**：`GopayPayChatGPT`

**三步流水线**：

```
     ChatGPT (已登录 session_token)
          │
          ▼
  ┌────────────────────────────┐
  │ ① generate_plus_link()     │ ← 协议，生成 Stripe Checkout URL
  │   payment.py L325          │
  │   → cashier_url            │
  └────────┬───────────────────┘
           │
           ▼
  ┌────────────────────────────┐
  │ ② select_gopay_and_grab()  │ ← 浏览器，打开 → 选 GoPay → 填账单 → 订阅
  │   payment.py L7394         │   → midtrans_url
  │   → app.midtrans.com/...   │
  └────────┬───────────────────┘
           │
           ▼
  ┌────────────────────────────┐
  │ ③ GoPayPayment.pay()       │ ← 协议，14 步 Midtrans API
  │   gopay_payment_protocol.py│
  │   → "settlement" / "capture"│
  └────────────────────────────┘
```

**关键参数**：
- `country`: `ID`（印尼）
- `currency`: `IDR`（印尼盾）
- `subscription_plan`: ChatGPT Plus 月付计划
- `price_id`: Stripe Price ID
- 代理：步骤①强制直连，步骤③走代理

### 3.2 步骤①：生成支付链接（协议）

**文件**：`platforms/chatgpt/payment.py` — `generate_plus_link()`（L325）

```python
def generate_plus_link(
    session_token: str,
    country: str = "ID",
    currency: str = "IDR",
    proxy: str = "",
) -> str:
    """
    POST https://chatgpt.com/backend-api/payments/checkout
    Headers:
      Authorization: Bearer {session_token}
      Content-Type: application/json
    Body:
      { price_id, success_url, country, currency }
    → { cashier_url }
    """
```

**关键细节**：
- **强制直连**：不用代理（否则 Stripe 风控拒付）
- **指数退避重试**：应对 `curl_cffi` 多线程 SSL 竞态（`invalid library` / `tls connect error`），最多 3 次
- **URL 格式**：`https://checkout.stripe.com/c/pay/cs_live_xxx#fidkdWx...`

### 3.3 步骤②：浏览器抓取 Midtrans URL

**文件**：`platforms/chatgpt/payment.py` — `select_gopay_and_grab_midtrans()`（L7394）

**支持的浏览器模式**：

| 模式 | 参数 | 说明 |
|------|------|------|
| `camoufox_headed` | — | Camoufox 反检测，可见窗口 |
| `camoufox_headless` | — | Camoufox 反检测，无头 |
| `bitbrowser_headed` | `--browser bitbrowser` | BitBrowser 可见 |
| `bitbrowser_headless` | `--browser bitbrowser_headless` | BitBrowser 无头 |
| `bitbrowser_headless_port` | `--browser bitbrowser_headless_port` | BitBrowser 指定端口 |

**自动化步骤**：
1. 启动 Playwright + 反检测浏览器
2. 打开 Stripe Checkout URL
3. 等待页面加载完成
4. 自动选择 **GoPay** 支付方式
5. 填写账单信息（印尼地址/邮编）
6. 点击"订阅"（Subscribe）按钮
7. 等待跳转到 `app.midtrans.com/snap/v[34]/redirection/{uuid}`
8. 抓取 → 返回 `midtrans_url`
9. 关闭浏览器

**超时**：300 秒
**回调**：`cancel_check` 参数可传入终止检查函数

### 3.4 步骤③：GoPay 协议付款（14 步）

**文件**：`platforms/gopay-deploy/app/src/opai/core/gopay_payment_protocol.py`（485行）

**类**：`GoPayPayment`（7 个方法）

**API 基础地址**：
```python
MIDTRANS_BASE = "https://app.midtrans.com"
SNAP        = "/snap/v3/accounts"
LINKING     = "/v1/linking"
PAYMENT     = "/v1/payment"
GWA_BASE    = "https://gopay.co.id"
```

#### Phase A：账户链接（7 步）

| 步 | 方法 | HTTP | 端点 | 说明 |
|----|------|------|------|------|
| A1 | `_midtrans_post` | POST | `/snap/v3/accounts/{snap}/linking` | 初始化链接 → `reference` |
| A2 | `_midtrans_post` | POST | `/v1/linking/validate-reference` | 验证引用 |
| A3 | `_midtrans_post` | POST | `/v1/linking/user-consent` | 用户同意 |
| A4 | `_midtrans_post` | POST | `/v1/linking/resend-otp` | 强制 SMS OTP |
| A5 | `_midtrans_post` | POST | `/v1/linking/validate-otp` | 验证 OTP → `challenge_id` |
| A6 | `_gwa_post` | POST | `/api/v1/users/pin/tokens/nb` | PIN 令牌 (MGUPA) → `pin_token` |
| A7 | `_pin_verify` | POST | `/v1/linking/validate-pin` | 提交 `pin_token` |

#### Phase B：扣款（2 步）

| 步 | 方法 | HTTP | 端点 | 说明 |
|----|------|------|------|------|
| B1 | `_midtrans_get` | GET | `/snap/v3/accounts/{snap}/gopay` | 轮询直到 `linked` |
| B2 | `_midtrans_post` | POST | `/snap/v2/transactions/{snap}/charge` | 扣款 → `challenge_id` |

#### Phase C：支付挑战（4 步）

| 步 | 方法 | HTTP | 端点 | 说明 |
|----|------|------|------|------|
| C1 | `_midtrans_get` | GET | `/v1/payment/validate` | 验证支付 |
| C2 | `_midtrans_post` | POST | `/v1/payment/confirm` | 确认 |
| C3 | `_gwa_post` | POST | `/api/v1/users/pin/tokens/nb` | PIN 令牌 (GWC) |
| C4 | `_midtrans_post` | POST | `/v1/payment/process` | 最终处理 |

#### Phase D：验证（1 步）

| 步 | 方法 | HTTP | 端点 | 说明 |
|----|------|------|------|------|
| D1 | `_midtrans_get` | GET | `/snap/v1/transactions/{snap}/status` | 确认 `settlement` 或 `capture` |

#### 付款后处理

**文件**：`application/gopay_pay_chatgpt.py` L640-660

```python
# 付款成功 → 查询余额 → 标记账号
patch_account_graph(account, lifecycle_status="subscribed")
```

### 3.5 辅助功能

| 功能 | 方法/行号 | 说明 |
|------|----------|------|
| 领红包补余额 | `claim_envelope_for_account()` L80-100 | 自动领 GoPay 红包 |
| 余额轮询 | `wait_for_balance()` L240-270 | 最多等 20 分钟（`PhoneTTLGuard`） |
| 自动注册 GoPay | `register_gopay_account()` L190-230 | 池里没号时自动注册 |
| 号码 TTL 守护 | `PhoneTTLGuard` L56-75 | 20 分钟超时保护 |
| 渠道感知付款 OTP | `_build_payment_sms_callbacks()` L585-640 | 使用与注册相同的 SMS 渠道 |

---

## 四、ChatGPT 邮箱注册流程（协议路径）

### 4.1 注册框架

**文件**：`core/registration/flows.py`（141行）

```python
class ProtocolMailboxFlow:
    def run(self, context: RegistrationContext) -> RegistrationResult:
        # 1. preflight: 验证身份/配置
        # 2. identity: 从邮箱池分配邮箱
        # 3. artifacts: 构造 OTP 回调 + 验证码获取器
        # 4. executor: ChatGPTProtocolMailboxWorker
        # 5. worker.register(): 执行注册
        # 6. result_mapper: 映射到 RegistrationResult
```

### 4.2 执行器模式

**文件**：`platforms/chatgpt/plugin.py` — `ChatGPTPlatform`（~300行）

| 执行器 | `supported_executors` | 说明 |
|--------|----------------------|------|
| `protocol` | ✅ | API 协议，无浏览器（默认） |
| `headless` | ✅ | 无头浏览器（Playwright） |
| `headed` | ✅ | 可见浏览器 |

### 4.3 身份模式

| 模式 | 适配器 | 说明 |
|------|--------|------|
| `mailbox` | `ProtocolMailboxAdapter` | 邮箱验证码注册 |
| `oauth_browser` | `BrowserRegistrationAdapter` | OAuth 浏览器授权 |

### 4.4 回调构建

**文件**：`core/registration/helpers.py`

```python
def build_otp_callback(identity, config, timeout=300):
    """邮箱验证码回调：IMAP 轮询 / Web API 获取验证码"""

def build_phone_callbacks(config):
    """手机 SMS 回调：herosms / smspool / smsbower"""
```

---

## 五、PayPal 纯协议路线

### 5.1 概述

PayPal 路线**完全不需要浏览器**，通过 `payment_protocol.py`（2060行）的 curl_cffi 纯协议管线完成：

```
Stripe Checkout → PayPal 登录/注册 → 授权 → Stripe 轮询 → 完成
```

### 5.2 协议管线

**文件**：`payment_protocol.py`（2060行）

**入口**：`run_protocol_checkout()`

**默认 Pipeline Stages**（`default_pipeline()`）：

| Stage | 函数 | 说明 |
|-------|------|------|
| 1 | `proto_stage_stripe_checkout` | curl_cffi 请求 Stripe Checkout，选 PayPal |
| 2 | `proto_stage_paypal_approve` | PayPal 登录/注册，处理 OTP 验证 |
| 3 | `proto_stage_paypal_signup` | PayPal 新用户注册（含手机 SMS 验证） |
| 4 | `proto_stage_paypal_authorize` | PayPal 授权支付 |
| 5 | `proto_stage_stripe_poll` | 轮询 Stripe 确认支付完成 |

### 5.3 调用示例

```python
from plus_payment import generate_plus_link, complete_paypal_checkout_protocol

# ① 生成支付链接
checkout_url = generate_plus_link(
    access_token=chatgpt_access_token,
    country="US", currency="USD"
)

# ② 纯协议 PayPal 付款（无需浏览器！）
result = complete_paypal_checkout_protocol(
    checkout_url=checkout_url,
    cookies_str=chatgpt_cookies,
    proxy="socks5h://127.0.0.1:10808",
    email="your_paypal@email.com",
    timeout=300,
)
# → {"ok": True, "status": "completed", ...}
```

### 5.4 PayPal 协议路线特性

- **零浏览器**：全程 curl_cffi HTTP 请求
- **自动注册 PayPal**：协议生成美国 VISA 卡号 + 身份信息
- **SMS 号码池**：支持传入 `sms_pool` 用于 PayPal 手机验证
- **Turnstile 验证**：可选 `turnstile_solver` 回调
- **多阶段管线**：可自定义 pipeline stages

---

## 六、接入指南

### 5.1 最小集成：GoPay 注册 → Plus 升级

```python
# ── 第一步：注册 GoPay 账号 ──
from full_pure_signup_pin import run as register_gopay

gopay = register_gopay(
    pin="123456",
    proxy="socks5h://127.0.0.1:10808",
    provider="herosms",
    api_key="YOUR_HEROSMS_KEY",
)
# → { phone: "+6281234567890", pin: "123456", token: "jwe...", balance: 10000 }

# ── 第二步：升级 ChatGPT Plus ──
from application.gopay_pay_chatgpt import GopayPayChatGPT

pipeline = GopayPayChatGPT(
    session_token=chatgpt_session_token,  # ChatGPT 已登录 token
    gopay_phone=gopay["phone"],
    gopay_pin=gopay["pin"],
    gopay_token=gopay["token"],
    sms_api_key="YOUR_HEROSMS_KEY",
    sms_activation_id=gopay["activation_id"],  # 复用已买的号码
    proxy="socks5h://127.0.0.1:10808",
    browser_mode="camoufox_headless",
)
result = pipeline.run()
# → { subscribed: True, order_id: "order_xxx", account_id: "acc_xxx" }
```

### 5.2 依赖安装

```bash
pip install curl-cffi requests pycryptodome fastapi uvicorn sqlmodel playwright
playwright install chromium

# 反检测浏览器（可选）
pip install camoufox
```

### 5.3 完整配置示例

```json
{
  "gopay": {
    "pin": "123456",
    "proxy": "socks5h://127.0.0.1:10808",
    "sms_provider": "herosms",
    "sms_api_key": "YOUR_KEY",
    "sms_country": "6",
    "sms_service": "ni"
  },
  "chatgpt": {
    "session_token": "eyJhbG...",
    "price_id": "price_1Qxxx",
    "country": "ID",
    "currency": "IDR"
  },
  "browser": {
    "mode": "camoufox_headless",
    "headless": true,
    "timeout": 300
  },
  "payment": {
    "midtrans_base": "https://app.midtrans.com",
    "gopay_base": "https://gopay.co.id",
    "poll_interval": 5,
    "max_poll_seconds": 300
  }
}
```

### 5.4 Docker 部署

**文件**：`Dockerfile` / `docker-compose.yml`

```bash
# 构建
docker build -t abai-autoplus .

# 启动
docker-compose up -d

# API 地址
http://localhost:8000

# Web UI
http://localhost:5173
```

### 5.5 环境变量

**文件**：`.env.example`

| 变量 | 说明 |
|------|------|
| `DATABASE_URL` | SQLite 路径，默认 `account_manager.db` |
| `HEROSMS_API_KEY` | HeroSMS API Key |
| `SMSBOWER_API_KEY` | SMSBower API Key |
| `SMSPOOL_API_KEY` | SMSPool API Key |
| `PROXY_POOL_URL` | 动态代理 API 地址 |
| `YESCAPTCHA_KEY` | YesCaptcha Key |
| `ANY2API_URL` | Any2API 网关地址 |

---

## 六、错误处理与重试

### 6.1 GoPay 注册重试

**文件**：`platforms/gopay/plugin.py` L204-230

| 场景 | 重试次数 | 行为 |
|------|---------|------|
| `Already registered` | 最多 5 次 | 换号码重新注册 |
| 网络错误 | 3 次 | 指数退避 |
| WAF 拦截 (HTML 返回) | 3 次 | 换 IP/代理 |

### 6.2 支付重试

**文件**：`application/gopay_pay_chatgpt.py`

| 步骤 | 重试策略 |
|------|---------|
| 生成链接 | 3 次，指数退避（TLS 竞态） |
| Midtrans OTP 验证 | 3 次（代码错误/过期） |
| 扣款 | 1 次（失败即标 `FAILED`，避免重复扣款） |
| 余额不足 | 自动领红包 → 重试 5 次 |

---

## 七、已知平台

| 平台 | 协议 | 浏览器 | OAuth | 特殊操作 |
|------|:----:|:------:|:-----:|---------|
| ChatGPT | ✅ | ✅ | ✅ | Plus 付款 |
| Cursor | ✅ | ✅ | ✅ | 需手机验证 |
| Kiro | ✅ | ✅ | ✅ | 账号切换 |
| Grok | ✅ | ✅ | ✅ | — |
| Windsurf | ✅ | ✅ | ✅ | Trial 链接 |
| Trae.ai | ✅ | ✅ | ✅ | Pro 升级链接 |
| Tavily | ✅ | ✅ | ✅ | — |
| Blink | ✅ | ✅ | ✅ | — |
| Cerebras | ✅ | ✅ | ✅ | — |
| OpenBlockLabs | ✅ | ✅ | ✅ | — |
| GoPay | ✅ | — | — | 手机+PIN，付款 Plus |
| Anything | ✅ | ✅ | — | 通用适配器 |

---

## 八、安全说明

> ⚠️ **开源前审计检查清单**（来自 `OPEN_SOURCE_RELEASE.md`）：

| 风险级别 | 类型 | 处理 |
|---------|------|------|
| 🔴 P0 | `acc.json` / `acc81.json` — 真实账号凭证（密码、session_token、cookies） | 立即删除 + git 重写历史 + 轮换 |
| 🔴 P0 | `otp_*.txt` / `har_*.txt` — 调试 dump（含第三方实时 cookie/PII） | 立即删除 + git 重写历史 |
| 🟠 P1 | `gopay_protocol.py` — 硬编码 HeroSMS API Key（`FILL_YOUR_OWN`） | 改为环境变量 |
| 🟡 P2 | `ENHANCED_KEY` — APK 逆向提取的 HMAC 密钥 | 评估法律风险 |
| 🟡 P2 | `config/database.py` — 默认 `admin` / `admin123` | 启动校验 + 文档提醒 |

---

## 九、致谢

- [lxf746/any-auto-register](https://github.com/lxf746/any-auto-register) — 插件化注册框架
- [asz798838958/aBaiAutoplus](https://github.com/asz798838958/aBaiAutoplus) — GoPay + Plus 扩展
- Playwright / Camoufox — 浏览器自动化
- Midtrans API — GoPay 支付网关
