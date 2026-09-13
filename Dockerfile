# TG-Assistant — 多账号 Telegram 助手
#
# 构建：docker build -t tg-assistant .
# 运行：docker compose up -d
#
# 数据持久化：
#   - /app/data   会话、配置、状态（必须挂载）
#   - /app/data/logs   日志
#
# 首次使用：
#   1. 复制 .env.example 为 .env 并填入 api_id/api_hash/bot_token
#   2. docker compose run --rm tg-assistant login -a main
#   3. docker compose run --rm tg-assistant config init -a main --example
#   4. 编辑 data/accounts/main/config.json 中的规则
#   5. docker compose up -d

FROM python:3.12-slim AS base

# 避免交互式配置提示
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TGA_DATA_DIR=/app/data

# 系统依赖：build-essential 给 tgcrypto 兜底（无 wheel 时需要编译）
# procps 提供 pgrep，HEALTHCHECK 要用；slim 镜像默认不带
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        curl \
        tini \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖清单，利用 Docker 缓存层
COPY pyproject.toml README.md ./
COPY tg_assistant/ ./tg_assistant/
# 入口脚本：启动前建数据目录、校验凭据，再把参数透传给 tg-assistant
COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh

# 安装 Python 依赖（含加密加速 extra）
RUN pip install --upgrade pip \
    && pip install -e ".[speed]" \
    && chmod +x /usr/local/bin/entrypoint.sh

# 非 root 运行，降低容器逃逸风险
RUN useradd --create-home --shell /bin/bash tga \
    && mkdir -p /app/data \
    && chown -R tga:tga /app
USER tga

# 健康检查：进程存活 + 日志目录可写
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os; assert os.path.isdir('${TGA_DATA_DIR}/logs'), 'data dir missing'" \
        && pgrep -f 'tg-assistant run' > /dev/null || exit 1

# tini 做 PID 1，保证 SIGTERM 能正确转发给子进程。
# entrypoint.sh 负责启动前检查，并把参数原样透传给 tg-assistant ——
# 这样 `docker compose run --rm tg-assistant login -a main` 才能真正生效
# （直接写 CMD ["tg-assistant", "run"] 的话，追加的参数会覆盖整个 CMD，
#   变成 `tini -- login -a main`，而 login 并不是可执行文件）。
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["run"]
