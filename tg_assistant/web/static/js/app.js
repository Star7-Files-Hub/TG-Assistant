/* TG-Assistant Web 前端 JS */

// --------------------------------------------------------------------------- //
// 通用工具
// --------------------------------------------------------------------------- //
// 后端返回的群标题、用户名、显示名等文本可能由第三方控制，
// 拼进 innerHTML 前必须转义，否则会形成存储型 XSS。
function escapeHtml(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(
        /[&<>"']/g,
        (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
    );
}

function redirectToAuth() {
    const url = new URL('/auth', location.origin);
    url.searchParams.set('next', location.pathname + location.search);
    location.href = url.toString();
}

// 账号下拉选项的显示文本。
//
// ⚠️ 别直接用 /api/accounts 的 `user` 字段：它是 `小白 @meng5680 id=5608153118`
// 这种带 user_id 的调试用长标签，塞进 <option> 会把页面头部整个撑变形
// （优选 IP / 通知 / 抢红包 三个页面都踩过）。这里只取账号名 + 用户名。
function accountOptionText(account) {
    const handle = account.username ? '@' + account.username : (account.display_name || '');
    return handle ? `${account.name}（${handle}）` : account.name;
}

// 账号下拉框默认该选哪个。
//
// ⚠️ **不要无脑选 `accounts[0]`**：功能往往只配在另一个账号上，于是用户打开
// 「优选 IP」页看到的是空配置 + 状态卡「未运行」，很容易以为功能坏了
// —— 实测就是这么误判的（优选 IP 配在 SevenStar 上，页面默认选中了小白）。
//
// 优先选「开了这个功能」的账号（`/api/accounts` 的 `features` 字段，
// 见 `RuntimeManager._feature_flags`）；一个都没开就退回第一个。
function pickDefaultAccount(accounts, feature) {
    if (!accounts || !accounts.length) return '';
    if (feature) {
        const hit = accounts.find((a) => a.features && a.features[feature]);
        if (hit) return hit.name;
    }
    return accounts[0].name;
}

// 把后端给的时间渲染成本地时间。
//
// 后端有两种时间格式，别搞混：
//   * 账号的 `last_login` 是 **ISO 字符串**（`utc_now_iso()`，形如
//     `2026-09-15T11:23:16+00:00`）—— 直接丢给 `new Date()` 就行；
//   * 优选 IP 的 `updated_at` / 消息时间戳是 **Unix 秒** —— 要 `* 1000`。
// 以前账号页是 `${escapeHtml(a.last_login)}` 直接拼，表格里就是一坨
// `2026-09-15T11:23:16+00:00`，又长又看不懂本地是几点。
function formatDateTime(value) {
    if (value === null || value === undefined || value === '') return '';
    let date;
    if (typeof value === 'number') {
        date = new Date(value * 1000);           // Unix 秒
    } else if (/^\d+(\.\d+)?$/.test(String(value))) {
        date = new Date(Number(value) * 1000);   // 数字字符串也当 Unix 秒
    } else {
        date = new Date(value);                  // ISO 字符串
    }
    if (Number.isNaN(date.getTime())) return String(value);   // 解析不了就原样显示，别显示 Invalid Date
    return date.toLocaleString('zh-CN', { hour12: false });
}

// 统一的 401 处理：未鉴权或会话过期时直接送去 /auth，
// 省得每个页面的每个请求各写一遍判断。
const _nativeFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
    const response = await _nativeFetch(...args);
    if (response.status === 401) {
        redirectToAuth();
        throw new Error('未授权：正在跳转到访问验证页');
    }
    return response;
};

// --------------------------------------------------------------------------- //
// Toast 提示
// --------------------------------------------------------------------------- //
let toastTimer = null;
function toast(message, success = true) {
    const el = document.getElementById('toast');
    el.textContent = message;
    el.className = 'toast toast-' + (success ? 'success' : 'error');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.className = 'toast toast-hidden', 3000);
}

// --------------------------------------------------------------------------- //
// WebSocket 日志流
// --------------------------------------------------------------------------- //
function connectLogWs(onLog) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${proto}//${location.host}/ws/logs`);
    ws.onmessage = (e) => {
        const data = JSON.parse(e.data);
        if (data.type === 'ping') return;
        if (data.type === 'log' && onLog) onLog(data);
    };
    ws.onclose = (event) => {
        // 1008 = 服务端判定未授权，重连没有意义，直接去验证页
        if (event && event.code === 1008) {
            redirectToAuth();
            return;
        }
        // 断线后 3 秒自动重连
        setTimeout(() => connectLogWs(onLog), 3000);
    };
    ws.onerror = () => ws.close();
    return ws;
}

// --------------------------------------------------------------------------- //
// 运行状态轮询
// --------------------------------------------------------------------------- //
function pollStatus(intervalMs = 5000) {
    const statusEl = document.getElementById('run-status');
    if (!statusEl) return;
    async function check() {
        try {
            const res = await fetch('/api/status');
            const data = await res.json();
            if (data.running) {
                statusEl.className = 'badge badge-green';
                statusEl.textContent = '● 运行中';
            } else {
                statusEl.className = 'badge';
                statusEl.textContent = '● 未运行';
            }
        } catch (e) { /* 静默忽略 */ }
    }
    check();
    setInterval(check, intervalMs);
}

// 自动启动状态轮询
if (document.getElementById('run-status')) {
    pollStatus();
}

// --------------------------------------------------------------------------- //
// 防抖
// --------------------------------------------------------------------------- //
function debounce(fn, ms = 300) {
    let timer = null;
    return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), ms);
    };
}
