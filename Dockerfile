# BLivePost 容器镜像
# 包含playwright chromium内核（扫码登录必需，镜像较大属预期）
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright

WORKDIR /app

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

# 数据与配置挂载点
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8080

CMD ["blivepost", "-c", "/app/config.yaml"]
