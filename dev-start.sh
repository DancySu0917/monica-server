#!/usr/bin/env bash
# Monica Server — 本地开发脚本
#
# 用法：
#   ./dev-start.sh            启动所有服务
#   ./dev-start.sh stop       停止所有服务
#   ./dev-start.sh restart    重启所有服务
#   ./dev-start.sh status     查看状态
#   ./dev-start.sh logs [app|worker|all]  查看日志
#   ./dev-start.sh clean      清理环境（venv + 数据）
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PID_DIR="${SCRIPT_DIR}/.dev-pids"
LOG_DIR="${SCRIPT_DIR}/.dev-logs"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_LOCAL="${SCRIPT_DIR}/.env.local"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[1m'; N='\033[0m'
ok()   { echo -e "  ${G}✓${N}  $*"; }
warn() { echo -e "  ${Y}!${N}  $*"; }
err()  { echo -e "  ${R}✗${N}  $*" >&2; exit 1; }
info() { echo -e "  ${B}·${N}  $*"; }

# ── 加载 .env.local ──────────────────────────────────────────
_load_env() {
    if [ -f "$ENV_LOCAL" ]; then
        set -a
        # shellcheck disable=SC1090
        source <(grep -v '^#' "$ENV_LOCAL" | grep -v '^$')
        set +a
    fi
}

# ── 确保 .env.local 存在 ─────────────────────────────────────
_ensure_env() {
    [ -f "$ENV_LOCAL" ] && return

    info "首次运行，生成 .env.local ..."

    local secret
    secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # 从 .env / .env.example 继承配置
    local llm_url llm_key llm_model wx_appid wx_secret
    llm_url=""   llm_key=""   llm_model=""
    wx_appid=""  wx_secret=""
    for f in "$ENV_FILE" "${SCRIPT_DIR}/.env.example"; do
        [ -f "$f" ] || continue
        [ -z "$llm_url"   ] && llm_url=$(grep -E '^LLM_BASE_URL=' "$f"  | cut -d= -f2- | xargs 2>/dev/null || true)
        [ -z "$llm_key"   ] && llm_key=$(grep -E '^LLM_API_KEY='  "$f"  | cut -d= -f2- | xargs 2>/dev/null || true)
        [ -z "$llm_model" ] && llm_model=$(grep -E '^LLM_MODEL='  "$f"  | cut -d= -f2- | xargs 2>/dev/null || true)
        [ -z "$wx_appid"  ] && wx_appid=$(grep -E '^WX_APPID='    "$f"  | cut -d= -f2- | xargs 2>/dev/null || true)
        [ -z "$wx_secret" ] && wx_secret=$(grep -E '^WX_SECRET='   "$f" | cut -d= -f2- | xargs 2>/dev/null || true)
    done

    cat > "$ENV_LOCAL" <<EOF
# Monica Server — 本地开发配置（不要提交到 Git）

SECRET_KEY=${secret}

# 微信小程序
WX_APPID=${wx_appid:-your_wx_appid}
WX_SECRET=${wx_secret:-your_wx_secret}

# 数据库
DATABASE_URL=sqlite:///./monica.db

# Redis
REDIS_URL=redis://localhost:6379/0

# LLM
LLM_BASE_URL=${llm_url:-https://yundou.ai/v1}
LLM_API_KEY=${llm_key:-your_api_key_here}
LLM_MODEL=${llm_model:-gpt-5.4}

# 存储
STORAGE_ROOT=./storage
MAX_UPLOAD_SIZE_MB=500
CHUNK_SIZE_MB=5

# 性能
ARQ_MAX_JOBS=2
ARQ_JOB_TIMEOUT=600
DICOM_BATCH_SIZE=20
TOP_K_SLICES=10
TOTALSEG_FAST=true

# CORS
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000

# 配额
DAILY_TOKEN_LIMIT=200000

# 开发模式（允许 code="monica-code" 绕过微信鉴权）
DEV_MODE=true
EOF

    ok ".env.local 已生成"
    [[ "${llm_key}" == *"your_api_key"* ]] || [ -z "$llm_key" ] && \
        warn "LLM_API_KEY 未填写，请编辑 .env.local"
    return 0
}

# ── Redis ────────────────────────────────────────────────────
_start_redis() {
    redis-cli ping >/dev/null 2>&1 && { ok "Redis 已在运行"; return; }

    if command -v brew &>/dev/null; then
        if ! brew list redis &>/dev/null 2>&1; then
            info "安装 Redis ..."
            brew install redis -q
        fi
        brew services start redis >/dev/null 2>&1 || true
        sleep 2
        redis-cli ping >/dev/null 2>&1 && { ok "Redis 已启动"; return; }
    fi

    command -v redis-server &>/dev/null || err "Redis 未安装，请运行：brew install redis"
    redis-server --daemonize yes \
        --logfile "${LOG_DIR}/redis.log" \
        --port 6379 --maxmemory 256mb --maxmemory-policy allkeys-lru
    sleep 1
    redis-cli ping >/dev/null 2>&1 && ok "Redis 已启动" || err "Redis 启动失败"
}

# ── FastAPI ──────────────────────────────────────────────────
_start_fastapi() {
    local pid_file="${PID_DIR}/fastapi.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        ok "FastAPI 已在运行 (PID=$(cat "$pid_file"))"; return
    fi
    nohup env $(grep -v '^#' "$ENV_LOCAL" | grep -v '^$' | xargs) \
        "${VENV_DIR}/bin/uvicorn" app.main:app \
        --host 0.0.0.0 --port 8000 \
        --workers 1 --loop asyncio --log-level info --reload \
        > "${LOG_DIR}/fastapi.log" 2>"${LOG_DIR}/fastapi.err" &
    echo $! > "$pid_file"
    ok "FastAPI 已启动 (PID=$!)"
}

# ── ARQ Worker ───────────────────────────────────────────────
_start_worker() {
    local pid_file="${PID_DIR}/arq_worker.pid"
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        ok "ARQ Worker 已在运行 (PID=$(cat "$pid_file"))"; return
    fi
    nohup env $(grep -v '^#' "$ENV_LOCAL" | grep -v '^$' | xargs) \
        "${VENV_DIR}/bin/watchfiles" --filter python \
        "arq app.workers.arq_worker.WorkerSettings" \
        "${SCRIPT_DIR}/app" \
        > "${LOG_DIR}/arq_worker.log" 2>"${LOG_DIR}/arq_worker.err" &
    echo $! > "$pid_file"
    ok "ARQ Worker 已启动 (PID=$!)"
}

# ════════════════════════════════════════════════════════════
# 命令：start
# ════════════════════════════════════════════════════════════
cmd_start() {
    echo ""
    echo -e "${B}Monica Server — 启动中${N}"
    echo ""

    mkdir -p "$PID_DIR" "$LOG_DIR"

    # Python
    local py
    for v in python3.12 python3.11 python3; do
        command -v "$v" &>/dev/null && { py="$v"; break; }
    done
    [ -z "${py:-}" ] && err "未找到 Python3，请安装：brew install python@3.11"
    $py -c "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)" \
        || err "Python 版本过低（需要 >= 3.11）：brew install python@3.11"
    ok "Python $($py -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

    # venv
    if [ ! -d "$VENV_DIR" ]; then
        info "创建虚拟环境 ..."
        $py -m venv "$VENV_DIR"
    fi

    # 依赖（仅 requirements.txt 更新时重装）
    local marker="${VENV_DIR}/.installed"
    if [ ! -f "$marker" ] || [ "${SCRIPT_DIR}/requirements.txt" -nt "$marker" ]; then
        info "安装依赖（首次较慢）..."
        "${VENV_DIR}/bin/pip" install --upgrade pip -q
        "${VENV_DIR}/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt" \
            -i https://mirrors.aliyun.com/pypi/simple/ -q
        touch "$marker"
        ok "依赖安装完成"
    else
        ok "依赖已是最新"
    fi

    # 配置
    _ensure_env
    _load_env

    # 存储目录
    mkdir -p "${SCRIPT_DIR}/storage/"{uploads,processed,exports,chunks}

    # 启动各服务
    _start_redis
    _start_fastapi
    _start_worker

    # 等待 FastAPI 就绪
    local i=0
    echo -n "  · 等待服务就绪"
    until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
        i=$((i+1)); [ $i -ge 30 ] && { echo ""; err "启动超时，查看日志：./dev-start.sh logs"; }
        echo -n "."; sleep 2
    done
    echo ""

    echo ""
    echo -e "  ${G}${B}✓ 启动完成${N}"
    echo ""
    echo -e "  测试台  → ${G}http://localhost:8000/test${N}"
    echo -e "  健康检查→ ${G}http://localhost:8000/health${N}"
    echo ""
    echo -e "  日志    → ./dev-start.sh logs [app|worker|all]"
    echo -e "  状态    → ./dev-start.sh status"
    echo -e "  停止    → ./dev-start.sh stop"
    echo ""
}

# ════════════════════════════════════════════════════════════
# 命令：stop
# ════════════════════════════════════════════════════════════
cmd_stop() {
    local found=0
    for pid_file in "${PID_DIR}"/*.pid; do
        [ -f "$pid_file" ] || continue
        local name pid
        name=$(basename "$pid_file" .pid)
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && ok "已停止 ${name} (PID=${pid})"
        else
            warn "${name} 已不在运行"
        fi
        rm -f "$pid_file"
        found=$((found+1))
    done
    [ $found -eq 0 ] && warn "没有找到运行中的服务"
}

# ════════════════════════════════════════════════════════════
# 命令：status
# ════════════════════════════════════════════════════════════
cmd_status() {
    echo ""
    for pid_file in "${PID_DIR}"/*.pid; do
        [ -f "$pid_file" ] || continue
        local name pid
        name=$(basename "$pid_file" .pid)
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            ok "${name}  (PID=${pid})"
        else
            warn "${name}  已停止"
        fi
    done
    redis-cli ping >/dev/null 2>&1 && ok "Redis  PONG" || warn "Redis  未响应"
    local h
    h=$(curl -sf http://localhost:8000/health 2>/dev/null || true)
    if [ -n "$h" ]; then
        ok "API    http://localhost:8000/test"
    else
        warn "API    未响应（./dev-start.sh logs 查看错误）"
    fi
    echo ""
}

# ════════════════════════════════════════════════════════════
# 命令：logs
# ════════════════════════════════════════════════════════════
cmd_logs() {
    case "${1:-app}" in
        app|fastapi|api)
            tail -f "${LOG_DIR}/fastapi.log" "${LOG_DIR}/fastapi.err" 2>/dev/null \
                || err "日志不存在，请先运行 ./dev-start.sh"
            ;;
        worker|arq)
            tail -f "${LOG_DIR}/arq_worker.log" "${LOG_DIR}/arq_worker.err" 2>/dev/null \
                || err "日志不存在"
            ;;
        all)
            tail -f "${LOG_DIR}/fastapi.log" "${LOG_DIR}/arq_worker.log" 2>/dev/null \
                || err "日志不存在"
            ;;
        *)
            echo "用法: $0 logs [app|worker|all]"
            ;;
    esac
}

# ════════════════════════════════════════════════════════════
# 命令：clean
# ════════════════════════════════════════════════════════════
cmd_clean() {
    warn "将删除：.venv/  .dev-pids/  .dev-logs/  storage/  monica.db  .env.local"
    read -rp "  确认？(yes/n): " c
    [ "$c" = "yes" ] || { info "已取消"; exit 0; }
    cmd_stop 2>/dev/null || true
    rm -rf "$VENV_DIR" "$PID_DIR" "$LOG_DIR" "${SCRIPT_DIR}/storage"
    rm -f "${SCRIPT_DIR}/monica.db" "${SCRIPT_DIR}/.env.local"
    ok "清理完成"
}

# ════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════
case "${1:-start}" in
    start)              cmd_start ;;
    stop)               cmd_stop ;;
    restart)            cmd_stop 2>/dev/null || true; sleep 1; cmd_start ;;
    status)             cmd_status ;;
    logs)               shift; cmd_logs "${1:-app}" ;;
    clean)              cmd_clean ;;
    help|-h|--help)
        echo ""
        echo "  用法: ./dev-start.sh <命令>"
        echo ""
        echo "  start            启动所有服务（默认）"
        echo "  stop             停止所有服务"
        echo "  restart          重启所有服务"
        echo "  status           查看进程 + API 状态"
        echo "  logs [app|worker|all]  查看日志"
        echo "  clean            清理环境"
        echo ""
        ;;
    *)
        echo "未知命令：$1，运行 ./dev-start.sh help 查看帮助"
        exit 1
        ;;
esac
