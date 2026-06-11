#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# Monica Medical AI Server — 一键部署脚本（裸机）
#
# 支持系统：Ubuntu 20.04 / 22.04 / Debian 11+ / CentOS 7+ / Rocky / AlmaLinux
#
# 用法：
#   chmod +x deploy.sh
#   sudo ./deploy.sh install   # 首次安装（安装依赖 + 配置服务）
#   sudo ./deploy.sh update    # 更新代码并重启
#   ./deploy.sh logs           # 查看实时日志
#   sudo ./deploy.sh stop      # 停止服务
#   sudo ./deploy.sh restart   # 重启服务
#   ./deploy.sh status         # 查看运行状态
#   sudo ./deploy.sh uninstall # 卸载服务（保留数据）
#
# 部署后目录结构：
#   /www/wwwroot/monica-server/         ← 应用代码
#   /www/wwwroot/monica-server/venv/    ← Python 虚拟环境
#   /www/wwwroot/monica-server/storage/ ← 文件存储（DICOM / 切片 / 导出）
#   /www/wwwroot/monica-server/db/      ← SQLite 数据库
#   /var/log/monica/            ← 日志文件
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

# ── 系统平台检测 ────────────────────────────────────────────────
_detect_os() {
    local os_id=""
    if [ -f /etc/os-release ]; then
        os_id=$(. /etc/os-release && echo "${ID:-}")
    fi
    echo "$os_id"
}

_check_platform() {
    local uname_s; uname_s=$(uname -s)
    if [ "$uname_s" = "Darwin" ]; then
        echo -e "\033[0;31m[ERROR]\033[0m 检测到 macOS 系统。"
        echo -e "        deploy.sh 专为 Linux 服务器（Ubuntu/Debian/CentOS）设计。"
        echo -e "        本地 macOS 开发请使用：\033[0;32m./dev-start.sh\033[0m"
        exit 1
    fi
    local os_id; os_id=$(_detect_os)
    case "$os_id" in
        ubuntu|debian|linuxmint)
            PKG_MANAGER="apt"
            ;;
        centos|rhel|fedora|rocky|almalinux)
            PKG_MANAGER="yum"
            if command -v dnf &>/dev/null; then PKG_MANAGER="dnf"; fi
            ;;
        *)
            echo -e "\033[1;33m[WARN]\033[0m 未知发行版 '${os_id}'，尝试用 apt-get..."
            PKG_MANAGER="apt"
            ;;
    esac
}

_check_platform

# ── 配置（按需修改）─────────────────────────────────────────────
APP_DIR="/www/wwwroot/monica-server"
APP_USER="monica"
LOG_DIR="/var/log/monica"
VENV_DIR="${APP_DIR}/venv"
PYTHON_BIN="python3.11"        # 优先使用 3.11，install 步骤会自动安装
NGINX_CONF_DEST="/etc/nginx/sites-available/monica"
SUPERVISOR_CONF_DEST="/etc/supervisor/conf.d/monica.conf"

# ── 颜色 ───────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${CYAN}▶ $*${NC}"; }

# 必须以 root 运行（install/update/stop/restart/uninstall）
require_root() {
    [ "$(id -u)" -eq 0 ] || error "此命令需要 root 权限，请使用 sudo 运行"
}

