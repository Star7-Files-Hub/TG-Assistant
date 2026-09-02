#!/usr/bin/env bash
# Docker 入口脚本：做启动前的最后检查，再交给 tini 当 PID 1。
set -euo pipefail

DATA_DIR="${TGA_DATA_DIR:-/app/data}"

# 确保数据目录存在且可写
mkdir -p "${DATA_DIR}/logs/accounts" "${DATA_DIR}/qr"

if [[ ! -w "${DATA_DIR}" ]]; then
    echo "[entrypoint] 错误：数据目录 ${DATA_DIR} 不可写" >&2
    exit 1
fi

# 没配 api_id/api_hash 时给出明确提示（而不是等 pyrogram 报错）
if [[ -z "${TGA_API_ID:-}" || -z "${TGA_API_HASH:-}" ]]; then
    echo "[entrypoint] 警告：TGA_API_ID / TGA_API_HASH 未设置，login 会失败。" >&2
    echo "           请在 .env 或环境变量中配置。" >&2
fi

echo "[entrypoint] 数据目录：${DATA_DIR}"
echo "[entrypoint] 启动命令：tg-assistant $*"

# 交给 tini（Dockerfile 里的 ENTRYPOINT 已经包含 tini --）
exec tg-assistant "$@"
