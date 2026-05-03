#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Monica Medical AI Server — 一键部署脚本
#
# 用法：
#   chmod +x deploy.sh
#   ./deploy.sh            # 首次部署（构建镜像 + 启动）
#   ./deploy.sh update     # 更新代码后重新构建并重启
#   ./deploy.sh logs       # 查看实时日志
#   ./deploy.sh stop       # 停止服务
#   ./deploy.sh restart    # 重启服务（不重建镜像）
#   ./deploy.sh status     # 查看运行状态
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ── 颜色输出 ──────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 前置检查 ──────────────────────────────────────────────────
check_deps() {
    command -v docker >/dev/null 2>&1   || error "Docker 未安装，请先安装 Docker"
    command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
        || error "Docker Compose v2 未安装，请升级 Docker Desktop 或安装 docker-compose-plugin"
}

# ── 环境文件检查 ──────────────────────────────────────────────
check_env() {
    if [ ! -f .env ]; then
        warn ".env 文件不存在，正在从 .env.example 复制..."
        cp .env.example .env
        warn "请编辑 .env 文件，填写真实的 SECRET_KEY、EVOLINK_API_KEY 等配置后重新运行！"
        exit 1
    fi

    # 检查关键配置
    local secret_key
    secret_key=$(grep -E '^SECRET_KEY=' .env | cut -d= -f2- | tr -d '"'"'" | xargs)
    if [[ "$secret_key" == *"change-me"* ]] || [[ "$secret_key" == *"your-secret-key"* ]]; then
        error "检测到默认 SECRET_KEY，请在 .env 中修改为随机强密钥后再部署！\n  生成命令: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    fi
}

# ── 子命令：首次部署 / 更新部署 ──────────────────────────────
cmd_deploy() {
    local mode="${1:-first}"
    info "🚀 开始${mode == 'update' && echo '更新' || echo ''}部署 Monica Server..."

    check_deps
    check_env

    info "拉取基础镜像..."
    docker compose pull redis nginx 2>/dev/null || true

    info "构建应用镜像..."
    docker compose build --no-cache app

    if [ "$mode" = "update" ]; then
        info "滚动重启服务..."
        docker compose up -d --no-deps app
    else
        info "启动所有服务..."
        docker compose up -d
    fi

    info "等待服务就绪（最多 60 秒）..."
    local i=0
    until docker compose exec -T app python -c \
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
        >/dev/null 2>&1; do
        i=$((i+1))
        [ $i -ge 12 ] && error "服务启动超时，请运行 ./deploy.sh logs 查看错误"
        sleep 5
        echo -n "."
    done
    echo ""

    success "✅ 部署完成！"
    echo ""
    echo "  HTTP 地址: http://$(hostname -I | awk '{print $1}')"
    echo "  健康检查: curl http://localhost/health"
    echo "  查看日志: ./deploy.sh logs"
    echo "  查看状态: ./deploy.sh status"
}

cmd_update() {
    cmd_deploy "update"
}

cmd_logs() {
    check_deps
    docker compose logs -f --tail=100 "${@:-app}"
}

cmd_stop() {
    check_deps
    info "停止所有服务..."
    docker compose down
    success "服务已停止"
}

cmd_restart() {
    check_deps
    info "重启服务..."
    docker compose restart "${@:-}"
    success "服务已重启"
}

cmd_status() {
    check_deps
    echo ""
    echo "═══════════════════ 容器状态 ═══════════════════"
    docker compose ps
    echo ""
    echo "═══════════════════ 资源使用 ═══════════════════"
    docker compose stats --no-stream 2>/dev/null || true
    echo ""
    echo "════════════════════ 健康检查 ══════════════════"
    if docker compose exec -T app python -c \
        "import urllib.request, json; r=urllib.request.urlopen('http://localhost:8000/health'); print(json.loads(r.read()))" \
        2>/dev/null; then
        success "API 服务正常"
    else
        warn "API 服务未响应"
    fi
}

# ── 主入口 ────────────────────────────────────────────────────
COMMAND="${1:-deploy}"

case "$COMMAND" in
    deploy)   cmd_deploy  ;;
    update)   cmd_update  ;;
    logs)     shift; cmd_logs "$@" ;;
    stop)     cmd_stop    ;;
    restart)  shift; cmd_restart "$@" ;;
    status)   cmd_status  ;;
    *)
        echo "用法: $0 {deploy|update|logs|stop|restart|status}"
        exit 1
        ;;
esac
