#!/usr/bin/env python3
"""Web 服务器 - 实时展示抖音登录二维码"""
import asyncio
import json
import time
import socket
from http.server import HTTPServer, SimpleHTTPRequestHandler
from threading import Thread
from urllib.parse import urlparse
from crawler import DouyinQRLogin, QRCodeResult

# 全局二维码状态
_qr_state = {
    "result": None,
    "timestamp": 0,
    "running": False,
}
_main_loop = None  # 主线程事件循环引用，用于跨线程调度

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>抖音登录二维码</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: #0a0a0a; color: #fff;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh;
}
.card {
    background: #1a1a2e; border-radius: 16px;
    padding: 40px; text-align: center;
    box-shadow: 0 20px 60px rgba(252/116/116/0.15);
    max-width: 420px; width: 90%;
}
h1 { font-size: 24px; margin-bottom: 8px; color: #fe2c55; }
.subtitle { color: #888; font-size: 14px; margin-bottom: 24px; }
.qr-container {
    background: #fff; border-radius: 12px;
    padding: 20px; margin: 16px auto;
    display: inline-block; position: relative;
}
.qr-container img { display: block; width: 240px; height: 240px; }
.qr-container .loading {
    width: 240px; height: 240px;
    display: flex; align-items: center; justify-content: center;
    color: #666; font-size: 14px;
}
.meta { color: #666; font-size: 12px; margin-top: 16px; }
.meta .time { color: #fe2c55; }
.status {
    display: inline-block; padding: 4px 12px;
    border-radius: 20px; font-size: 12px; margin-top: 12px;
}
.status.ok { background: #16a34a22; color: #4ade80; }
.status.err { background: #dc262622; color: #f87171; }
.status.loading { background: #f59e0b22; color: #fbbf24; }
.tip { color: #888; font-size: 13px; margin-top: 20px; line-height: 1.6; }
.refresh-btn {
    margin-top: 16px; padding: 10px 24px;
    background: #fe2c55; color: #fff; border: none;
    border-radius: 8px; cursor: pointer; font-size: 14px;
}
.refresh-btn:hover { background: #e61e4d; }
</style>
</head>
<body>
<div class="card">
    <h1>抖音登录二维码</h1>
    <p class="subtitle">使用抖音 App 扫描下方二维码登录</p>
    <div class="qr-container" id="qr-box">
        <div class="loading" id="loading">加载中...</div>
    </div>
    <div id="status" class="status loading">等待加载</div>
    <div class="meta" id="meta"></div>
    <button class="refresh-btn" onclick="refreshQR()">刷新二维码</button>
    <p class="tip">
        ⏰ 二维码有效期约 3-5 分钟<br>
        📱 打开抖音 → 点击扫一扫 → 扫描上方二维码
    </p>
</div>
<script>
let lastTs = 0;
let refreshing = false;
async function refreshQR() {
    if (refreshing) return;
    refreshing = true;
    const box = document.getElementById('qr-box');
    const st = document.getElementById('status');
    box.innerHTML = '<div class="loading">⏳ 刷新中...</div>';
    st.className = 'status loading'; st.textContent = '⏳ 正在刷新...';
    try {
        const r = await fetch('/api/refresh', {method:'POST'});
        const d = await r.json();
        if (!d.ok) {
            box.innerHTML = '<div class="loading">❌ ' + (d.error || '刷新请求失败') + '</div>';
            st.className = 'status err'; st.textContent = '❌ ' + (d.error || '刷新失败');
        }
        // 不管 ok 还是失败，都等 poll 拿到新数据后自动更新显示
    } catch(e) {
        box.innerHTML = '<div class="loading">❌ 网络错误</div>';
        st.className = 'status err'; st.textContent = '❌ 网络错误';
    }
    refreshing = false;
}
function updateDisplay(d) {
    const box = document.getElementById('qr-box');
    const st = document.getElementById('status');
    const meta = document.getElementById('meta');
    if (d.success && d.img_data) {
        box.innerHTML = '<img src="' + d.img_data + '" />';
        st.className = 'status ok'; st.textContent = '✅ 二维码就绪';
        meta.innerHTML = '尺寸: ' + d.size + ' | 更新: <span class="time">' + d.time_str + '</span>';
        lastTs = d.timestamp;
    } else {
        box.innerHTML = '<div class="loading">❌ ' + (d.error || '加载失败') + '</div>';
        st.className = 'status err'; st.textContent = '❌ ' + (d.error || '加载失败');
    }
}
async function poll() {
    try {
        const r = await fetch('/api/status');
        const d = await r.json();
        if (d.timestamp !== lastTs) updateDisplay(d);
    } catch(e) {}
}
setInterval(poll, 2000);
poll();
</script>
</body>
</html>"""


class QRHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self._respond(200, "text/html", HTML_PAGE.encode())
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/qrcode":
            self._api_qrcode()
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/refresh":
            self._api_refresh()
        else:
            self._respond(404, "text/plain", b"Not Found")

    def _api_status(self):
        st = _qr_state
        r = st.get("result")
        if r and r.success:
            data = {
                "success": True,
                "img_data": f"data:image/png;base64,{r.image_base64}",
                "size": f"{r.natural_size[0]}x{r.natural_size[1]}",
                "timestamp": r.timestamp,
                "time_str": time.strftime("%H:%M:%S", time.localtime(r.timestamp)),
            }
        else:
            data = {
                "success": False,
                "error": r.error if r else "尚未抓取",
                "timestamp": 0,
            }
        self._respond(200, "application/json", json.dumps(data).encode())

    def _api_qrcode(self):
        st = _qr_state
        r = st.get("result")
        if r and r.success:
            self._respond(200, "image/png", r.image_bytes)
        else:
            self._respond(404, "text/plain", b"No QR code")

    def _api_refresh(self):
        # 触发后台重新抓取（从 HTTP handler 线程安全提交到主线程事件循环）
        try:
            if _qr_state["running"]:
                self._respond(200, "application/json",
                              json.dumps({"ok": True, "msg": "正在刷新中，请稍候"}).encode())
                return
            if _main_loop is None:
                self._respond(500, "application/json",
                              json.dumps({"ok": False, "error": "事件循环未初始化"}).encode())
                return
            asyncio.run_coroutine_threadsafe(_do_fetch(), _main_loop)
            self._respond(200, "application/json", b'{"ok":true}')
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _respond(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 静默日志


async def _do_fetch():
    """后台抓取二维码"""
    if _qr_state["running"]:
        return
    _qr_state["running"] = True
    try:
        async with DouyinQRLogin(output_dir="./output") as crawler:
            result = await crawler.fetch_qrcode(save=True)
            _qr_state["result"] = result
            _qr_state["timestamp"] = result.timestamp
            if result.success:
                print(f"[OK] 二维码抓取成功 ({result.natural_size[0]}x{result.natural_size[1]})")
            else:
                print(f"[FAIL] {result.error}")
    finally:
        _qr_state["running"] = False


def run_server(host="0.0.0.0", port=8765):
    """启动 Web 服务器 + 二维码抓取

    主线程运行 asyncio 事件循环（处理异步抓取）
    HTTP 服务器在守护线程中运行
    """
    global _main_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _main_loop = loop

    # 先抓一次二维码
    print("[*] 正在抓取抖音登录二维码...")
    loop.run_until_complete(_do_fetch())

    # HTTP 服务器在独立守护线程中运行
    class ReusableHTTPServer(HTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True

    server = ReusableHTTPServer((host, port), QRHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"\n🌐 二维码展示页面: http://localhost:{port}")
    print(f"📡 API 状态接口:   http://localhost:{port}/api/status")
    print(f"🖼️ 二维码直链:     http://localhost:{port}/api/qrcode")
    print(f"🔄 刷新接口:       POST http://localhost:{port}/api/refresh")
    print(f"\n按 Ctrl+C 停止\n")

    try:
        # 主线程运行事件循环，处理后台抓取任务
        loop.run_forever()
    except KeyboardInterrupt:
        print("\n[*] 已停止")
        server.shutdown()
        server.server_close()
        loop.close()


if __name__ == "__main__":
    run_server()
