# BLivePost 容器镜像
# 包含playwright chromium内核（扫码登录必需，镜像较大属预期）
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/ms-playwright \
    TZ=Asia/Shanghai

WORKDIR /app

# 可选：构建期pip镜像（直连pypi.org不可达的网络环境下传入，如国内）：
#   docker build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple .
ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# 系统依赖（playwright chromium运行所需；tzdata让ENV TZ生效——
# 容器默认UTC，日志按日轮转/文件名会与北京时间差8小时）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 tzdata \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖层（利用构建缓存）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e . \
    && playwright install chromium --only-shell

# 项目文件（--chown写入时直接落目标属主：避免建层后再chown -R /app
# 触发全量复制——旧写法曾让镜像多出1.75GB的chown层）
COPY --chown=1000:1000 . .

# 非特权运行（v1.2.0审查加固）：业务进程以appuser(UID 1000)运行。
# 宿主机bind mount目录（./data ./logs）属主常为root（旧root容器产物/
# Linux默认），appuser不可写——镜像默认root仅为启动时修正属主，
# entrypoint在chown后经setpriv降权执行业务命令。pip层文件保持root
# 属主（appuser只读即可，不需chown整个/app）
RUN useradd --system --uid 1000 --create-home appuser \
    && mkdir -p /app/data /app/logs /app/exports \
    && chown appuser:appuser /app/data /app/logs /app/exports

# heredoc方式内联entrypoint（避免仓库中.sh文件受git换行符转换影响；
# 需BuildKit/Dockerfile frontend≥1.4，Docker 23+默认启用）
COPY <<'EOF' /app/docker-entrypoint.sh
#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    # 挂载目录属主修正（幂等；数据目录很大时可按需注释掉chown -R）。
    # 挂载后端不支持chown（CIFS/只读bind等）时仅告警——降权后若无法
    # 写入会与旧root容器遗留文件同样表现为启动PermissionError
    chown -R appuser:appuser /app/data /app/logs /app/exports \
        || echo "WARN: chown挂载目录失败，appuser可能无法写入data/logs"
    # 降权到appuser后执行（--init-groups重置补充组；--no-new-privs
    # 纵深防御禁止子进程经setuid等提权；HOME供chromium用）
    exec setpriv --reuid=1000 --regid=1000 --init-groups --no-new-privs \
        env HOME=/home/appuser "$@"
fi
# 已是非root（docker run --user appuser ...场景）直接执行
exec "$@"
EOF
RUN chmod 755 /app/docker-entrypoint.sh

# 数据与配置挂载点
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8080

# 健康检查：/healthz返回200为健康（DB可达+子进程存活+bot在线）
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=5); sys.exit(0 if r.status==200 else 1)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["blivepost", "-c", "/app/config.yaml"]
