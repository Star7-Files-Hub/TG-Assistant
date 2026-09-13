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
