# ── Stage 1: 构建依赖层（避免每次重建都重装包）──────────────────────
FROM python:3.11-slim AS builder

# 安装编译依赖（SimpleITK / OpenCV 等需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libgl1-mesa-glx libglib2.0-0 \
    libgomp1 libsm6 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先只复制 requirements.txt，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: 最终运行镜像 ─────────────────────────────────────────
FROM python:3.11-slim AS runtime

LABEL maintainer="monica-server" \
      description="Monica Medical AI Server" \
      version="1.0.0"

# 运行时依赖（OpenCV / SimpleITK 运行库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx libglib2.0-0 \
    libgomp1 libsm6 libxrender1 libxext6 \
    supervisor \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 运行用户
RUN groupadd -r monica && useradd -r -g monica -d /opt/monica-server -s /sbin/nologin monica

# supervisord 通过 %(ENV_APP_DIR)s / %(ENV_APP_USER)s 读取路径
ENV APP_DIR=/opt/monica-server \
    APP_USER=monica

# 从 builder 复制安装好的 Python 包
COPY --from=builder /install /usr/local

# 复制应用代码
WORKDIR /opt/monica-server
COPY --chown=monica:monica . .

# 创建运行时目录
RUN mkdir -p \
    /opt/monica-server/storage/uploads \
    /opt/monica-server/storage/processed \
    /opt/monica-server/storage/exports \
    /opt/monica-server/storage/chunks \
    /var/log/monica \
    && chown -R monica:monica \
        /opt/monica-server/storage \
        /var/log/monica

# 不暴露 80/443（由 docker-compose 中 nginx 容器处理）
# 只暴露内部 FastAPI 端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# 默认以 supervisor 启动（同时管理 fastapi + arq_worker）
CMD ["supervisord", "-c", "/opt/monica-server/supervisord.conf", "-n"]
