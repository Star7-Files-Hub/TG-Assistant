/* TG-Assistant Web 前端 JS */

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
    ws.onclose = () => {
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