# 脚本所在目录（即项目根目录）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ═══════════════════════════════════════════════════════════════
# install：首次安装
# ═══════════════════════════════════════════════════════════════
cmd_install() {
    require_root
    info "🖥️  裸机安装 Monica Server..."
    info "项目目录: ${SCRIPT_DIR}"

    # ── 1. 安装系统依赖（含 Python 3.11）───────────────────────
    step "安装系统依赖"

    if [ "${PKG_MANAGER}" = "apt" ]; then
        # ── Debian / Ubuntu ──
        apt-get update -qq
        if ! python3.11 --version >/dev/null 2>&1; then
            info "Python 3.11 未安装，正在通过 deadsnakes PPA 安装..."
            apt-get install -y --no-install-recommends software-properties-common -q
            add-apt-repository -y ppa:deadsnakes/ppa
            apt-get update -qq
            apt-get install -y --no-install-recommends \
                python3.11 python3.11-venv python3.11-dev python3.11-distutils -q
            success "Python 3.11 安装完成"
        else
            success "Python 3.11 已存在，跳过安装"
        fi
        apt-get install -y --no-install-recommends \
            gcc g++ \
            libgl1 libglib2.0-0 libgomp1 libsm6 libxrender1 libxext6 \
            redis-server supervisor nginx curl -q
    else
        # ── CentOS / RHEL / Rocky / AlmaLinux ──
        $PKG_MANAGER install -y epel-release 2>/dev/null || true
        if ! python3.11 --version >/dev/null 2>&1; then
            info "Python 3.11 未安装，正在安装..."
            $PKG_MANAGER install -y python3.11 python3.11-devel 2>/dev/null || \
                $PKG_MANAGER install -y python311 python311-devel
            success "Python 3.11 安装完成"
        else
            success "Python 3.11 已存在，跳过安装"
        fi
        $PKG_MANAGER install -y \
            gcc gcc-c++ \
            mesa-libGL glib2 libgomp libSM libXrender libXext \
            redis supervisor nginx curl
        # CentOS 的 supervisor 服务名为 supervisord
        SUPERVISOR_SVC="supervisord"
    fi
    success "系统依赖安装完成"

    # ── 2. 检查 Python 版本 ─────────────────────────────────────
    step "检查 Python 版本"
    PYTHON_BIN="python3.11"
    $PYTHON_BIN --version >/dev/null 2>&1 || error "Python 3.11 安装失败，请手动执行：${PKG_MANAGER} install python3.11"
    local py_ver
    py_ver=$($PYTHON_BIN -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    success "Python ${py_ver} ✓  (${PYTHON_BIN})"

    # ── 3. 创建运行用户 ─────────────────────────────────────────
    step "创建运行用户 ${APP_USER}"
    if ! id "$APP_USER" &>/dev/null; then
        useradd -r -s /sbin/nologin -d "$APP_DIR" -m "$APP_USER"
        success "用户 ${APP_USER} 已创建"
    else
        success "用户 ${APP_USER} 已存在，跳过"
    fi

    # ── 4. 部署代码 ─────────────────────────────────────────────
    step "部署应用代码到 ${APP_DIR}"
    if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
        mkdir -p "$APP_DIR"
        rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
              --exclude='*.pyc' --exclude='.env' \
              "${SCRIPT_DIR}/" "${APP_DIR}/"
        success "代码已同步到 ${APP_DIR}"
    else
        success "当前目录即为部署目录，跳过复制"
    fi

    # ── 5. 配置 .env ────────────────────────────────────────────
    step "检查 .env 配置"
    if [ ! -f "${APP_DIR}/.env" ]; then
        cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
        warn ".env 文件已创建，请编辑 ${APP_DIR}/.env 填写真实配置后重新运行！"
        echo ""
        echo -e "  必填项："
        echo -e "    ${YELLOW}SECRET_KEY${NC}      → python3 -c \"import secrets; print(secrets.token_hex(32))\""
        echo -e "    ${YELLOW}LLM_API_KEY${NC}     → 填写你的 LLM API Key"
        echo -e "    ${YELLOW}ALLOWED_ORIGINS${NC} → 填写前端域名，如 https://example.com"
        echo ""
        error "请先完成 .env 配置，然后重新运行 sudo ./deploy.sh install"
    fi
    _check_env_values
    success ".env 配置检查通过"

    # ── 6. 创建目录并授权 ───────────────────────────────────────
    step "创建运行时目录"
    mkdir -p \
        "${APP_DIR}/storage/uploads" \
        "${APP_DIR}/storage/processed" \
        "${APP_DIR}/storage/exports" \
        "${APP_DIR}/storage/chunks" \
        "${APP_DIR}/db" \
        "$LOG_DIR"
    chown -R "${APP_USER}:${APP_USER}" \
        "${APP_DIR}/storage" \
        "${APP_DIR}/db" \
        "$LOG_DIR"
    success "目录已就绪"

    # ── 7. 创建 Python venv 并安装依赖 ──────────────────────────
    step "创建 Python 虚拟环境并安装依赖"
    if [ ! -d "$VENV_DIR" ]; then
        $PYTHON_BIN -m venv "$VENV_DIR"
    fi
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt" \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --no-cache-dir -q
    chown -R "${APP_USER}:${APP_USER}" "$VENV_DIR"
    success "Python 依赖安装完成"

    # ── 8. 配置 Redis ────────────────────────────────────────────
    step "配置并启动 Redis"
    # 设置内存限制
    grep -q "^maxmemory " /etc/redis/redis.conf 2>/dev/null || \
        echo "maxmemory 256mb" >> /etc/redis/redis.conf
    grep -q "^maxmemory-policy " /etc/redis/redis.conf 2>/dev/null || \
        echo "maxmemory-policy allkeys-lru" >> /etc/redis/redis.conf
    systemctl enable redis-server
    systemctl start redis-server
    # 等待 Redis 就绪
    local i=0
    until redis-cli ping >/dev/null 2>&1; do
        i=$((i+1)); [ $i -ge 10 ] && error "Redis 启动失败"; sleep 1
    done
    success "Redis 已就绪"

    # ── 9. 写入 Supervisor 配置 ──────────────────────────────────
    step "配置 Supervisor（进程守护）"
    _write_supervisor_conf
    systemctl enable supervisor
    systemctl start supervisor
    supervisorctl reread
    supervisorctl update
    success "Supervisor 配置完成"

    # ── 10. 配置 Nginx ───────────────────────────────────────────
    step "配置 Nginx（反向代理）"
    _write_nginx_conf
    # 禁用默认站点
    rm -f /etc/nginx/sites-enabled/default
    ln -sf "$NGINX_CONF_DEST" /etc/nginx/sites-enabled/monica
    nginx -t || error "Nginx 配置检查失败，请查看错误信息"
    systemctl enable nginx
    systemctl reload nginx
    success "Nginx 配置完成"

    # ── 11. 等待服务就绪 ─────────────────────────────────────────
    step "等待应用服务就绪（最多 90 秒）"
    local j=0
    until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
        j=$((j+1))
        [ $j -ge 18 ] && error "服务启动超时，请查看日志：./deploy.sh logs"
        sleep 5; echo -n "."
    done
    echo ""
    success "应用服务已就绪"

    # ── 完成 ─────────────────────────────────────────────────────
    local ip; ip=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
    echo ""
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  ✅ Monica Server 裸机部署完成！${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
    echo ""
    echo -e "  🌐 访问地址  : ${GREEN}http://${ip}${NC}"
    echo -e "  🔍 健康检查  : curl http://localhost/health"
    echo -e "  🧪 测试台    : http://${ip}/test"
    echo -e "  📋 查看日志  : ./deploy.sh logs"
    echo -e "  📊 查看状态  : ./deploy.sh status"
    echo ""
}

# ═══════════════════════════════════════════════════════════════
# update：更新代码并重启
# ═══════════════════════════════════════════════════════════════
cmd_update() {
    require_root
    info "🔄 更新 Monica Server..."

    step "同步新代码到 ${APP_DIR}"
    if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
        rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
              --exclude='*.pyc' --exclude='.env' \
              "${SCRIPT_DIR}/" "${APP_DIR}/"
    fi

    step "更新 Python 依赖"
    "${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt" \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --no-cache-dir -q

    step "重启应用进程"
    supervisorctl restart monica:fastapi monica:arq_worker

    step "等待服务就绪"
    local i=0
    until curl -sf http://localhost:8000/health >/dev/null 2>&1; do
        i=$((i+1)); [ $i -ge 18 ] && error "重启超时，请查看日志：./deploy.sh logs"
        sleep 5; echo -n "."
    done
    echo ""
    success "✅ 更新完成！"
}

# ═══════════════════════════════════════════════════════════════
# logs：查看日志
# ═══════════════════════════════════════════════════════════════
cmd_logs() {
    local target="${1:-fastapi}"
    case "$target" in
        fastapi|app)
            info "📋 FastAPI 日志 (Ctrl+C 退出)"
            tail -f "${LOG_DIR}/fastapi.log" "${LOG_DIR}/fastapi-err.log" 2>/dev/null \
                || error "日志文件不存在，请先运行 install"
            ;;
        worker|arq)
            info "📋 ARQ Worker 日志 (Ctrl+C 退出)"
            tail -f "${LOG_DIR}/arq_worker.log" "${LOG_DIR}/arq_worker-err.log" 2>/dev/null \
                || error "日志文件不存在"
            ;;
        nginx)
            info "📋 Nginx 日志 (Ctrl+C 退出)"
            tail -f /var/log/nginx/access.log /var/log/nginx/error.log 2>/dev/null
            ;;
        all)
            info "📋 所有日志 (Ctrl+C 退出)"
            tail -f \
                "${LOG_DIR}/fastapi.log" \
                "${LOG_DIR}/arq_worker.log" \
                /var/log/nginx/error.log 2>/dev/null
            ;;
        *)
            echo "用法: $0 logs [app|worker|nginx|all]"
            ;;
    esac
}

