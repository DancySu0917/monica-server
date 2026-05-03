# Monica Medical AI Server

> 医疗影像 AI 分析平台后端 · Python · FastAPI · ARQ · SQLite · Redis

---

## 目录

- [项目结构](#项目结构)
- [快速开始（本地开发）](#快速开始本地开发)
- [服务器部署（Docker · 推荐）](#服务器部署docker--推荐)
- [服务器部署（裸机）](#服务器部署裸机)
- [环境变量说明](#环境变量说明)
- [API 概览](#api-概览)
- [常见问题](#常见问题)

---

## 项目结构

```
monica-server/
├── app/
│   ├── main.py              # FastAPI 应用入口
│   ├── config.py            # 配置（pydantic-settings，读取 .env）
│   ├── database.py          # SQLAlchemy + SQLite WAL + sqlite-vec
│   ├── api/                 # 路由层
│   │   ├── auth.py          # POST /auth/wx_login  微信登录 + JWT
│   │   ├── upload.py        # 分片上传 init / chunk / complete
│   │   ├── analysis.py      # 创建任务 / 查询状态
│   │   ├── stream.py        # SSE 实时进度推送
│   │   ├── result.py        # 获取报告 / 切片 / 阶段详情
│   │   └── deps.py          # FastAPI 依赖（JWT 解析、DB Session）
│   ├── models/              # SQLAlchemy ORM 模型
│   ├── schemas/             # Pydantic v2 流水线阶段 Schema
│   ├── services/            # 核心业务逻辑
│   │   ├── dicom_service.py # DICOM 解析、HU 变换、pHash 去重
│   │   ├── file_service.py  # 安全解压、磁盘守卫、SHA256 校验
│   │   ├── llm_service.py   # LLM 调用，Evolink/Ollama 模型降级链
│   │   ├── quota_service.py # 每日 Token 配额（基于 Redis）
│   │   └── knowledge_service.py # 向量知识库（sqlite-vec）
│   ├── pipeline/            # 7 阶段分析流水线
│   │   ├── orchestrator.py        # 流水线调度器
│   │   ├── stage1_normalizer.py   # 标准化 / DICOM 预处理
│   │   ├── stage2_screener.py     # 质量粗筛
│   │   ├── stage3_detector.py     # 结节候选检测
│   │   ├── stage4_selector.py     # Top-K 关键切片提取
│   │   ├── stage5_context.py      # 知识库上下文注入
│   │   ├── stage6_llm.py          # CoT 大模型推理
│   │   └── stage7_storage.py      # 结构化结果落库
│   ├── evaluators/          # 独立评估器（质量 / 结节 / 报告）
│   ├── utils/
│   │   ├── thread_pool.py   # CPU 密集任务线程池
│   │   └── llm_parser.py    # 鲁棒 JSON 解析（兼容 Markdown 包裹）
│   └── workers/
│       └── arq_worker.py    # ARQ Worker 定义 + 定时清理任务
├── knowledge_base/
│   ├── cases.jsonl          # 示例病例（向量知识库种子数据）
│   └── guidelines.jsonl     # 医学指南条目
├── static/
│   └── test.html            # 内置 Web 测试台（访问 /test）
├── storage/                 # 运行时文件存储（自动创建）
│   ├── uploads/             # 原始上传文件
│   ├── processed/           # 处理后文件（PNG、元数据）
│   ├── exports/             # 导出文件
│   └── chunks/              # 分片临时文件
├── .env.example             # 环境变量模板
├── requirements.txt         # Python 依赖
├── Dockerfile               # 多阶段构建镜像
├── docker-compose.yml       # 一键编排（Redis + App + Nginx）
├── deploy.sh                # 服务器部署辅助脚本
├── supervisord.conf         # 裸机部署进程守护配置
└── nginx.conf               # 反向代理配置
```

---

## 快速开始（本地开发）

适用于本机调试，开启热重载和 Swagger 文档。

### 前置要求

| 依赖 | 版本 | 安装方式 |
|------|------|----------|
| Python | 3.11+ | [python.org](https://python.org) 或 `pyenv` |
| Redis | 6.0+ | `brew install redis`（macOS）|

### 1. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

> ⏱️ 首次安装约需 5～10 分钟（SimpleITK、OpenCV 体积较大）。

### 2. 配置环境变量

```bash
cp .env.example .env
```

打开 `.env`，修改以下关键项：

```dotenv
APP_ENV=development              # 开启 /docs Swagger 文档
SECRET_KEY=<随机字符串>           # 生成命令见下方

# LLM（至少配置一个）
EVOLINK_API_KEY=sk-...           # 生产推荐：Evolink 代理（Gemini 协议）
# 或者本地 Ollama（开发环境 APP_ENV=development 时自动切换）
OLLAMA_BASE_URL=http://localhost:11434/v1/chat/completions

# 微信小程序（留空则自动进入 dev mock 模式，无需真实微信环境）
WX_APPID=
WX_SECRET=
```

**生成 SECRET_KEY：**

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 3. 启动 Redis

```bash
# macOS
brew services start redis

# Ubuntu / Debian
sudo systemctl start redis

# 验证
redis-cli ping   # 返回 PONG 即正常
```

### 4. 启动服务（两个终端）

**终端 1 — FastAPI：**

```bash
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**终端 2 — ARQ Worker**（必须同时运行，否则分析任务不会执行）：

```bash
source .venv/bin/activate
python -m arq app.workers.arq_worker.WorkerSettings
```

### 5. 访问服务

| 地址 | 说明 |
|------|------|
| `http://localhost:8000/test` | 🎛️ 内置 Web 测试台（推荐首先访问） |
| `http://localhost:8000/docs` | 📖 Swagger 交互文档（需 `APP_ENV=development`） |
| `http://localhost:8000/health` | ❤️ 服务健康状态 |

---

## 服务器部署（Docker · 推荐）

使用 Docker Compose 一键编排，自动管理 Redis、App、Nginx，**无需手动安装 Python 环境**，适合所有 Linux 服务器。

### 前置要求

- Docker 24.0+（含 Docker Compose v2）
- 服务器开放 80 / 443 端口

**安装 Docker（Ubuntu）：**

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

### 1. 上传代码到服务器

```bash
# 方式一：scp
scp -r ./monica-server user@your-server:~/monica-server

# 方式二：Git
ssh user@your-server
git clone https://github.com/your-org/monica-server.git
cd monica-server
```

### 2. 配置环境变量

```bash
cp .env.example .env
nano .env    # 或 vim .env
```

生产环境必填项：

```dotenv
APP_ENV=production

# 生成命令：python3 -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=<强随机密钥，≥32位>

# LLM
EVOLINK_API_KEY=sk-your-key-here

# CORS（填写你的域名或小程序来源，多个用逗号分隔）
ALLOWED_ORIGINS=https://your-domain.com

# 微信小程序
WX_APPID=wx_your_appid
WX_SECRET=your_wx_secret
```

> 其余字段（`REDIS_URL`、`STORAGE_ROOT`）在 `docker-compose.yml` 中已自动覆盖为容器内正确路径，无需修改。

### 3. 一键部署

```bash
chmod +x deploy.sh
./deploy.sh
```

脚本会自动完成：构建镜像 → 拉取 Redis/Nginx → 启动服务 → 等待健康检查通过。

**常用命令：**

```bash
./deploy.sh              # 首次部署
./deploy.sh update       # 更新代码后重新构建并重启
./deploy.sh logs         # 查看实时日志
./deploy.sh status       # 查看容器状态和健康信息
./deploy.sh restart      # 重启服务（不重建镜像）
./deploy.sh stop         # 停止所有服务
```

或直接使用 docker compose：

```bash
docker compose ps                  # 查看状态
docker compose logs -f app         # 应用日志
docker compose logs -f nginx       # Nginx 日志
docker compose exec app bash       # 进入容器调试
```

### 4. 配置 HTTPS（可选但推荐）

```bash
# 安装 Certbot
sudo apt install certbot -y

# 申请证书（在 Nginx 容器停止后进行）
docker compose stop nginx
sudo certbot certonly --standalone -d your-domain.com
sudo cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ./certs/
sudo cp /etc/letsencrypt/live/your-domain.com/privkey.pem   ./certs/
docker compose start nginx
```

然后取消 `nginx.conf` 中 HTTPS 配置块的注释并重启 Nginx：

```bash
docker compose restart nginx
```

### 5. 数据持久化说明

Docker 使用三个命名 Volume 持久化数据，容器重启/重建均不丢失：

| Volume | 挂载路径 | 内容 |
|--------|----------|------|
| `storage_data` | `/opt/monica-server/storage` | DICOM、切片、上传文件 |
| `db_data` | `/opt/monica-server/db` | SQLite 数据库 |
| `redis_data` | Redis 容器内 | 任务队列、Token 配额 |

---

## 服务器部署（裸机）

适用于不方便使用 Docker 的场景，以 Ubuntu 22.04 + 2C2G 为例。

### 1. 安装系统依赖

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip \
    redis-server nginx supervisor git \
    libgl1-mesa-glx libglib2.0-0 libgomp1
```

### 2. 创建应用用户和目录

```bash
sudo useradd -r -s /sbin/nologin monica
sudo mkdir -p /opt/monica-server /var/log/monica
sudo chown monica:monica /opt/monica-server /var/log/monica
```

### 3. 部署代码

```bash
# 上传代码
scp -r ./monica-server user@your-server:/opt/monica-server

# 或 Git 克隆
sudo git clone https://github.com/your-org/monica-server.git /opt/monica-server
sudo chown -R monica:monica /opt/monica-server
```

### 4. 安装 Python 依赖

```bash
python3.11 -m venv /opt/venv
/opt/venv/bin/pip install --upgrade pip
/opt/venv/bin/pip install -r /opt/monica-server/requirements.txt
```

### 5. 配置环境变量

```bash
cd /opt/monica-server
cp .env.example .env
sudo nano .env
```

裸机部署关键配置：

```dotenv
APP_ENV=production
SECRET_KEY=<强随机密钥>
EVOLINK_API_KEY=sk-your-key-here
DATABASE_URL=sqlite:////opt/monica-server/db/monica.db
REDIS_URL=redis://127.0.0.1:6379/0
STORAGE_ROOT=/opt/monica-server/storage
ALLOWED_ORIGINS=https://your-domain.com
```

### 6. 配置 Supervisor（进程守护）

```bash
sudo cp /opt/monica-server/supervisord.conf /etc/supervisor/conf.d/monica.conf

# supervisord.conf 通过环境变量读取路径，确保设置正确
sudo bash -c 'cat >> /etc/supervisor/conf.d/monica.conf << EOF

[supervisord]
environment=APP_DIR="/opt/monica-server",APP_USER="monica"
EOF'

sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start monica:*
```

查看进程状态：

```bash
sudo supervisorctl status
# fastapi     RUNNING   pid 1234, uptime 0:00:10
# arq_worker  RUNNING   pid 1235, uptime 0:00:10
```

### 7. 配置 Nginx

```bash
sudo cp /opt/monica-server/nginx.conf /etc/nginx/nginx.conf
sudo nginx -t                    # 检查语法
sudo systemctl restart nginx
```

### 8. 查看日志

```bash
# FastAPI 和 ARQ Worker 日志（裸机模式输出到 stdout，由 supervisor 的 syslog 收集）
sudo journalctl -u supervisor -f

# 或直接查看 supervisor 管理的进程日志
sudo supervisorctl tail -f fastapi
sudo supervisorctl tail -f arq_worker
```

---

## 环境变量说明

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `APP_ENV` | `production` | 环境标识；`development` 时开启 `/docs` |
| `SECRET_KEY` | `change-me-in-production` | JWT 签名密钥，**生产必须修改** |
| `WX_APPID` | `` | 微信小程序 AppID，留空则进入 dev mock 模式 |
| `WX_SECRET` | `` | 微信小程序 Secret |
| `DATABASE_URL` | `sqlite:///./monica.db` | SQLAlchemy 数据库连接串 |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接地址 |
| `DEFAULT_MODEL` | `gemini-2.5-flash` | 默认 LLM 模型名称 |
| `EVOLINK_API_KEY` | `` | Evolink 代理 API Key（生产推荐） |
| `EVOLINK_BASE_URL` | `https://direct.evolink.ai/v1beta/models` | Evolink 请求地址 |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1/chat/completions` | Ollama 本地地址（开发环境自动生效） |
| `OLLAMA_DEFAULT_MODEL` | `amsaravi/medgemma-4b-it:q6` | Ollama 默认模型 |
| `STORAGE_ROOT` | `./storage` | 文件存储根目录（支持相对/绝对路径） |
| `MAX_UPLOAD_SIZE_MB` | `500` | 单文件最大上传大小（MB） |
| `CHUNK_SIZE_MB` | `5` | 分片大小（MB） |
| `DAILY_TOKEN_LIMIT` | `200000` | 每用户每日 Token 配额 |
| `ALLOWED_ORIGINS` | `` | CORS 允许域名，逗号分隔；留空时开发环境允许所有 |
| `ARQ_MAX_JOBS` | `1` | ARQ Worker 最大并发任务数（2C2G 建议为 1） |
| `ARQ_JOB_TIMEOUT` | `600` | 单任务最大执行时间（秒） |
| `DICOM_BATCH_SIZE` | `50` | DICOM 批处理大小 |
| `TOP_K_SLICES` | `10` | 发送给 LLM 的关键切片数 |
| `TOTALSEG_FAST` | `true` | TotalSegmentator 快速模式 |

---

## API 概览

所有接口均以 `Authorization: Bearer <JWT>` 进行鉴权（`/auth/wx_login` 除外）。

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/wx_login` | 微信 code 换取 JWT Token |

### 文件上传（分片）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/upload/init` | 初始化上传会话，获取 `upload_id` |
| PUT | `/upload/chunk?upload_id=&chunk_index=` | 上传单个分片（二进制流） |
| POST | `/upload/complete?upload_id=` | 合并分片，获取 `file_id` |
| GET | `/upload/chunks/{upload_id}` | 查询已上传分片（断点续传） |

### 分析任务

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/analysis/create` | 创建分析任务，获取 `task_id` |
| GET | `/analysis/status/{task_id}` | 轮询任务状态 |

### 实时进度

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/stream/{task_id}` | SSE 长连接，实时推送流水线进度 |

### 分析结果

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/result/{task_id}` | 获取完整分析报告 |
| GET | `/result/{task_id}/slices` | 获取关键切片图像列表 |
| GET | `/result/{task_id}/stages` | 获取各流水线阶段执行详情 |

### 系统

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 服务健康状态 + 磁盘信息 |
| GET | `/test` | 内置 Web 测试台页面 |
| GET | `/docs` | Swagger 文档（仅 `development` 环境） |

---

## 常见问题

### Q：启动报错 `redis.exceptions.ConnectionError`

Redis 未启动，执行：

```bash
brew services start redis      # macOS
sudo systemctl start redis     # Linux
# Docker 部署时 Redis 由 docker compose 自动管理，无需手动启动
```

### Q：登录接口返回 mock openid，是否正常？

正常。`WX_APPID` / `WX_SECRET` 未配置时，系统自动进入开发模式，以 `dev_<code前20位>` 作为 openid，**方便本地联调，无需真实微信环境**。

### Q：创建分析任务后状态一直是 `pending`

ARQ Worker 未启动。本地开发请在另一个终端执行：

```bash
source .venv/bin/activate
python -m arq app.workers.arq_worker.WorkerSettings
```

Docker 部署时 Worker 由 supervisord 在容器内自动启动，可通过以下命令检查：

```bash
docker compose exec app supervisorctl status
```

### Q：上传大文件时 Nginx 报 413 错误

`nginx.conf` 已设置 `client_max_body_size 512M`，若仍报错检查是否覆盖了系统级 nginx.conf：

```bash
sudo nginx -t && sudo systemctl reload nginx     # 裸机
docker compose restart nginx                      # Docker
```

### Q：如何查看运行日志？

```bash
# 本地开发：直接看终端输出

# Docker 部署：
docker compose logs -f app        # 应用 + Worker 日志
docker compose logs -f nginx      # Nginx 日志

# 裸机 Supervisor：
sudo supervisorctl tail -f fastapi
sudo supervisorctl tail -f arq_worker
```

### Q：Swagger 文档页面无法访问

将 `.env` 中 `APP_ENV` 改为 `development` 后重启服务。生产环境出于安全考虑默认关闭。

### Q：如何安装可选的 TotalSegmentator（器官分割）？

TotalSegmentator 首次下载模型约 1.5GB，2C2G 服务器**不建议启用**：

```bash
pip install TotalSegmentator==2.2.1 torch==2.2.0+cpu
```

安装后取消 `requirements.txt` 中对应行的注释，重新构建 Docker 镜像或重启服务。
