#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Monica Medical AI Server — Docker 一键部署脚本
#
# 用法：
#   chmod +x deploy.sh
#   ./deploy.sh             # 首次部署（构建镜像 + 启动全部服务）
#   ./deploy.sh update      # 拉取新代码后重建镜像并热重启
#   ./deploy.sh logs        # 实时查看应用日志
#   ./deploy.sh logs nginx  # 查看 nginx 日志
#   ./deploy.sh stop        # 停止所有容器
#   ./deploy.sh restart     # 重启所有容器（不重建镜像）
#   ./deploy.sh status      # 查看容器状态 + 资源占用 + 健康检查
#
# 依赖：Docker + Docker Compose v2
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 前置检查 ───────────────────────────────────────────────────
check_deps() {
    command -v docker >/dev/null 2>&1 \
        || error "Docker 未安装，请先安装：https://docs.docker.com/get-docker/"
    docker compose version >/dev/null 2>&1 \
        || error "Docker Compose v2 未安装，请升级 Docker Desktop 或安装 docker-compose-plugin"
}

# ── .env 检查 ──────────────────────────────────────────────────
check_env() {
    if [ ! -f .env ]; then
        warn ".env 文件不存在，从 .env.example 复制..."
        cp .env.example .env
        warn "请编辑 .env，填写真实的 SECRET_KEY / EVOLINK_API_KEY 后重新运行！"
        echo -e "  生成 SECRET_KEY: ${YELLOW}python3 -c \"import secrets; print(secrets.token_hex(32))\"${NC}"
        exit 1
    fi

    local secret_key
    secret_key=$(grep -E '^SECRET_KEY=' .env | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
    if [[ "$secret_key" == *"change-me"* ]] || [[ "$secret_key" == *"your-secret-key"* ]]; then
        error "SECRET_KEY 仍为默认值！请修改后再部署\n  生成命令: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    fi

    local evolink_key
    evolink_key=$(grep -E '^EVOLINK_API_KEY=' .env | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
    if [ -z "$evolink_key" ] || [[ "$evolink_key" == *"your_evolink"* ]]; then
        warn "EVOLINK_API_KEY 未配置，LLM 调用将失败"
    fi
}

# ── 等待健康检查通过 ───────────────────────────────────────────
wait_healthy() {
    info "等待服务就绪（最多 90 秒）..."
    local i=0
    until docker compose exec -T app \
        python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
        >/dev/null 2>&1; do
        i=$((i+1))
        [ $i -ge 18 ] && error "服务启动超时，请运行 ./deploy.sh logs 查看错误"
        sleep 5
        echo -n "."
    done
    echo ""
}

# ── 命令：首次部署 ─────────────────────────────────────────────
cmd_deploy() {
    info "🐳 Docker 首次部署 Monica Server..."
    check_deps
    check_env

    info "拉取基础镜像（redis / nginx）..."
    docker compose pull redis nginx 2>/dev/null || true

    info "构建应用镜像..."
    docker compose build --no-cache app

    info "启动所有服务..."
    docker compose up -d

    wait_healthy

    local ip; ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    success "✅ 部署完成！"
    echo ""
    echo -e "  🌐 访问地址  : ${GREEN}http://${ip}${NC}"
    echo -e "  🔍 健康检查  : curl http://localhost/health"
    echo -e "  🧪 测试台    : http://${ip}/test"
    echo -e "  📋 查看日志  : ./deploy.sh logs"
    echo -e "  📊 查看状态  : ./deploy.sh status"
}

# ── 命令：更新部署 ─────────────────────────────────────────────
cmd_update() {
    info "🔄 更新部署 Monica Server..."
    check_deps
    check_env

    info "重新构建应用镜像（保留 redis/nginx）..."
    docker compose build app

    info "滚动重启应用容器..."
    docker compose up -d --no-deps app

    wait_healthy
    success "✅ 更新完成！"
}

# ── 命令：日志 ─────────────────────────────────────────────────
cmd_logs() {
    check_deps
    docker compose logs -f --tail=200 "${@:-app}"
}

# ── 命令：停止 ─────────────────────────────────────────────────
cmd_stop() {
    check_deps
    info "停止所有容器..."
    docker compose down
    success "已停止"
}

# ── 命令：重启 ─────────────────────────────────────────────────
cmd_restart() {
    check_deps
    info "重启容器..."
    docker compose restart "${@:-}"
    success "已重启"
}

# ── 命令：状态 ─────────────────────────────────────────────────
cmd_status() {
    check_deps
    echo ""
    echo -e "${BLUE}══════════════════ 容器状态 ══════════════════${NC}"
    docker compose ps
    echo ""
    echo -e "${BLUE}══════════════════ 资源占用 ══════════════════${NC}"
    docker compose stats --no-stream 2>/dev/null || true
    echo ""
    echo -e "${BLUE}══════════════════ 健康检查 ══════════════════${NC}"
    if docker compose exec -T app \
        python -c "import urllib.request, json; r=urllib.request.urlopen('http://localhost:8000/health'); print(json.loads(r.read()))" \
        2>/dev/null; then
        success "API 服务正常"
    else
        warn "API 服务未响应"
    fi
}

# ── 主入口 ─────────────────────────────────────────────────────
COMMAND="${1:-deploy}"
case "$COMMAND" in
    deploy)   cmd_deploy ;;
    update)   cmd_update ;;
    logs)     shift; cmd_logs "$@" ;;
    stop)     cmd_stop ;;
    restart)  shift; cmd_restart "$@" ;;
    status)   cmd_status ;;
    *)
        echo "用法: $0 {deploy|update|logs|stop|restart|status}"
        exit 1
        ;;
esac