# ═══════════════════════════════════════════════════════════════
# stop / restart
# ═══════════════════════════════════════════════════════════════
cmd_stop() {
    require_root
    info "停止应用进程..."
    supervisorctl stop monica:fastapi monica:arq_worker 2>/dev/null || true
    success "已停止"
}

cmd_restart() {
    require_root
    local target="${1:-all}"
    info "重启 Monica Server..."
    if [ "$target" = "nginx" ]; then
        systemctl reload nginx
        success "Nginx 已重载"
    else
        supervisorctl restart monica:fastapi monica:arq_worker
        success "应用进程已重启"
    fi
}

# ═══════════════════════════════════════════════════════════════
# status：查看运行状态
# ═══════════════════════════════════════════════════════════════
cmd_status() {
    echo ""
    echo -e "${BLUE}══════════════ Supervisor 进程状态 ══════════════${NC}"
    supervisorctl status 2>/dev/null || warn "Supervisor 未运行"

    echo ""
    echo -e "${BLUE}══════════════ 系统服务状态 ══════════════════════${NC}"
    for svc in redis-server nginx supervisor; do
        local st; st=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
        local icon="✅"; [ "$st" != "active" ] && icon="❌"
        printf "  ${icon} %-20s %s\n" "$svc" "$st"
    done

    echo ""
    echo -e "${BLUE}══════════════ API 健康检查 ══════════════════════${NC}"
    local result; result=$(curl -sf http://localhost:8000/health 2>/dev/null || echo "")
    if [ -n "$result" ]; then
        echo "  $result"
        success "API 服务正常"
    else
        warn "API 服务未响应（端口 8000）"
    fi

    echo ""
    echo -e "${BLUE}══════════════ 磁盘 / 内存 ═══════════════════════${NC}"
    df -h "$APP_DIR" 2>/dev/null | tail -1 | awk '{printf "  磁盘: 总 %s / 已用 %s / 剩余 %s\n", $2, $3, $4}'
    free -h 2>/dev/null | awk '/^Mem:/{printf "  内存: 总 %s / 已用 %s / 剩余 %s\n", $2, $3, $4}'
}

# ═══════════════════════════════════════════════════════════════
# uninstall：卸载（保留数据）
# ═══════════════════════════════════════════════════════════════
cmd_uninstall() {
    require_root
    warn "此操作将停止并移除服务配置，但保留 ${APP_DIR}/db 和 ${APP_DIR}/storage 中的数据"
    read -rp "确认卸载？(输入 yes 继续): " confirm
    [ "$confirm" = "yes" ] || { info "已取消"; exit 0; }

    supervisorctl stop monica:fastapi monica:arq_worker 2>/dev/null || true
    rm -f "$SUPERVISOR_CONF_DEST"
    supervisorctl reread 2>/dev/null || true
    supervisorctl update 2>/dev/null || true

    rm -f /etc/nginx/sites-enabled/monica
    rm -f "$NGINX_CONF_DEST"
    systemctl reload nginx 2>/dev/null || true

    success "服务配置已移除（数据目录 ${APP_DIR}/db 和 ${APP_DIR}/storage 已保留）"
}

# ═══════════════════════════════════════════════════════════════
# 内部函数
# ═══════════════════════════════════════════════════════════════

_check_env_values() {
    local env_file="${APP_DIR}/.env"
    local secret_key
    secret_key=$(grep -E '^SECRET_KEY=' "$env_file" | cut -d= -f2- | tr -d '"'"'" | xargs 2>/dev/null || true)
    if [[ "$secret_key" == *"change-me"* ]] || [[ "$secret_key" == *"your-secret-key"* ]]; then
        error "SECRET_KEY 仍为默认值！\n  生成命令: python3 -c \"import secrets; print(secrets.token_hex(32))\""
    fi
}

_write_supervisor_conf() {
    cat > "$SUPERVISOR_CONF_DEST" <<EOF
; ── Monica Medical AI Server - Supervisor 配置 ──────────────

[program:fastapi]
command=${VENV_DIR}/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --loop uvloop --log-level info --no-access-log
directory=${APP_DIR}
user=${APP_USER}
autostart=true
autorestart=true
startretries=5
startsecs=5
stdout_logfile=${LOG_DIR}/fastapi.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3
stderr_logfile=${LOG_DIR}/fastapi-err.log
stderr_logfile_maxbytes=20MB
stderr_logfile_backups=3
environment=PYTHONPATH="${APP_DIR}"

[program:arq_worker]
command=${VENV_DIR}/bin/arq app.workers.arq_worker.WorkerSettings
directory=${APP_DIR}
user=${APP_USER}
autostart=true
autorestart=true
startretries=5
startsecs=5
stdout_logfile=${LOG_DIR}/arq_worker.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3
stderr_logfile=${LOG_DIR}/arq_worker-err.log
stderr_logfile_maxbytes=20MB
stderr_logfile_backups=3
environment=PYTHONPATH="${APP_DIR}"

[group:monica]
programs=fastapi,arq_worker
EOF
}

_write_nginx_conf() {
    cat > "$NGINX_CONF_DEST" <<'NGINX_EOF'
# Monica Medical AI Server - Nginx 反向代理
# FastAPI 监听本机 127.0.0.1:8000

limit_req_zone  $binary_remote_addr  zone=api_limit:10m     rate=30r/m;
limit_req_zone  $binary_remote_addr  zone=upload_limit:10m  rate=5r/m;
limit_req_zone  $binary_remote_addr  zone=auth_limit:10m    rate=10r/m;
limit_conn_zone $binary_remote_addr  zone=conn_limit:10m;

upstream fastapi_backend {
    server 127.0.0.1:8000;
    keepalive 8;
}

server {
    listen 80;
    server_name _;

    client_max_body_size 512M;
    client_body_timeout  120s;
    gzip on;

    # 微信小程序域名验证
    location = /MP_verify_xxxxxx.txt {
        alias /www/wwwroot/monica-server/static/MP_verify_xxxxxx.txt;
    }

    # 鉴权接口
    location /auth/ {
        limit_req  zone=auth_limit   burst=5  nodelay;
        limit_conn conn_limit 20;
        proxy_pass         http://fastapi_backend;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    }

    # 上传初始化 / 完成（限流防滥用，不限分片上传本身）
    location ~ ^/upload/(init|complete) {
        limit_req  zone=upload_limit  burst=10 nodelay;
        limit_conn conn_limit 10;
        proxy_pass              http://fastapi_backend;
        proxy_http_version      1.1;
        proxy_set_header        Host            $host;
        proxy_set_header        X-Real-IP       $remote_addr;
        proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout      300s;
        proxy_send_timeout      300s;
        proxy_request_buffering off;
    }

    # 分片上传（PUT /upload/chunk）：不限频率，仅限并发连接数
    location /upload/ {
        limit_conn conn_limit 20;
        proxy_pass              http://fastapi_backend;
        proxy_http_version      1.1;
        proxy_set_header        Host            $host;
        proxy_set_header        X-Real-IP       $remote_addr;
        proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout      300s;
        proxy_send_timeout      300s;
        proxy_request_buffering off;
    }

    # SSE 实时推送（禁止缓冲）
    location /stream/ {
        limit_req  zone=api_limit burst=20 nodelay;
        proxy_pass              http://fastapi_backend;
        proxy_http_version      1.1;
        proxy_set_header        Host            $host;
        proxy_set_header        X-Real-IP       $remote_addr;
        proxy_set_header        X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header        Connection      "";
        proxy_buffering         off;
        proxy_cache             off;
        proxy_read_timeout      650s;
        add_header              X-Accel-Buffering "no";
        add_header              Cache-Control     "no-cache";
    }

    # 其他 API
    location / {
        limit_req  zone=api_limit burst=20 nodelay;
        limit_conn conn_limit 50;
        proxy_pass         http://fastapi_backend;
        proxy_http_version 1.1;
        proxy_set_header   Host            $host;
        proxy_set_header   X-Real-IP       $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }
}
NGINX_EOF
}

# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
COMMAND="${1:-help}"
case "$COMMAND" in
    install)   cmd_install ;;
    update)    cmd_update ;;
    logs)      shift; cmd_logs "${1:-app}" ;;
    stop)      cmd_stop ;;
    restart)   shift; cmd_restart "${1:-all}" ;;
    status)    cmd_status ;;
    uninstall) cmd_uninstall ;;
    help|--help|-h)
        echo ""
        echo -e "${CYAN}Monica Medical AI Server — 裸机部署脚本${NC}"
        echo ""
        echo "  用法: sudo $0 <命令>"
        echo ""
        echo "  命令:"
        echo "    install           首次安装（安装系统依赖 + 配置所有服务）"
        echo "    update            更新代码并重启（保留数据和配置）"
        echo "    logs [target]     查看日志  target: app(默认)/worker/nginx/all"
        echo "    stop              停止应用进程"
        echo "    restart [target]  重启  target: all(默认)/nginx"
        echo "    status            查看进程状态 + 健康检查"
        echo "    uninstall         卸载服务配置（保留数据）"
        echo ""
        ;;
    *)
        echo "未知命令: $COMMAND"
        echo "运行 $0 help 查看帮助"
        exit 1
        ;;
esac
