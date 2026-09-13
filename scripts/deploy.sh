#!/usr/bin/env bash
# TG-Assistant 一键部署脚本（systemd 方式，适合裸机 / 虚拟机）
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/Star7-Files-Hub/TG-Assistant/main/scripts/deploy.sh | bash
# 或下载后执行：
#   bash scripts/deploy.sh
#
# 流程：root 检查 → 装系统依赖 → 创建用户 → 拉代码 → 建 venv → 装包 → 初始化数据目录 → 写 .env → 写 systemd → 启动
set -euo pipefail

# ---------------- 配置（按需修改） ----------------
APP_NAME="tg-assistant"
APP_USER="tga"
APP_DIR="/opt/${APP_NAME}"
DATA_DIR="/opt/${APP_NAME}/data"
LOG_DIR="${DATA_DIR}/logs"
VENV_DIR="${APP_DIR}/.venv"
BRANCH="main"
REPO_URL="https://github.com/Star7-Files-Hub/TG-Assistant.git"
PYTHON_BIN="python3"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
# ---------------- 配置结束 ----------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[*]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[✗]${NC} $*"; exit 1; }

# ---------------- 检查 ----------------
if [[ $EUID -ne 0 ]]; then
    fail "请用 root 执行：bash $0"
fi

if ! command -v ${PYTHON_BIN} >/dev/null 2>&1; then
    fail "未找到 ${PYTHON_BIN}，请先安装 Python 3.9+"
fi

PYTHON_VERSION=$(${PYTHON_BIN} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python 版本：${PYTHON_VERSION}"

# ---------------- 系统依赖 ----------------
info "安装系统依赖..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    git \
    build-essential \
    libffi-dev \
    curl \
    tini \
    >/dev/null 2>&1 || warn "部分系统依赖安装失败（可能已存在）"

# ---------------- 用户 ----------------
if ! id "${APP_USER}" >/dev/null 2>&1; then
    info "创建用户 ${APP_USER}..."
    useradd --system --create-home --shell /bin/bash "${APP_USER}"
fi

# ---------------- 代码 ----------------
if [[ -d "${APP_DIR}/.git" ]]; then
    info "更新代码..."
    cd "${APP_DIR}"
    sudo -u "${APP_USER}" git fetch --all --prune
    sudo -u "${APP_USER}" git checkout "${BRANCH}"
    sudo -u "${APP_USER}" git pull origin "${BRANCH}"
else
    info "克隆代码..."
    git clone --branch "${BRANCH}" --depth 1 "${REPO_URL}" "${APP_DIR}"
fi

# ---------------- venv ----------------
if [[ ! -d "${VENV_DIR}" ]]; then
    info "创建虚拟环境..."
    sudo -u "${APP_USER}" ${PYTHON_BIN} -m venv "${VENV_DIR}"
fi

info "安装 Python 依赖（含加密加速）..."
sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip -q
sudo -u "${APP_USER}" "${VENV_DIR}/bin/pip" install -e "${APP_DIR}/.[speed]" -q

# ---------------- 数据目录 ----------------
info "初始化数据目录..."
mkdir -p "${DATA_DIR}" "${LOG_DIR}/accounts" "${DATA_DIR}/qr"
chown -R "${APP_USER}:${APP_USER}" "${DATA_DIR}"

# ---------------- .env ----------------
if [[ ! -f "${APP_DIR}/.env" ]]; then
    info "生成 .env 模板..."
    cat > "${APP_DIR}/.env" <<ENV
# ===== TG-Assistant 环境变量 =====
# 从 https://my.telegram.org 获取
TGA_API_ID=
TGA_API_HASH=

# 代理（国内服务器必填）
# TGA_PROXY=socks5://user:pass@127.0.0.1:1080

# 通知 bot（可选）
# TGA_BOT_TOKEN=

# 数据目录：写成绝对路径，这样在任何目录下执行 CLI 都指向同一份数据
TGA_DATA_DIR=${DATA_DIR}

# 日志级别：DEBUG / INFO / WARNING / ERROR
TGA_LOG_LEVEL=INFO

# 并发 worker 数
TGA_WORKERS=8
ENV
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    warn ".env 已生成，请编辑填入 api_id / api_hash："
    warn "  nano ${APP_DIR}/.env"
else
    info ".env 已存在，跳过"
fi

# ---------------- systemd ----------------
info "写入 systemd 服务..."
cat > "${SERVICE_FILE}" <<SERVICE
[Unit]
Description=TG-Assistant Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/.env
# systemd 本身就是 init，不需要 tini 当 PID 1。
# 注意：tini 由 apt 装在 /usr/bin，venv 里并没有 bin/tini，
# 之前把 tini 写成 venv 下的路径会让 systemd 直接报 203/EXEC 起不来。
ExecStart=${VENV_DIR}/bin/tg-assistant run
Restart=always
RestartSec=15
StandardOutput=append:${LOG_DIR}/service.log
StandardError=append:${LOG_DIR}/service.log

# 资源与权限
LimitNOFILE=65536
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=${DATA_DIR}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
ok "systemd 服务已写入：${SERVICE_FILE}"

# ---------------- 完成 ----------------
echo ""
ok "部署完成！下一步："
echo ""
echo "  1. 编辑配置："
echo "       nano ${APP_DIR}/.env"
echo ""
echo "  2. 扫码登录（会显示二维码，用 Telegram 扫）："
echo "       cd ${APP_DIR}      # 重要：.env 是从当前目录加载的"
echo "       sudo -u ${APP_USER} ${VENV_DIR}/bin/tg-assistant login -a main"
echo ""
echo "  3. 生成示例配置："
echo "       sudo -u ${APP_USER} ${VENV_DIR}/bin/tg-assistant config init -a main --example"
echo ""
echo "  4. 编辑转发规则："
echo "       nano ${DATA_DIR}/accounts/main/config.json"
echo ""
echo "  5. 启动服务："
echo "       systemctl enable --now ${APP_NAME}"
echo "       journalctl -u ${APP_NAME} -f          # 看日志"
echo "       systemctl status ${APP_NAME}          # 看状态"
echo ""
warn "提示：国内服务器务必在 .env 里填 TGA_PROXY，否则连不上 Telegram。"
