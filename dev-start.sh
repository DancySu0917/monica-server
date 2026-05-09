#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Monica Medical AI Server — macOS 本地开发验证脚本
#
# 适用于：在 Mac 笔记本上快速启动服务，验证功能是否正常
# 无需 Docker、无需 sudo、无需服务器
#
# 前提条件（自动检测）：
#   - Python 3.11+（通过 pyenv 或系统自带）
#   - Redis（通过 Homebrew 安装或已在运行）
#
# 用法：
#   ./dev-start.sh          # 启动所有服务（首次会自动安装依赖）
#   ./dev-start.sh stop     # 停止所有后台服务
#   ./dev-start.sh status   # 查看服务状态
#   ./dev-start.sh logs     # 查看 FastAPI 日志
#   ./dev-start.sh clean    # 清理 venv 和临时文件
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ── 路径配置 ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PID_DIR="${SCRIPT_DIR}/.dev-pids"
LOG_DIR="${SCRIPT_DIR}/.dev-logs"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_LOCAL_FILE="${SCRIPT_DIR}/.env.local"   # 本地开发专用配置（优先级最高）

# ── 颜色 ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()    { echo -e "\n${CYAN}${BOLD}▶ $*${NC}"; }

# ═══════════════════════════════════════════════════════════════
# start：启动所有服务
# ═══════════════════════════════════════════════════════════════
cmd_start() {
    echo -e "${CYAN}${BOLD}"
    echo "  ╔══════════════════════════════════════╗"
    echo "  ║   Monica Server — 本地开发模式启动   ║"
    echo "  ╚══════════════════════════════════════╝"
    echo -e "${NC}"

    mkdir -p "$PID_DIR" "$LOG_DIR"

    # ── 1. 检查 Python ────────────────────────────────────────
    step "检查 Python 版本"
    local PYTHON_BIN
    # 优先使用 pyenv 管理的版本，其次用系统 python3
    if command -v python3.12 &>/dev/null; then
        PYTHON_BIN="python3.12"
    elif command -v python3.11 &>/dev/null; then
        PYTHON_BIN="python3.11"
    elif command -v python3 &>/dev/null; then
        PYTHON_BIN="python3"
    else
        error "未找到 Python3，请先安装：brew install python@3.11"
    fi

    local py_ver
    py_ver=$($PYTHON_BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    $PYTHON_BIN -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" \
        || error "Python ${py_ver} 版本过低，需要 >= 3.11。安装：brew install python@3.11"
    success "Python ${py_ver} ✓  (${PYTHON_BIN})"

    # ── 2. 创建/激活 venv ────────────────────────────────────
    step "准备 Python 虚拟环境"
    if [ ! -d "$VENV_DIR" ]; then
        info "创建 venv..."
        $PYTHON_BIN -m venv "$VENV_DIR"
        success "venv 已创建: ${VENV_DIR}"
    else
        success "venv 已存在，跳过创建"
    fi

    local VENV_PYTHON="${VENV_DIR}/bin/python"
    local VENV_PIP="${VENV_DIR}/bin/pip"

    # ── 3. 安装 Python 依赖 ──────────────────────────────────
    step "安装 Python 依赖（首次较慢，后续秒级）"
    # 仅当 requirements.txt 比 venv 的 sentinel 文件新时才重装
    local sentinel="${VENV_DIR}/.installed_marker"
    if [ ! -f "$sentinel" ] || [ "${SCRIPT_DIR}/requirements.txt" -nt "$sentinel" ]; then
        info "安装中（使用阿里云镜像加速）..."
        "$VENV_PIP" install --upgrade pip -q
        "$VENV_PIP" install -r "${SCRIPT_DIR}/requirements.txt" \
            -i https://mirrors.aliyun.com/pypi/simple/ \
            --no-cache-dir -q
        touch "$sentinel"
        success "依赖安装完成"
    else
        success "依赖无变化，跳过安装"
    fi

    # ── 4. 准备 .env（本地开发版）────────────────────────────
    step "准备本地开发 .env 配置"
    _ensure_env_local
    # 将 .env.local 作为主配置（覆盖 .env）
    export $(grep -v '^#' "$ENV_LOCAL_FILE" | grep -v '^$' | xargs) 2>/dev/null || true
    success ".env.local 已加载"

    # ── 5. 启动 Redis ────────────────────────────────────────
    step "启动 Redis"
    _start_redis

    # ── 6. 确保存储目录存在 ──────────────────────────────────
    step "创建本地存储目录"
    mkdir -p \
        "${SCRIPT_DIR}/storage/uploads" \
        "${SCRIPT_DIR}/storage/processed" \
        "${SCRIPT_DIR}/storage/exports" \
        "${SCRIPT_DIR}/storage/chunks"
    success "存储目录已就绪: ${SCRIPT_DIR}/storage/"

    # ── 7. 启动 FastAPI ──────────────────────────────────────
    step "启动 FastAPI 应用服务"
    _start_fastapi

    # ── 8. 启动 ARQ Worker ───────────────────────────────────
    step "启动 ARQ 任务队列 Worker"
    _start_arq_worker

    # ── 9. 等待 FastAPI 就绪 ─────────────────────────────────
    step "等待服务就绪"
    local i=0
    echo -n "  "
    until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
        i=$((i+1))
        [ $i -ge 30 ] && {
            echo ""
            error "服务启动超时！请查看日志：./dev-start.sh logs"
        }
        echo -n "."
        sleep 2
    done
    echo ""
    success "FastAPI 服务已就绪 ✓"

    # ── 完成 ─────────────────────────────────────────────────
    echo ""
    echo -e "${GREEN}${BOLD}════════════════════════════════════════${NC}"
    echo -e "${GREEN}${BOLD}  ✅ Monica Server 本地启动完成！${NC}"
    echo -e "${GREEN}${BOLD}════════════════════════════════════════${NC}"
    echo ""
    echo -e "  🧪 测试台    : ${GREEN}http://localhost:8000/test${NC}"
    echo -e "  🔍 健康检查  : ${GREEN}http://localhost:8000/health${NC}"
    echo ""
    echo -e "  📋 查看日志  : ${CYAN}./dev-start.sh logs${NC}"
    echo -e "  📊 服务状态  : ${CYAN}./dev-start.sh status${NC}"
    echo -e "  ⏹  停止服务  : ${CYAN}./dev-start.sh stop${NC}"
    echo ""
    echo -e "  ${YELLOW}提示：服务在后台运行，关闭终端不会停止${NC}"
    echo ""
}

# ═══════════════════════════════════════════════════════════════
# stop：停止所有本地服务
# ═══════════════════════════════════════════════════════════════
cmd_stop() {
    info "停止本地开发服务..."

    local stopped=0

    for pid_file in "${PID_DIR}"/*.pid; do
        [ -f "$pid_file" ] || continue
        local name; name=$(basename "$pid_file" .pid)
        local pid; pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && success "已停止 ${name} (PID=${pid})"
        else
            warn "${name} 进程不存在 (PID=${pid}，可能已停止)"
        fi
        rm -f "$pid_file"
        stopped=$((stopped+1))
    done

    # 停止本脚本启动的 Redis（如果是通过 redis-server 前台启动的）
    if [ -f "${PID_DIR}/redis.pid" ]; then
        local rpid; rpid=$(cat "${PID_DIR}/redis.pid")
        kill -0 "$rpid" 2>/dev/null && kill "$rpid" && success "已停止 Redis (PID=${rpid})"
        rm -f "${PID_DIR}/redis.pid"
    fi

    if [ $stopped -eq 0 ]; then
        warn "没有找到正在运行的本地服务（PID 文件不存在）"
    fi
}

# ═══════════════════════════════════════════════════════════════
# status：查看状态
# ═══════════════════════════════════════════════════════════════
cmd_status() {
    echo ""
    echo -e "${BLUE}${BOLD}══════════════════ 进程状态 ══════════════════${NC}"

    local all_ok=true
    for pid_file in "${PID_DIR}"/*.pid; do
        [ -f "$pid_file" ] || continue
        local name; name=$(basename "$pid_file" .pid)
        local pid; pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo -e "  ✅  ${GREEN}${name}${NC}  PID=${pid}"
        else
            echo -e "  ❌  ${RED}${name}${NC}  PID=${pid} (已停止)"
            all_ok=false
        fi
    done

    # 检查 Redis
    echo ""
    if redis-cli ping >/dev/null 2>&1; then
        echo -e "  ✅  ${GREEN}Redis${NC}  $(redis-cli ping)"
    else
        echo -e "  ❌  ${RED}Redis${NC}  未响应"
        all_ok=false
    fi

    echo ""
    echo -e "${BLUE}${BOLD}══════════════════ API 健康检查 ═══════════════${NC}"
    local health
    health=$(curl -sf http://localhost:8000/health 2>/dev/null || echo "")
    if [ -n "$health" ]; then
        echo "  $health" | python3 -m json.tool 2>/dev/null || echo "  $health"
        echo -e "  ✅  ${GREEN}API 服务正常${NC}  http://localhost:8000/test"
    else
        echo -e "  ❌  ${RED}API 服务未响应${NC}  请运行 ./dev-start.sh logs 查看错误"
    fi
    echo ""
}

# ═══════════════════════════════════════════════════════════════
# logs：查看日志
# ═══════════════════════════════════════════════════════════════
cmd_logs() {
    local target="${1:-fastapi}"
    case "$target" in
        fastapi|app|api)
            info "📋 FastAPI 日志 (Ctrl+C 退出)"
            tail -f "${LOG_DIR}/fastapi.log" "${LOG_DIR}/fastapi.err" 2>/dev/null \
                || error "日志不存在，请先运行 ./dev-start.sh"
            ;;
        worker|arq)
            info "📋 ARQ Worker 日志 (Ctrl+C 退出)"
            tail -f "${LOG_DIR}/arq_worker.log" "${LOG_DIR}/arq_worker.err" 2>/dev/null \
                || error "日志不存在"
            ;;
        all)
            info "📋 所有日志 (Ctrl+C 退出)"
            tail -f \
                "${LOG_DIR}/fastapi.log" \
                "${LOG_DIR}/arq_worker.log" 2>/dev/null \
                || error "日志不存在"
            ;;
        *)
            echo "用法: $0 logs [app|worker|all]"
            ;;
    esac
}

# ═══════════════════════════════════════════════════════════════
# clean：清理开发环境
# ═══════════════════════════════════════════════════════════════
cmd_clean() {
    warn "将清理：.venv/ .dev-pids/ .dev-logs/ storage/ monica.db"
    read -rp "确认清理？(输入 yes 继续): " confirm
    [ "$confirm" = "yes" ] || { info "已取消"; exit 0; }

    cmd_stop 2>/dev/null || true
    rm -rf "$VENV_DIR" "$PID_DIR" "$LOG_DIR"
    rm -rf "${SCRIPT_DIR}/storage"
    rm -f "${SCRIPT_DIR}/monica.db" "${SCRIPT_DIR}/.env.local"
    success "清理完成"
}

# ═══════════════════════════════════════════════════════════════
# 内部函数
# ═══════════════════════════════════════════════════════════════

_ensure_env_local() {
    if [ -f "$ENV_LOCAL_FILE" ]; then
        success ".env.local 已存在，跳过创建"
        return
    fi

    info "生成本地开发专用 .env.local..."

    # 自动生成一个随机 SECRET_KEY
    local secret_key
    secret_key=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    # 从 .env 或 .env.example 读取 EVOLINK_API_KEY
    local evolink_key=""
    if [ -f "$ENV_FILE" ]; then
        evolink_key=$(grep -E '^EVOLINK_API_KEY=' "$ENV_FILE" | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
    fi
    if [ -z "$evolink_key" ] && [ -f "${SCRIPT_DIR}/.env.example" ]; then
        evolink_key=$(grep -E '^EVOLINK_API_KEY=' "${SCRIPT_DIR}/.env.example" | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
    fi

    # 读取 WX 配置
    local wx_appid="" wx_secret=""
    for f in "$ENV_FILE" "${SCRIPT_DIR}/.env.example"; do
        [ -f "$f" ] || continue
        [ -z "$wx_appid" ] && wx_appid=$(grep -E '^WX_APPID=' "$f" | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
        [ -z "$wx_secret" ] && wx_secret=$(grep -E '^WX_SECRET=' "$f" | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
    done

    cat > "$ENV_LOCAL_FILE" <<EOF
# ═══════════════════════════════════════════════════════════════
# Monica Server — 本地开发配置（由 dev-start.sh 自动生成）
# 此文件仅用于本地验证，不要提交到 Git
# ═══════════════════════════════════════════════════════════════

SECRET_KEY=${secret_key}

# 微信小程序（本地测试时填写真实值才能登录）
WX_APPID=${wx_appid:-wx_your_appid}
WX_SECRET=${wx_secret:-your_wx_secret}

# 数据库（本地 SQLite，放在项目根目录）
DATABASE_URL=sqlite:///./monica.db

# Redis（本地）
REDIS_URL=redis://localhost:6379/0

# LLM
DEFAULT_MODEL=gemini-3-flash
EVOLINK_API_KEY=${evolink_key:-your_evolink_api_key_here}
EVOLINK_BASE_URL=https://direct.evolink.ai/v1beta/models

# 存储（本地相对路径）
STORAGE_ROOT=./storage
MAX_UPLOAD_SIZE_MB=500
CHUNK_SIZE_MB=5

# 性能（本地开发适当调高并发）
ARQ_MAX_JOBS=2
ARQ_JOB_TIMEOUT=600
DICOM_BATCH_SIZE=20
TOP_K_SLICES=10
TOTALSEG_FAST=true

# CORS（本地允许所有来源）
ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000

# 配额
DAILY_TOKEN_LIMIT=200000
EOF

    success ".env.local 已生成"

    # 检查关键配置
    if [[ "${evolink_key}" == *"your_evolink"* ]] || [ -z "${evolink_key}" ]; then
        warn ""
        warn "⚠️  EVOLINK_API_KEY 未配置！"
        warn "   请编辑 .env.local 填写真实的 API Key，否则 LLM 分析功能无法使用"
        warn "   文件路径: ${ENV_LOCAL_FILE}"
        warn ""
    fi
    if [[ "${wx_appid}" == *"your_appid"* ]] || [ -z "${wx_appid}" ]; then
        warn "⚠️  WX_APPID / WX_SECRET 未配置，微信登录将不可用"
        warn "   （如果只测试上传/分析逻辑，可直接用 test.html 页面的 mock token）"
        warn ""
    fi
}

_start_redis() {
    # 检查 Redis 是否已在运行
    if redis-cli ping >/dev/null 2>&1; then
        success "Redis 已在运行，跳过启动"
        return
    fi

    # 尝试用 brew services 启动（macOS）
    if command -v brew &>/dev/null; then
        if brew list redis &>/dev/null 2>&1; then
            info "通过 Homebrew 启动 Redis..."
            brew services start redis 2>/dev/null || true
            sleep 2
            if redis-cli ping >/dev/null 2>&1; then
                success "Redis 已启动（brew services）"
                return
            fi
        else
            warn "Redis 未安装，正在通过 Homebrew 安装..."
            brew install redis -q
            brew services start redis
            sleep 2
            if redis-cli ping >/dev/null 2>&1; then
                success "Redis 安装并启动完成"
                return
            fi
        fi
    fi

    # 直接后台启动 redis-server
    if command -v redis-server &>/dev/null; then
        info "直接启动 redis-server..."
        redis-server --daemonize yes \
            --logfile "${LOG_DIR}/redis.log" \
            --port 6379 \
            --maxmemory 256mb \
            --maxmemory-policy allkeys-lru
        echo $! > "${PID_DIR}/redis.pid" 2>/dev/null || true
        sleep 1
        redis-cli ping >/dev/null 2>&1 \
            && success "Redis 已启动（独立进程）" \
            || error "Redis 启动失败，请手动安装：brew install redis"
        return
    fi

    error "Redis 未安装！请运行：brew install redis"
}

_start_fastapi() {
    local pid_file="${PID_DIR}/fastapi.pid"

    # 如果已在运行则跳过
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        success "FastAPI 已在运行 (PID=$(cat $pid_file))，跳过"
        return
    fi

    # 使用 --env-file 加载本地配置，以 nohup 后台运行
    nohup env $(grep -v '^#' "$ENV_LOCAL_FILE" | grep -v '^$' | xargs) \
        "${VENV_DIR}/bin/uvicorn" app.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers 1 \
        --loop asyncio \
        --log-level info \
        --reload \
        > "${LOG_DIR}/fastapi.log" 2> "${LOG_DIR}/fastapi.err" &

    echo $! > "$pid_file"
    success "FastAPI 已启动 (PID=$!)"
}

_start_arq_worker() {
    local pid_file="${PID_DIR}/arq_worker.pid"

    # 如果已在运行则跳过
    if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
        success "ARQ Worker 已在运行 (PID=$(cat $pid_file))，跳过"
        return
    fi

    nohup env $(grep -v '^#' "$ENV_LOCAL_FILE" | grep -v '^$' | xargs) \
        "${VENV_DIR}/bin/watchfiles" \
        "--filter" "python" \
        "arq app.workers.arq_worker.WorkerSettings" \
        "${SCRIPT_DIR}/app" \
        > "${LOG_DIR}/arq_worker.log" 2> "${LOG_DIR}/arq_worker.err" &

    echo $! > "$pid_file"
    success "ARQ Worker 已启动 (PID=$!)"
}

# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
COMMAND="${1:-start}"
case "$COMMAND" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    restart)
        cmd_stop 2>/dev/null || true
        sleep 1
        cmd_start
        ;;
    logs)    shift; cmd_logs "${1:-app}" ;;
    clean)   cmd_clean ;;
    help|--help|-h)
        echo ""
        echo -e "${CYAN}${BOLD}Monica Server — macOS 本地开发启动脚本${NC}"
        echo ""
        echo "  用法: $0 <命令>"
        echo ""
        echo "  命令:"
        echo "    start           启动所有服务（默认命令）"
        echo "    stop            停止所有后台服务"
        echo "    restart         重启所有服务"
        echo "    status          查看服务状态 + 健康检查"
        echo "    logs [target]   查看日志  target: app(默认)/worker/all"
        echo "    clean           清理环境（停止服务 + 删除 venv 和数据）"
        echo ""
        echo "  首次运行会自动："
        echo "    ① 检查 Python 3.11+，提示安装方式"
        echo "    ② 创建 .venv 虚拟环境"
        echo "    ③ 安装 requirements.txt 依赖"
        echo "    ④ 生成 .env.local 本地配置"
        echo "    ⑤ 启动 Redis（通过 Homebrew 或独立进程）"
        echo "    ⑥ 后台启动 FastAPI + ARQ Worker"
        echo ""
        echo "  测试台地址: http://localhost:8000/test"
        echo ""
        ;;
    *)
        echo "未知命令: $COMMAND"
        echo "运行 $0 help 查看帮助"
        exit 1
        ;;
esac
