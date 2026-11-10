# BLivePost 容器镜像
# 包含playwright chromium内核（扫码登录必需，镜像较大属预期）
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

WORKDIR /app

# 可选：构建期pip镜像（直连pypi.org不可达的网络环境下传入，如国内）：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# 系统依赖（playwright chromium运行所需）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖层（利用构建缓存）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e . \
    && playwright install chromium

# 项目文件
COPY . .

# 非特权运行用户（v1.2.0审查加固：容器不再以root跑业务进程）。
# chromium以非root无头运行需--cap-drop不影响（sandbox降级由chromium
# 自行处理headless场景）；数据/日志目录预建并归属appu，宿主机挂载时
# 请确保卷目录对该UID(1000)可写（chown 1000或放宽组权限）
RUN useradd --system --uid 1000 --create-home appuser \
    && mkdir -p /app/data /app/logs /app/exports \
    && chown -R appuser:appuser /app
USER appuser

# 数据与配置挂载点
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8080

# 健康检查：/healthz返回200为健康（DB可达+子进程存活+bot在线）
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=5); sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["blivepost", "-c", "/app/config.yaml"]
