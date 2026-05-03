# 模型配置与更换教程

> 本文档说明如何为 Monica Server 配置 API Key、切换默认模型、更换请求地址（代理/中转）、按需指定模型，以及如何接入新的大模型提供商。

---

## 目录

- [当前支持的模型](#当前支持的模型)
- [配置 API Key](#配置-api-key)
- [更换默认模型](#更换默认模型)
- [更换请求地址（代理 / 中转 / 其他厂商）](#更换请求地址代理--中转--其他厂商)
- [创建任务时指定模型](#创建任务时指定模型)
- [自动降级机制](#自动降级机制)
- [接入新模型提供商](#接入新模型提供商)
  - [接入 DeepSeek（兼容 OpenAI 格式）](#接入-deepseek兼容-openai-格式)
  - [接入 Claude（Anthropic）](#接入-claudeanthropic)
  - [接入国内模型（通义千问 / 文心等）](#接入国内模型通义千问--文心等)

---

## 当前支持的模型

| 模型名（传参用） | 提供商 | 特点 | 推荐场景 |
|------------------|--------|------|----------|
| `gpt-4o` | OpenAI | 最强多模态，支持 CT 图像 | 精度优先 |
| `gpt-4o-mini` | OpenAI | 成本低，速度快 | 日常分析 |
| `gpt-4` | OpenAI | 纯文本，较稳定 | 无图像场景 |
| `gpt-3.5-turbo` | OpenAI | 最便宜 | 测试调试 |
| `gemini-1.5-pro` | Google | 强多模态，长上下文 | 精度优先 + 节省成本 |
| `gemini-1.5-flash` | Google | 速度极快，成本最低 | 高并发 / 低配服务器 |
| `gemini-pro` | Google | 旧版 Gemini | 兼容旧接口 |

> **2C2G 服务器推荐**：`gemini-1.5-flash`（速度最快、内存占用最低）或 `gpt-4o-mini`

---

## 配置 API Key

所有 Key 统一写在项目根目录的 `.env` 文件中：

```bash
# 如果还没有 .env，先复制模板
cp .env.example .env

# 编辑
nano .env
```

找到 LLM 相关配置项，填入你的 Key：

```dotenv
# OpenAI —— https://platform.openai.com/api-keys
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Google Gemini —— https://aiskudio.google.com/app/apikey
GEMINI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 全局默认模型（不传参时使用）
DEFAULT_MODEL=gpt-4o
```

**修改后无需重新安装依赖，重启服务即可生效：**

```bash
# 重启 FastAPI（开发环境 --reload 模式会自动热重载）
pkill -f uvicorn && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 生产环境（Supervisor）
sudo supervisorctl restart monica:fastapi
sudo supervisorctl restart monica:arq_worker
```

---

## 更换默认模型

修改 `.env` 中的 `DEFAULT_MODEL`，重启服务后所有未指定模型的任务都会使用新模型：

```dotenv
# 改为 Gemini Flash（速度最快、最省钱）
DEFAULT_MODEL=gemini-1.5-flash

# 或改为 GPT-4o-mini（OpenAI 性价比最高）
DEFAULT_MODEL=gpt-4o-mini
```

---

## 更换请求地址（代理 / 中转 / 其他厂商）

### 背景

默认情况下，系统直连 OpenAI 和 Google 官方接口。在以下场景需要更换地址：

- 国内服务器无法直连 OpenAI / Gemini，需要使用**代理或中转服务**
- 使用 **DeepSeek、通义千问、月之暗面** 等兼容 OpenAI 格式的第三方模型
- 自建或租用 **API 网关**统一管理多个 Key

### 配置方式

请求地址统一通过 `.env` 配置，**无需修改任何代码**：

```dotenv
# OpenAI 兼容接口地址（适用于 OpenAI / DeepSeek / 通义千问 / 月之暗面 / 智谱等）
OPENAI_BASE_URL=https://api.openai.com/v1/chat/completions

# Google Gemini 接口地址
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/models
```

修改后重启服务即可生效（无需重装依赖）：

```bash
# 开发环境（--reload 模式下保存 .env 需手动重启）
pkill -f uvicorn && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# 生产环境
sudo supervisorctl restart monica:fastapi
sudo supervisorctl restart monica:arq_worker
```

### 常见替换场景

**场景1：国内服务器使用 OpenAI 代理**

```dotenv
OPENAI_BASE_URL=https://api.your-proxy.com/v1/chat/completions
OPENAI_API_KEY=sk-your-openai-key
DEFAULT_MODEL=gpt-4o
```

**场景2：切换为 DeepSeek（无需改代码，只改地址和 Key）**

```dotenv
OPENAI_BASE_URL=https://api.deepseek.com/v1/chat/completions
OPENAI_API_KEY=sk-your-deepseek-key
DEFAULT_MODEL=deepseek-chat
```

**场景3：切换为通义千问**

```dotenv
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
OPENAI_API_KEY=sk-your-dashscope-key
DEFAULT_MODEL=qwen-max
```

**场景4：切换为月之暗面 Moonshot**

```dotenv
OPENAI_BASE_URL=https://api.moonshot.cn/v1/chat/completions
OPENAI_API_KEY=sk-your-moonshot-key
DEFAULT_MODEL=moonshot-v1-8k
```

**场景5：切换为智谱 GLM**

```dotenv
OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4/chat/completions
OPENAI_API_KEY=your-zhipu-key
DEFAULT_MODEL=glm-4
```

**场景6：Gemini 使用代理转发**

```dotenv
GEMINI_BASE_URL=https://your-proxy.com/gemini/v1beta/models
GEMINI_API_KEY=AIza-your-key
DEFAULT_MODEL=gemini-1.5-flash
```

### 所有兼容 OpenAI 格式的厂商地址一览

| 厂商 | 官方地址 | 默认模型名示例 |
|------|----------|----------------|
| OpenAI | `https://api.openai.com/v1/chat/completions` | `gpt-4o` |
| DeepSeek | `https://api.deepseek.com/v1/chat/completions` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | `qwen-max` |
| 月之暗面 | `https://api.moonshot.cn/v1/chat/completions` | `moonshot-v1-8k` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4/chat/completions` | `glm-4` |
| 零一万物 | `https://api.lingyiwanwu.com/v1/chat/completions` | `yi-large` |
| MiniMax | `https://api.minimax.chat/v1/text/chatcompletion_v2` | `abab6.5s-chat` |

> ⚠️ 上述厂商均兼容 OpenAI 消息格式，可直接替换 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 使用，**无需修改代码**。但需将对应模型名加入 `_OPENAI_MODELS` 集合（参见[接入新模型提供商](#接入新模型提供商)中的 Step 3）。

---

## 创建任务时指定模型

每次创建分析任务时可以单独指定模型，优先级高于 `DEFAULT_MODEL`。

**通过 API（`POST /analysis/create`）：**

```json
{
  "file_id": "your_file_id",
  "scan_type": "CT",
  "clinical_notes": "患者吸烟史20年",
  "model": "gemini-1.5-flash"
}
```

**通过内置测试台（`http://localhost:8000/test`）：**

进入「创建任务」页面，在「LLM 模型」下拉框中选择目标模型即可。

---

## 自动降级机制

当指定模型调用失败（超时、余额不足、API 故障等），系统会**自动按以下顺序依次降级**，无需人工干预：

```
gpt-4o  →  gpt-4o-mini  →  gemini-1.5-pro  →  gemini-1.5-flash
```

- 从你指定（或默认）的模型开始，失败后自动尝试链中下一个
- 日志中会打印降级信息：`[LLM] 降级到 gemini-1.5-pro 成功`
- 所有模型均失败时任务状态变为 `error`，报告降级填充兜底文案

**修改降级链顺序**（`app/services/llm_service.py`，第 27-32 行）：

```python
_MODEL_CHAIN: List[str] = [
    "gpt-4o",
    "gpt-4o-mini",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
```

直接调整列表顺序，保存后重启服务。

---

## 接入新模型提供商

以下三种场景按步骤操作，**每种都需要修改同一个文件**：`app/services/llm_service.py`。

---

### 接入 DeepSeek（兼容 OpenAI 格式）

DeepSeek 的 API 与 OpenAI 完全兼容，改动最小。

**Step 1：在 `.env` 中添加 Key**

```dotenv
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

**Step 2：在 `app/config.py` 中注册配置项**

```python
# LLM
OPENAI_API_KEY: str = ""
GEMINI_API_KEY: str = ""
DEEPSEEK_API_KEY: str = ""          # ← 新增这一行
DEFAULT_MODEL: str = "gpt-4o"
```

**Step 3：在 `app/services/llm_service.py` 中注册模型**

```python
# 在文件顶部，找到这两个集合，添加 DeepSeek 模型名：
_OPENAI_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-3.5-turbo",
    "deepseek-chat", "deepseek-reasoner",   # ← 新增
}

# 加入降级链（放在你希望的优先级位置）：
_MODEL_CHAIN: List[str] = [
    "gpt-4o",
    "gpt-4o-mini",
    "deepseek-chat",        # ← 新增
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
```

**Step 4：修改 `_call_openai` 方法，根据模型名切换 Base URL**

找到 `_call_openai` 方法（约第 106 行），将请求地址改为动态选择：

```python
async def _call_openai(self, model, messages, response_format, max_tokens, temperature):
    # 根据模型名选择 Base URL 和 API Key
    if model.startswith("deepseek"):
        base_url = "https://api.deepseek.com/v1/chat/completions"
        api_key  = settings.DEEPSEEK_API_KEY
    else:
        base_url = "https://api.openai.com/v1/chat/completions"
        api_key  = settings.OPENAI_API_KEY

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if response_format == "json_object":
        body["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        total_tok = data.get("usage", {}).get("total_tokens", 0)
        return text, total_tok
```

完成后设置 `.env`：

```dotenv
DEFAULT_MODEL=deepseek-chat
DEEPSEEK_API_KEY=sk-your-key
```

---

### 接入 Claude（Anthropic）

Claude 的消息格式与 OpenAI 略有差异（`system` 字段独立）。

**Step 1：安装 Anthropic SDK（可选，也可直接用 httpx）**

```bash
pip install anthropic
# 并在 requirements.txt 末尾添加：
# anthropic>=0.25.0
```

**Step 2：在 `.env` 和 `config.py` 中添加 Key**

```dotenv
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx
```

```python
# config.py
ANTHROPIC_API_KEY: str = ""
```

**Step 3：在 `app/services/llm_service.py` 中注册并实现**

在文件顶部添加：

```python
_CLAUDE_MODELS = {
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
}
```

更新模型集合和降级链：

```python
_MODEL_CHAIN: List[str] = [
    "gpt-4o",
    "claude-3-5-sonnet-20241022",   # ← 新增
    "gpt-4o-mini",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
]
```

在 `_call_with_retry` 方法中添加路由分支：

```python
async def _call_with_retry(self, model, messages, response_format, max_tokens, temperature):
    if model in _OPENAI_MODELS:
        return await self._call_openai(model, messages, response_format, max_tokens, temperature)
    elif model in _CLAUDE_MODELS:                          # ← 新增分支
        return await self._call_claude(model, messages, response_format, max_tokens, temperature)
    else:
        return await self._call_gemini(model, messages, response_format, max_tokens, temperature)
```

新增 `_call_claude` 方法：

```python
@retry(
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
)
async def _call_claude(self, model, messages, response_format, max_tokens, temperature):
    # 提取 system 消息（Claude 要求独立传入）
    system_text = ""
    user_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"]
        else:
            user_messages.append(msg)

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": user_messages,
    }
    if system_text:
        body["system"] = system_text

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
        usage = data.get("usage", {})
        total_tok = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return text, total_tok
```

---

### 接入国内模型（通义千问 / 文心等）

大多数国内模型也兼容 OpenAI 格式，参考 DeepSeek 方案修改 `base_url` 和 `api_key` 即可：

| 模型 | Base URL | Key 变量名 |
|------|----------|-----------|
| 通义千问 (qwen) | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | `DASHSCOPE_API_KEY` |
| 文心一言 (ernie) | `https://aip.baidubce.com/rpc/2.0/ai_custom/v1/...` | `ERNIE_API_KEY` |
| 月之暗面 (moonshot) | `https://api.moonshot.cn/v1/chat/completions` | `MOONSHOT_API_KEY` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4/chat/completions` | `ZHIPU_API_KEY` |

以通义千问为例，在 `_call_openai` 中添加分支：

```python
if model.startswith("qwen"):
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    api_key  = settings.DASHSCOPE_API_KEY
```

---

## 快速验证模型是否配置成功

启动服务后，访问测试台 `http://localhost:8000/test`：

1. 进入「身份认证」页 → 点击「登录」获取 Token
2. 进入「原始请求」页，发送一个简单的分析任务创建请求：

```json
POST /analysis/create
{
  "file_id": "test",
  "scan_type": "CT",
  "model": "gemini-1.5-flash"
}
```

3. 查看返回的 `task_id`，进入「SSE 进度」页连接查看任务执行日志
4. Worker 日志中会显示 `[LLM] 降级到 xxx 成功` 或直接成功的模型名称
