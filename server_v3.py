#!/usr/bin/env python3
"""
抖音登录 Web 控制台 - Crawl4AI V3 交互式版本

功能:
  📱 二维码实时展示与自动刷新
  🔘 扫码后网页渲染身份验证按钮（对应抖音验证页面）
      - 接收短信验证码
      - 手机刷脸验证
      - 验证登录密码
      - 发送短信验证
  ⌨️  验证码/密码输入框
  ✅ 登录状态实时反馈
  🔄 保持浏览器会话持久化

启动:
    python server_v3.py
    浏览器打开 http://localhost:8765
"""
import asyncio
import base64
import json
import os
import time
import threading
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from typing import Optional

sys_path = Path(__file__).parent
import sys
sys.path.insert(0, str(sys_path))

from douyin_crawl4ai_v3 import DouyinCrawlerV3

# ════════════════════ 全局状态机 ════════════════════

STATE_LOADING = "loading"           # 初始化中
STATE_QR_READY = "qr_ready"         # 二维码就绪，等待扫码
STATE_SCANNED = "scanned"           # 已扫码
STATE_VERIFY_NEEDED = "verify"      # 需要身份验证，显示按钮
STATE_INPUT_CODE = "input_code"     # 等待输入验证码
STATE_INPUT_PWD = "input_pwd"       # 等待输入密码
STATE_FACE_WAIT = "face_wait"       # 等待刷脸
STATE_SUCCESS = "success"           # 登录成功
STATE_ERROR = "error"               # 错误

_app = {
    "state": STATE_LOADING,
    "qrcode_b64": "",
    "qrcode_ts": 0,
    "message": "正在启动浏览器...",
    "verify_options": [],           # 从页面解析出的验证选项
    "user_info": {},
    "cookie_file": "",
    "phone": "",
    "error": "",
    "crawler": None,                # DouyinCrawlerV3 实例（持久化）
    "busy": False,                  # 操作锁
    "debug": {},                    # 页面探针调试信息
    "last_fetch": 0,                # 上次获取二维码时间（冷却用）
    "qr_net_status": "",            # 网络层捕获的二维码状态
    "qr_net_ts": 0,
    "net_log": [],                  # 最近的相关接口响应
    "mfa_lock": False,              # MFA 验证锁定（收到信号后禁止自动刷新）
    "verify_capture": [],           # 验证流程全量抓包（REQ+RESP 时间线）
    "verify_buttons": [],           # 从验证页面抓取的实体按钮（文本+全局坐标）
    "face_qrcode_b64": "",          # 刷脸验证页二维码（截图提取）
}

_main_loop = None                   # 主线程事件循环引用


# ════════════════════════════════════════════════════════════
# 自动回复引擎（独立线程 + 独立浏览器，不走代理；复用 auto_replier/dm_scraper）
# ════════════════════════════════════════════════════════════
REPLIER_CONFIG_F = sys_path / "replies_config.json"
REPLIER_OUT_DIR = sys_path / "output"

_replier_app = {
    "enabled": False,          # 引擎是否处于自动循环
    "one_shot": False,         # 请求立即执行一轮
    "one_shot_dry": True,
    "busy": False,             # 正在执行一轮
    "last_results": [],        # 最近一轮每条规则的结果
    "last_run_ts": 0,
    "next_run_ts": 0,
    "error": "",
    "started_ts": 0,
    "session_file": "",
}
_replier_thread = None
_replier_lock = threading.Lock()


def _replier_load_config() -> dict:
    try:
        return json.loads(REPLIER_CONFIG_F.resolve().read_text(encoding="utf-8"))
    except Exception:
        return {"check_interval_min": 10, "rules": []}


async def _replier_round(page, dry: bool) -> list:
    """执行一轮：读配置 → 打开面板 → 逐规则处理"""
    import dm_scraper as D
    import auto_replier as R
    cfg = _replier_load_config()
    st = R.load_state()
    # 每轮重新加载页面（长驻页面 SPA 状态会漂移导致面板打不开）
    try:
        await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)
    except Exception as e:
        log(f"[自动回复] 页面加载异常: {e}")
    ok = await D.open_message_panel(page)
    if not ok:
        try:
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(6000)
            ok = await D.open_message_panel(page)
        except Exception:
            ok = False
    if not ok:
        dbg = ""
        try:
            await page.screenshot(path=str(REPLIER_OUT_DIR / "replier_fail.png"))
            dbg = (await page.evaluate("() => (document.body.innerText||'').slice(0,300)") or "").replace("\n", " ")
        except Exception:
            pass
        _replier_app["error"] = f"消息面板未能打开（登录态可能失效）| 页面: {dbg[:120]}"
        return []
    await page.evaluate(D.REMOVE_MASK_JS)
    results = await R.run_round(page, cfg, st, dry)
    R.save_state(st)
    if not results:
        _replier_app["error"] = f"本轮 0 结果（规则数={len(cfg.get('rules', []))}，dry={dry}）"
    log(f"[自动回复] 本轮完成: 规则数={len(cfg.get('rules', []))} 结果数={len(results)} "
        f"dry={dry} " + " | ".join(f"{r.get('name')}:{r.get('action')}:{str(r.get('reason'))[:40]}" for r in results))
    return results


async def _replier_engine_async():
    # 不再创建独立浏览器：直接调度到主服务的登录浏览器上执行（登录态/页面渲染 100% 可用）
    log("[自动回复] 引擎启动（共用主服务浏览器）")
    _replier_app["session_file"] = "主服务浏览器（共用）"
    try:
        while _replier_app["enabled"] or _replier_app["one_shot"]:
            dry = _replier_app["one_shot_dry"]
            if _replier_app["one_shot"]:
                _replier_app["one_shot"] = False
            # 等待主服务登录成功
            if _app.get("state") != STATE_SUCCESS or not _app.get("crawler"):
                _replier_app["error"] = "等待主服务登录（state != success），暂不执行"
                _replier_app["busy"] = False
                while (_app.get("state") != STATE_SUCCESS or not _app.get("crawler")) \
                        and _replier_app["enabled"] and not _replier_app["one_shot"]:
                    await asyncio.sleep(3)
                continue
            _replier_app["busy"] = True
            try:
                crawler = _app["crawler"]
                page = crawler.page
                fut = asyncio.run_coroutine_threadsafe(_replier_round(page, dry), _main_loop)
                results = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=600)
                _replier_app["last_results"] = results
                _replier_app["last_run_ts"] = time.time()
                if results:
                    _replier_app["error"] = ""
            except Exception as e:
                _replier_app["error"] = f"执行异常: {e}"
                log(f"[自动回复] 执行异常: {e}")
            finally:
                _replier_app["busy"] = False

            if not _replier_app["enabled"]:
                break
            interval = int(_replier_load_config().get("check_interval_min", 10)) * 60
            _replier_app["next_run_ts"] = time.time() + interval
            log(f"[自动回复] 本轮完成，{interval//60} 分钟后下一轮")
            while time.time() < _replier_app["next_run_ts"]:
                if not _replier_app["enabled"] or _replier_app["one_shot"]:
                    break
                await asyncio.sleep(2)
    except Exception as e:
        _replier_app["error"] = f"引擎异常: {e}"
        log(f"[自动回复] 引擎异常: {e}")
    finally:
        _replier_app["busy"] = False
        _replier_app["next_run_ts"] = 0
        log("[自动回复] 引擎已停止")


def _replier_engine_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_replier_engine_async())
    finally:
        loop.close()


def _replier_start_thread(set_enabled: bool = True):
    global _replier_thread
    with _replier_lock:
        if _replier_thread and _replier_thread.is_alive():
            if set_enabled:
                _replier_app["enabled"] = True
            return
        if set_enabled:
            _replier_app["enabled"] = True
        _replier_app["started_ts"] = time.time()
        _replier_app["error"] = ""
        _replier_thread = threading.Thread(target=_replier_engine_thread, daemon=True)
        _replier_thread.start()


def _replier_api_get(self):
    log_tail = []
    try:
        lg = json.loads((REPLIER_OUT_DIR / "auto_reply_log.json").read_text(encoding="utf-8"))
        log_tail = lg[-30:]
    except Exception:
        pass
    st = {}
    try:
        st = json.loads((REPLIER_OUT_DIR / "auto_reply_state.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    self._respond(200, "application/json", json.dumps({
        "ok": True,
        "engine": {k: _replier_app[k] for k in
                   ("enabled", "busy", "last_results", "last_run_ts", "next_run_ts", "error", "session_file")},
        "config": _replier_load_config(),
        "state": st,
        "log": log_tail,
    }, ensure_ascii=False).encode("utf-8"))


def _replier_api_config(self, data):
    """保存规则配置"""
    try:
        rules = data.get("rules", [])
        # 基本校验
        cleaned = []
        for r in rules:
            if not str(r.get("name", "")).strip():
                continue
            cleaned.append({
                "name": str(r["name"]).strip(),
                "reply": str(r.get("reply", "")),
                "active": bool(r.get("active")),
                "trigger": "always" if r.get("trigger") == "always" else "new_message",
                "active_hours": str(r.get("active_hours", "")).strip(),
                "min_gap_min": max(0, int(r.get("min_gap_min", 60) or 0)),
                "max_per_day": max(1, int(r.get("max_per_day", 10) or 10)),
            })
        cfg = {"check_interval_min": max(1, int(data.get("check_interval_min", 10) or 10)),
               "rules": cleaned}
        REPLIER_CONFIG_F.resolve().write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self._respond(200, "application/json",
                      json.dumps({"ok": True, "config": cfg}, ensure_ascii=False).encode("utf-8"))
    except Exception as e:
        self._respond(500, "application/json",
                      json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))


def _replier_api_control(self, data):
    action = data.get("action", "")
    try:
        if action == "start":
            if not _replier_load_config().get("rules"):
                self._respond(400, "application/json",
                              b'{"ok":false,"error":"no rules"}')
                return
            _replier_start_thread()
            self._respond(200, "application/json", b'{"ok":true,"enabled":true}')
        elif action == "stop":
            _replier_app["enabled"] = False
            _replier_app["one_shot"] = False
            self._respond(200, "application/json", b'{"ok":true,"enabled":false}')
        elif action == "run_once":
            _replier_app["one_shot_dry"] = bool(data.get("dry", True))
            _replier_app["one_shot"] = True
            _replier_start_thread(set_enabled=False)
            self._respond(200, "application/json", b'{"ok":true,"started":true}')
        else:
            self._respond(400, "application/json", b'{"ok":false,"error":"bad action"}')
    except Exception as e:
        self._respond(500, "application/json",
                      json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))


def log(msg: str):
    """带时间戳的日志"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ════════════════════ 核心业务流程 ════════════════════


async def init_session():
    """初始化浏览器会话并获取二维码"""
    global _app

    try:
        _app["state"] = STATE_LOADING
        _app["message"] = "正在启动浏览器..."
        log("启动浏览器...")

        crawler = DouyinCrawlerV3(
            output_dir=str(sys_path / "output"),
            headless=True,          # 服务器端无头运行
        )
        await crawler.start_browser()
        _app["crawler"] = crawler
        log("浏览器已启动")

        # 注册网络监听（必须在打开页面前，才能捕获二维码状态接口）
        await setup_network_monitor(crawler)

        # 尝试恢复上次的登录态（重启免扫码）
        try:
            out_dir = Path(sys_path) / "output"
            # 同时查找 .json 与 .json.bak（防止会话文件被清理工具重命名）
            bak_dir = out_dir / "_backup_sessions"
            candidates = (list(out_dir.glob("session_*.json"))
                          + list(out_dir.glob("session_*.json.bak"))
                          + list(bak_dir.glob("*.json"))
                          + list(bak_dir.glob("*.bak"))) if out_dir.exists() else []
            sessions = sorted(
                candidates,
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if sessions:
                log(f"发现历史登录会话: {sessions[0].name}，尝试恢复...")
                await crawler.load_cookies(str(sessions[0]))
                await crawler.page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded", timeout=60000,
                )
                await asyncio.sleep(4)
                if await has_login_cookie(crawler):
                    _app["state"] = STATE_SUCCESS
                    _app["cookie_file"] = str(sessions[0])
                    _app["user_info"] = {"login": True, "restored": True}
                    _app["message"] = "已恢复上次登录，进入抖音官网"
                    log("✅ 登录态恢复成功，已进入抖音官网")
                    return
                log("历史会话已失效，进入扫码登录")
        except Exception as e:
            log(f"恢复登录态失败: {e}，进入扫码登录")

        # 打开页面并进入登录流程
        await fetch_qrcode()

    except Exception as e:
        _app["state"] = STATE_ERROR
        _app["error"] = f"初始化失败: {str(e)}"
        log(f"初始化失败: {e}")
        import traceback
        traceback.print_exc()


# 已进入流程后禁止刷新，避免冲掉扫码状态
_NO_REFRESH_STATES = {STATE_SCANNED, STATE_VERIFY_NEEDED, STATE_INPUT_CODE,
                      STATE_INPUT_PWD, STATE_FACE_WAIT, STATE_SUCCESS}


async def fetch_qrcode(force: bool = False):
    """
    获取/刷新二维码

    Args:
        force: True 表示用户主动重置，忽略流程保护
    """
    global _app

    crawler = _app.get("crawler")
    if not crawler:
        return

    # 流程保护：扫码后不再刷新页面
    if not force and (_app["state"] in _NO_REFRESH_STATES or _app.get("mfa_lock")):
        log(f"跳过刷新（状态 {_app['state']}，mfa_lock={_app.get('mfa_lock')}，刷新会丢失进度）")
        return

    # 冷却保护：避免频繁导航
    if not force:
        elapsed = time.time() - _app.get("last_fetch", 0)
        if elapsed < 10:
            log(f"跳过刷新（冷却中，{10 - elapsed:.0f}s 后可用）")
            return

    try:
        # ── 快速路径：登录面板仍在 → 面板内刷新（秒级），不 reload 整页 ──
        if not force:
            try:
                panel_alive = await crawler.page.evaluate("""() => {
                    const p = document.querySelector('#login-panel-new');
                    return !!(p && p.getBoundingClientRect().width > 0);
                }""")
                if panel_alive:
                    log("面板仍在，尝试面板内快速刷新...")
                    # 记录旧二维码 src，用于判断是否真的换新码
                    old_src = await crawler.page.evaluate("""() => {
                        const img = document.querySelector('#douyin_login_comp_scan_code img');
                        return img ? img.src : '';
                    }""")
                    # 抖音的刷新入口是无文字的图标/遮罩层：
                    # 依次尝试 容器及父级内含"刷新/过期"文字或 refresh/expire 类名的元素，
                    # 都没有就直接点容器本身（过期时容器整体可点）
                    hit = await crawler.page.evaluate("""() => {
                        const wrap = document.querySelector('#douyin_login_comp_scan_code');
                        if (!wrap) return null;
                        const scopes = [wrap, wrap.parentElement];
                        for (const scope of scopes) {
                            if (!scope) continue;
                            for (const el of scope.querySelectorAll('*')) {
                                const t = (el.textContent || '').trim();
                                const c = String(el.className || '');
                                if ((t && (t.includes('刷新') || t.includes('过期') || t.includes('重新')))
                                    || /refresh|reload|expire|mask|update/i.test(c)) {
                                    el.click();
                                    return 'hit:' + (t || c).slice(0, 40);
                                }
                            }
                        }
                        wrap.click();
                        return 'clicked-wrap';
                    }""")
                    if hit:
                        log(f"面板内刷新点击: {hit}")
                        # 以 src 变化为准判断新码（旧码也一直存在，不能只判"有图"）
                        for _ in range(10):
                            await asyncio.sleep(0.6)
                            try:
                                new_src = await crawler.page.evaluate("""() => {
                                    const img = document.querySelector('#douyin_login_comp_scan_code img');
                                    return img ? img.src : '';
                                }""")
                            except Exception:
                                continue
                            if new_src and new_src != old_src:
                                ok, img_bytes, _ = await crawler.extract_qrcode()
                                if ok:
                                    _app["qrcode_b64"] = base64.b64encode(img_bytes).decode()
                                    _app["qrcode_ts"] = time.time()
                                    _app["last_fetch"] = time.time()
                                    _app["state"] = STATE_QR_READY
                                    _app["message"] = "二维码已刷新"
                                    log("✅ 二维码已快速刷新（面板内，未重载页面）")
                                    return
                    log("面板内快速刷新未成功，走完整流程")
            except Exception as e:
                log(f"快速刷新异常: {e}，走完整流程")

        # ── 完整流程（优化：轮询就绪即点，去掉固定 5s+3s 等待，~20s → ~8s）──
        _app["message"] = "正在获取二维码..."
        log("打开抖音页面...")

        await crawler.open_douyin()
        # 轮询等登录按钮出现即点（替代固定 sleep(3)，省 ~2.5s）
        btn_ready = False
        for _ in range(8):
            await asyncio.sleep(0.5)
            try:
                btn = crawler.page.locator('button:has-text("登录")').first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=3000)
                    btn_ready = True
                    log("已点击登录按钮")
                    break
            except Exception:
                continue
        if not btn_ready:
            await crawler.click_login_button()
        await asyncio.sleep(1.5)

        panel = await crawler.wait_for_panel(timeout=12)
        if not panel.get("found"):
            _app["state"] = STATE_ERROR
            _app["error"] = "登录面板未出现"
            log("登录面板未出现")
            return

        # 确保在二维码 tab
        if not panel.get("hasQR"):
            await crawler.page.evaluate("""() => {
                const tabs = document.querySelectorAll('[class*="tab'], li, [role="tab"]');
                for (const t of tabs) {
                    if ((t.textContent||'').includes('扫码')) { t.click(); return; }
                }
            }""")
            await asyncio.sleep(2)

        # 等 QR img 完成渲染（面板出现后 img 常延迟加载，最多等 10s）
        success, img_bytes, err = False, b"", ""
        for _ in range(12):
            success, img_bytes, err = await crawler.extract_qrcode()
            if success:
                break
            await asyncio.sleep(0.8)

        # DOM 提取失败 → 全页截图 + OpenCV 二维码定位裁剪兜底
        # （抖音改版后 QR 不再是 <img> 时依然可用，与刷脸码同一套检测）
        if not success:
            log(f"DOM 提取失败({err})，改用截图+QR定位兜底...")
            for _ in range(5):
                try:
                    shot = await crawler.page.screenshot(type="png")
                    crop = _detect_qr_in_png(shot)
                except Exception as e:
                    crop, err = None, str(e)
                if crop:
                    img_bytes = crop
                    success = True
                    log("✅ 截图+QR定位 兜底成功")
                    break
                await asyncio.sleep(1)

        if not success:
            _app["state"] = STATE_ERROR
            _app["error"] = f"二维码提取失败: {err}"
            log(f"二维码提取失败: {err}")
            return

        _app["qrcode_b64"] = base64.b64encode(img_bytes).decode()
        _app["qrcode_ts"] = time.time()
        _app["last_fetch"] = time.time()
        _app["state"] = STATE_QR_READY
        _app["message"] = "请使用抖音 App 扫描二维码"

        # 保存文件
        out = Path(sys_path) / "output"
        out.mkdir(exist_ok=True)
        (out / "qrcode_web.png").write_bytes(img_bytes)

        log(f"二维码已就绪 (ts={int(_app['qrcode_ts'])})")

    except Exception as e:
        _app["state"] = STATE_ERROR
        _app["error"] = str(e)
        log(f"获取二维码失败: {e}")


# 登录态 Cookie 标志（抖音登录成功后会写入）
# 注意：ttwid / passport_csrf_token 未登录时也会写入，不能作为登录判据！
LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt", "sid_uc",
                 "uid_tt", "uid_tt_ss"}

# 二维码状态：抖音各接口字段名不统一，用启发式取值
QR_STATUS_MAP = {
    # 数值型
    0: "pending", 1: "scanned", 2: "confirmed", 3: "expired",
    # 字符串型
    "new": "pending", "pending": "pending", "wait": "pending",
    "scanned": "scanned", "scaned": "scanned", "confirmed": "confirmed",
    "success": "confirmed", "ok": "confirmed",
    "expired": "expired", "invalid": "expired", "cancel": "cancelled",
}

# 强检测脚本：多信号综合判断，全部带空值保护
PROBE_JS = r"""() => {
    const txt = (el) => {
        try { return el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : ''; }
        catch(e) { return ''; }
    };
    // 可见性：尺寸>0 且未被隐藏（offsetParent 对 fixed 元素恒为 null，不可靠）
    const vis = (el) => {
        try {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            if (r.width <= 1 || r.height <= 1) return false;
            const cs = getComputedStyle(el);
            return cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0';
        } catch(e) { return false; }
    };

    const panel = document.querySelector('#login-panel-new');
    const panelText = txt(panel);
    const panelVis = vis(panel);

    // 二维码
    const qrWrap = document.querySelector('#douyin_login_comp_scan_code');
    const qrImg = qrWrap ? qrWrap.querySelector('img') : null;
    const qrVisible = vis(qrImg) || !!(qrImg && qrImg.naturalWidth > 0);

    // 扫码后的中间态提示
    const SCAN_HINTS = ['扫描成功', '扫码成功', '请在手机上确认', '已扫描',
                        '请在客户端确认', '确认登录', '等待确认'];
    const scanSuccess = SCAN_HINTS.some(k => panelText.includes(k));

    // 是否仍停留在「扫码登录」主界面
    // （该界面自带"验证码登录"tab 的输入框，必须先识别出来，否则会误判
    //   hasCodeInput 为"已扫码等待输入验证码"）
    const SCAN_PAGE_HINTS = ['扫码登录', '扫一扫', '如何扫码',
                             '打开「抖音APP」', '打开抖音APP', '点击左上角'];
    const onScanPage = SCAN_PAGE_HINTS.some(k => panelText.includes(k));

    // 身份验证关键词（限定面板内，避免页面其他文字误判）
    const VERIFY_HINTS = ['身份验证', '接收短信', '刷脸', '验证登录密码',
                          '发送短信', '为保障账号安全', '请选择验证方式',
                          '扫码验证', '以确保为本人操作'];
    let inVerify = VERIFY_HINTS.some(k => panelText.includes(k));

    // ── MFA 验证弹窗检测（抖音验证是独立弹窗，不在登录面板内！）──
    const captchaEl = document.querySelector(
        '#captcha_container, [id*="captcha"], [class*="captcha-verify"], [class*="secsdk"]'
    );
    const captchaVisible = vis(captchaEl);

    // 扫描高 z-index 弹窗的文字（验证弹窗 z-index 通常很高）
    let overlayText = '';
    try {
        for (const el of document.querySelectorAll('div')) {
            if (el === document.body || el === document.documentElement) continue;
            const cs = getComputedStyle(el);
            if ((cs.position === 'fixed' || cs.position === 'absolute')
                && parseInt(cs.zIndex) > 50
                && el.offsetWidth > 300 && el.offsetHeight > 200
                && vis(el)) {
                const t = txt(el);
                if (t.length > 20) { overlayText = t.slice(0, 400); break; }
            }
        }
    } catch(e) {}

    // iframe 验证组件（必须真正可见：抖音会预加载隐藏的 captcha iframe）
    let verifyIframe = false;
    try {
        const ifEl = document.querySelector(
            'iframe[src*="captcha"], iframe[id*="captcha"], iframe[class*="verify"]'
        );
        if (ifEl) {
            const r = ifEl.getBoundingClientRect();
            verifyIframe = r.width > 60 && r.height > 60;
        }
    } catch(e) {}

    // ── Shadow DOM 穿透检测（字节 MFA 验证弹窗渲染在 shadow root 里，
    //    querySelector 无法穿透，但截图看得见 —— 必须递归搜索）──
    let shadowVerify = false;
    const shadowOptions = [];
    try {
        const roots = [document];
        for (const h of document.querySelectorAll('*')) {
            if (h.shadowRoot) roots.push(h.shadowRoot);
        }
        const SHADOW_WORDS = ['身份验证', '接收短信验证码', '手机刷脸验证',
                              '验证登录密码', '发送短信验证', '以确保为本人操作'];
        for (const root of roots) {
            let els;
            try { els = root.querySelectorAll('div, li, button, span, p, h1, h2, h3'); }
            catch(e) { continue; }
            for (const el of els) {
                const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                if (!t || t.length > 50) continue;
                for (const w of SHADOW_WORDS) {
                    if (t.includes(w)) {
                        shadowVerify = true;
                        if (t.length > 3 && !shadowOptions.includes(t)) shadowOptions.push(t);
                        break;
                    }
                }
            }
        }
    } catch(e) {}
    if (shadowVerify) inVerify = true;
    if (shadowOptions.length) items.push(...shadowOptions);

    // 综合验证信号
    const overlayHasVerify = VERIFY_HINTS.some(k => overlayText.includes(k));
    if (captchaVisible || verifyIframe || overlayHasVerify) inVerify = true;

    // 弹窗内可点击项（验证按钮的真正来源）
    if ((captchaVisible || overlayHasVerify) && !items.length) {
        try {
            const scope = captchaEl || null;
            const root = scope || document;
            root.querySelectorAll('li, div[class*="item"], [role="button"], div[class*="menu"]').forEach(el => {
                const t = txt(el);
                if (t.length > 1 && t.length < 40) items.push(t);
            });
        } catch(e) {}
    }

    // 输入框检测
    const hasCodeInput  = !!document.querySelector('input[placeholder*="验证码"]');
    const hasPwdInput   = !!document.querySelector('input[type="password"]');
    const hasPhoneInput = !!document.querySelector('input[placeholder*="手机"], input[type="tel"]');

    // 登录后用户标志
    const hasAvatar = !!document.querySelector('[class*="avatar"], [data-e2e*="avatar"]');

    // 面板内可点击项（作为验证选项候选）
    const items = [];
    if (panel) {
        try {
            panel.querySelectorAll('li, div[class*="item"], [role="button"], div[class*="menu"]').forEach(el => {
                const t = txt(el);
                if (t.length > 1 && t.length < 40) items.push(t);
            });
        } catch(e) {}
    }

    return {
        panelVis, panelText: panelText.slice(0, 400),
        qrVisible, scanSuccess, inVerify, onScanPage,
        captchaVisible, verifyIframe, shadowVerify,
        shadowOptions: shadowOptions.slice(0, 8),
        overlayText: overlayText.slice(0, 300),
        hasCodeInput, hasPwdInput, hasPhoneInput, hasAvatar,
        items: Array.from(new Set(items)).slice(0, 12),
        url: (location.href || '').slice(0, 100),
    };
}"""


async def probe(crawler) -> dict:
    """探测页面状态（带异常保护）"""
    try:
        return await crawler.page.evaluate(PROBE_JS)
    except Exception as e:
        return {"_error": str(e)}


async def probe_all_frames(crawler) -> dict:
    """
    跨 frame 探针：遍历主页面 + 所有 iframe 分别运行检测，合并结果。

    关键：抖音 MFA 验证弹窗渲染在独立 <iframe> 中（secsdk captcha 组件），
    主 frame 的 querySelector/Shadow 搜索都够不着 —— 必须逐 frame 检测。
    """
    page = crawler._page
    merged: dict = {
        "panelVis": False, "panelText": "", "qrVisible": False,
        "scanSuccess": False, "inVerify": False, "onScanPage": False,
        "captchaVisible": False, "verifyIframe": False, "shadowVerify": False,
        "shadowOptions": [], "overlayText": "", "items": [],
        "hasCodeInput": False, "hasPwdInput": False, "hasPhoneInput": False,
        "hasAvatar": False, "url": "", "_error": "",
    }

    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = [page.main_frame] if hasattr(page, "main_frame") else []

    got_main = False
    for frame in frames:
        try:
            r = await frame.evaluate(PROBE_JS)
        except Exception:
            continue
        if not r or r.get("_error"):
            continue

        # 主 frame 提供基础面板信息
        if frame is getattr(page, "main_frame", None) or not got_main:
            got_main = True
            for k in ("panelVis", "panelText", "qrVisible", "scanSuccess",
                      "onScanPage", "overlayText", "hasCodeInput",
                      "hasPwdInput", "hasPhoneInput", "hasAvatar", "url"):
                merged[k] = r.get(k, merged.get(k))

        # 任意 frame 的验证信号都有效（验证弹窗在 iframe 里）
        if r.get("inVerify"):
            merged["inVerify"] = True
        # 输入框标志跨 frame 合并（MFA 密码/验证码框都在 iframe 里，
        # 只看主 frame 会永远检测不到 —— "刷脸页误显示"的根因之一）
        for k in ("hasCodeInput", "hasPwdInput", "hasPhoneInput"):
            if r.get(k):
                merged[k] = True
        if r.get("shadowVerify"):
            merged["shadowVerify"] = True
        if r.get("captchaVisible"):
            merged["captchaVisible"] = True
        opts = r.get("shadowOptions") or []
        for o in opts:
            if o not in merged["shadowOptions"]:
                merged["shadowOptions"].append(o)
        for it in (r.get("items") or []):
            if it not in merged["items"]:
                merged["items"].append(it)

    merged["shadowOptions"] = merged["shadowOptions"][:8]
    merged["items"] = merged["items"][:12]
    return merged


async def has_login_cookie(crawler) -> bool:
    """通过 Cookie 判断是否已登录（最可靠的信号）"""
    try:
        cookies = await crawler._context.cookies()
        names = {c.get("name", "") for c in cookies}
        return bool(names & LOGIN_COOKIES)
    except Exception:
        return False


def _extract_qr_status(obj) -> str:
    """从 API 响应中启发式提取二维码状态（抖音各接口字段名不统一）"""
    if not isinstance(obj, dict):
        return ""

    found = []

    def walk(d, depth=0):
        if depth > 4 or not isinstance(d, dict):
            return
        for k, v in d.items():
            kl = str(k).lower()
            if any(key in kl for key in ("status", "state")) and isinstance(v, (int, str, bool)):
                if not isinstance(v, bool):
                    found.append(v)
            if isinstance(v, dict):
                walk(v, depth + 1)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                walk(v[0], depth + 1)

    try:
        walk(obj)
    except Exception:
        return ""

    for v in found:
        mapped = QR_STATUS_MAP.get(v)
        if mapped is None and isinstance(v, str):
            mapped = QR_STATUS_MAP.get(v.lower())
        if mapped:
            return mapped
    return ""


async def setup_network_monitor(crawler):
    """
    注册网络监听，直接捕获抖音的二维码状态 API。

    比 DOM 检测灵敏可靠得多：抖音扫码状态通过 XHR 轮询返回，
    页面 DOM 未必同步更新，但接口一定会有响应。
    """
    try:
        page = crawler._page
    except Exception:
        return

    async def on_response(response):
        try:
            url = (response.url or "").lower()
            if not any(k in url for k in (
                "qrcode", "qrconnect", "check_qr", "sso",
                "passport", "login", "verify", "captcha",
            )):
                return

            try:
                ctype = (response.headers or {}).get("content-type", "")
            except Exception:
                ctype = ""

            # ── 验证流程全量抓包：passport/captcha/verify 域的响应全量落库 ──
            try:
                raw_body = await response.text()
            except Exception:
                raw_body = ""
            cap_entry = None
            if any(k in url for k in ("passport", "captcha", "verify")) and raw_body:
                cap_entry = {
                    "dir": "RESP",
                    "method": "",
                    "url": (response.url or "")[:300],
                    "status": response.status,
                    "body": raw_body[:300000],
                    "ts": time.time(),
                }
                _app["verify_capture"].append(cap_entry)
                if len(_app["verify_capture"]) > 400:
                    _app["verify_capture"] = _app["verify_capture"][-400:]

            if "json" not in ctype.lower():
                return

            body = raw_body
            if not body or len(body) > 20000:
                return

            entry = {
                "url": (response.url or "")[:110],
                "code": response.status,
                "body": body[:260],
                "ts": time.time(),
            }

            status = ""
            try:
                status = _extract_qr_status(json.loads(body))
            except Exception:
                pass

            # MFA 身份验证信号（扫码确认后要求验证，双保险）
            compact = body.replace(" ", "")
            if '"account_flow":"verify"' in compact:
                if _app.get("qr_net_status") != "mfa_verify":
                    _app["qr_net_status"] = "mfa_verify"
                    _app["qr_net_ts"] = time.time()
                    entry["qr_status"] = "mfa_verify"
                    log("[网络] 收到 MFA 身份验证信号（account_flow=verify）")

            if status:
                entry["qr_status"] = status
                if status != _app.get("qr_net_status"):
                    _app["qr_net_status"] = status
                    _app["qr_net_ts"] = time.time()
                    log(f"[网络] 二维码状态 → {status}")

            _app["net_log"] = (_app.get("net_log", []) + [entry])[-12:]

        except Exception:
            pass

    async def on_request(request):
        """捕获验证流程的请求（含 POST 参数）—— 抓包核心"""
        try:
            url = request.url or ""
            if not any(k in url for k in ("passport", "captcha", "verify")):
                return
            post = ""
            try:
                post = request.post_data or ""
            except Exception:
                pass
            _app["verify_capture"].append({
                "dir": "REQ",
                "method": request.method,
                "url": url[:300],
                "post_data": post[:10000],
                "ts": time.time(),
            })
            if len(_app["verify_capture"]) > 400:
                _app["verify_capture"] = _app["verify_capture"][-400:]
        except Exception:
            pass

    try:
        page.on("response", on_response)
        page.on("request", on_request)
        log("网络监听已启用（二维码状态 + 验证流程全量抓包）")
    except Exception as e:
        log(f"网络监听注册失败: {e}")


# ── Shadow DOM 穿透交互（验证弹窗在 shadow root 里，普通 querySelector 够不着）──

SHADOW_CLICK_JS = r"""
(labels) => {
    const roots = [document];
    try { for (const h of document.querySelectorAll('*')) if (h.shadowRoot) roots.push(h.shadowRoot); } catch(e) {}
    let best = null, bestLen = 999;
    for (const root of roots) {
        let els;
        try { els = root.querySelectorAll('li, div, button, span, a, [role="button"]'); } catch(e) { continue; }
        for (const el of els) {
            const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (!t || t.length > 30) continue;
            for (const label of labels) {
                if (t.includes(label) && t.length < bestLen) { best = el; bestLen = t.length; }
            }
        }
    }
    if (best) { best.click(); return (best.innerText || '').trim(); }
    return null;
}
"""

SHADOW_INPUT_JS = r"""
(args) => {
    const roots = [document];
    try { for (const h of document.querySelectorAll('*')) if (h.shadowRoot) roots.push(h.shadowRoot); } catch(e) {}
    let target = null;
    for (const root of roots) {
        let inputs;
        try { inputs = root.querySelectorAll('input'); } catch(e) { continue; }
        for (const inp of inputs) {
            if (args.exactType && inp.type === args.exactType) { target = inp; break; }
            if (args.hint && (inp.placeholder || '').includes(args.hint)) { target = inp; break; }
        }
        if (target) break;
    }
    if (!target) {
        for (const root of roots) {
            let inputs;
            try { inputs = root.querySelectorAll('input'); } catch(e) { continue; }
            for (const inp of inputs) {
                const r = inp.getBoundingClientRect();
                if (r.width > 40 && inp.type !== 'hidden' && inp.type !== 'password' && inp.type !== 'checkbox') { target = inp; break; }
            }
            if (target) break;
        }
    }
    if (!target) return false;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(target, args.value);
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
}
"""


async def shadow_click(crawler, labels: list) -> Optional[str]:
    """穿透 Shadow DOM + 跨 iframe 点击含指定文字的元素，返回命中文字"""
    try:
        for frame in crawler._page.frames:
            try:
                r = await frame.evaluate(SHADOW_CLICK_JS, labels)
                if r:
                    return r
            except Exception:
                continue
    except Exception:
        pass
    return None


# ── 受信任输入管道（关键修复）──
# 抖音验证组件检测事件的 isTrusted 标志：
#   el.click() 产生合成事件(isTrusted=false) → 被静默忽略（短信发码从未生效的根因）
#   mouse.click / keyboard.type 走 CDP 输入管道(isTrusted=true) → 真实生效
# 策略：在任意 frame 里找到目标元素的视口坐标，再用鼠标/键盘真实操作

TRUSTED_LOCATE_TEXT_JS = r"""
(labels) => {
    function findIn(root) {
        let best = null, bestLen = 999;
        let els;
        try { els = root.querySelectorAll('li, div, button, span, a, [role="button"]'); }
        catch(e) { return null; }
        for (const el of els) {
            const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
            if (!t || t.length > 30) continue;
            for (const label of labels) {
                if (t.includes(label) && t.length < bestLen) { best = el; bestLen = t.length; }
            }
        }
        if (!best) return null;
        const r = best.getBoundingClientRect();
        if (r.width <= 1 || r.height <= 1) return null;
        return {x: r.x + r.width/2, y: r.y + r.height/2, text: (best.innerText||'').trim().slice(0, 30)};
    }
    let hit = findIn(document);
    if (hit) return hit;
    try {
        for (const h of document.querySelectorAll('*')) {
            if (h.shadowRoot) {
                hit = findIn(h.shadowRoot);
                if (hit) return hit;
            }
        }
    } catch(e) {}
    return null;
}
"""

TRUSTED_LOCATE_INPUT_JS = r"""
(args) => {
    function findIn(root) {
        let target = null;
        let inputs;
        try { inputs = root.querySelectorAll('input'); } catch(e) { return null; }
        for (const inp of inputs) {
            if (args.exactType && inp.type === args.exactType) { target = inp; break; }
            if (args.hint && (inp.placeholder || '').includes(args.hint)) { target = inp; break; }
        }
        if (!target && args.fallbackFirst) {
            for (const inp of inputs) {
                const r = inp.getBoundingClientRect();
                if (r.width > 40 && inp.type !== 'hidden' && inp.type !== 'checkbox'
                    && inp.type !== 'password') { target = inp; break; }
            }
        }
        if (!target) return null;
        const r = target.getBoundingClientRect();
        if (r.width <= 1) return null;
        return {x: r.x + r.width/2, y: r.y + r.height/2,
                ph: (target.placeholder || '').slice(0, 30)};
    }
    let hit = findIn(document);
    if (hit) return hit;
    try {
        for (const h of document.querySelectorAll('*')) {
            if (h.shadowRoot) {
                hit = findIn(h.shadowRoot);
                if (hit) return hit;
            }
        }
    } catch(e) {}
    return null;
}
"""


async def trusted_click(crawler, labels: list) -> Optional[str]:
    """
    跨 frame 真实点击含指定文字的元素。

    关键：使用 Playwright locator API（frame.get_by_text().click()）——
    Playwright 自动换算 iframe 内元素的全局坐标并走 CDP 输入管道，
    产生 isTrusted=true 事件，且不受 iframe 偏移影响。
    （旧方案 evaluate getBoundingClientRect 返回的是 iframe 局部坐标，
      直接当全局坐标用会全部点偏 —— 已废弃）
    """
    try:
        for frame in crawler._page.frames:
            for label in labels:
                try:
                    # 精确匹配（按钮文本恰为该词，如「验证」）
                    loc = frame.get_by_text(label, exact=True)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)
                        await asyncio.sleep(0.3)
                        log(f"[trusted-click] 命中「{label}」")
                        return label
                except Exception:
                    pass
                try:
                    # 包含匹配兜底（文本更长，如「接收短信验证码」）
                    loc = frame.get_by_text(label, exact=False)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)
                        await asyncio.sleep(0.3)
                        log(f"[trusted-click] 命中（包含）「{label}」")
                        return label
                except Exception:
                    continue
    except Exception:
        pass
    return None


async def trusted_input(crawler, value: str, hint: str = "",
                        exact_type: str = "", fallback_first: bool = False) -> bool:
    """
    跨 frame 定位输入框 → locator.click() trusted 聚焦 → keyboard.type 逐键输入。

    locator.click 由 Playwright 换算全局坐标，iframe 偏移自动处理；
    keyboard.type 产生 isTrusted=true 键盘事件，绕过合成输入检测。
    """
    selectors = []
    if exact_type:
        selectors.append(f'input[type="{exact_type}"]')
    if hint:
        selectors.append(f'input[placeholder*="{hint}"]')

    try:
        for frame in crawler._page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)   # trusted 聚焦
                        await asyncio.sleep(0.3)
                        await crawler._page.keyboard.type(value, delay=60)
                        await asyncio.sleep(0.3)
                        log(f"[trusted-input] 已键盘输入（选择器: {sel}）")
                        return True
                except Exception:
                    continue
            # 可见文本框兜底
            if fallback_first:
                try:
                    loc = frame.locator(
                        'input[type="text"], input:not([type])')
                    n = await loc.count()
                    for i in range(n):
                        el = loc.nth(i)
                        box = await el.bounding_box()
                        if box and box.get("width", 0) > 40:
                            await el.click(timeout=3000)
                            await asyncio.sleep(0.3)
                            await crawler._page.keyboard.type(value, delay=60)
                            await asyncio.sleep(0.3)
                            log(f"[trusted-input] 兜底输入（第{i}个文本框）")
                            return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


# ── 实体按钮抓取：从验证页面动态提取按钮（文本+全局坐标）──
# 用 locator.bounding_box()：Playwright 自动换算 iframe 偏移，
# 返回的就是全局视口坐标，点击时 mouse.click 即可信命中。

VERIFY_BTN_LABELS = ["接收短信验证码", "手机刷脸验证", "验证登录密码",
                     "发送短信验证", "接收短信"]

BTN_METHOD_MAP = {
    "接收短信": "sms", "发送短信": "sms",
    "密码": "password",
    "刷脸": "face", "人脸": "face",
}


async def capture_verify_buttons(crawler) -> list:
    """从验证页面抓取验证按钮实体。返回 [{text, x, y, method}]"""
    buttons: list = []
    page = crawler._page
    try:
        for frame in page.frames:
            for label in VERIFY_BTN_LABELS:
                # 已抓到同义项则跳过
                if any(b["text"] == label for b in buttons):
                    continue
                m = "sms"
                for kw, mm in BTN_METHOD_MAP.items():
                    if kw in label:
                        m = mm
                        break
                try:
                    loc = frame.get_by_text(label)
                    n = await loc.count()
                    for i in range(min(n, 2)):
                        box = await loc.nth(i).bounding_box()
                        if not box or box.get("width", 0) < 60 \
                           or box.get("height", 0) < 14:
                            continue
                        # 可见性粗筛（排除隐藏容器里的重复项）
                        buttons.append({
                            "text": label,
                            "x": round(box["x"] + box["width"] / 2),
                            "y": round(box["y"] + box["height"] / 2),
                            "method": m,
                        })
                        break
                except Exception:
                    continue
    except Exception:
        pass
    # 去重（同 text 同坐标 ±4px）
    dedup = []
    for b in buttons:
        if not any(abs(b["x"] - d["x"]) <= 4 and abs(b["y"] - d["y"]) <= 4
                   for d in dedup):
            dedup.append(b)
    return dedup[:6]


# 验证弹窗按钮坐标兜底（1440x900 视口，从截图实测换算）
# 验证弹窗在隔离 iframe 里，JS evaluate 可能够不着，鼠标坐标点击不受隔离限制
VERIFY_COORDS = {
    "sms": (717, 459),        # 接收短信验证码
    "face": (717, 532),       # 手机刷脸验证
    "password": (717, 611),   # 验证登录密码
}

# 刷脸验证页上的二维码区域（1440x900 视口，截图实测换算，稍放宽边距）
FACE_QR_CLIP = {"x": 645, "y": 400, "width": 150, "height": 150}
# 验证子页面底部的"选择其他验证方式"返回链接
BACK_TO_OPTIONS_COORD = (717, 656)


def _detect_qr_in_png(data: bytes, pad: int = 24):
    """
    用 OpenCV 在截图中定位真实二维码，找到则返回带边距的裁剪 PNG bytes。
    定位不到（截到空白/半黑半白/无关画面）返回 None —— 内容级验证，
    彻底避免把错误区域当二维码展示。

    检测重试链：原图 → 2x 放大（小码） → CLAHE 对比度增强（低对比度码）
    """
    try:
        import numpy as _np
        import cv2
        arr = _np.frombuffer(data, dtype=_np.uint8)
        im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if im is None:
            return None

        det = cv2.QRCodeDetector()

        def _find(img):
            try:
                return det.detect(img)[1]
            except Exception:
                return None

        # 重试链：原图 → 2x 放大 → CLAHE 增强
        points = _find(im)
        scale = 1.0
        if points is None:
            big = cv2.resize(im, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
            points = _find(big)
            scale = 2.0
        if points is None:
            gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
            gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
            points = _find(gray)
            scale = 1.0
        if points is None:
            return None

        pts = points[0] / scale
        x0, y0 = int(pts[:, 0].min()), int(pts[:, 1].min())
        x1, y1 = int(pts[:, 0].max()), int(pts[:, 1].max())
        h, w = im.shape[:2]
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
        if x1 - x0 < 60 or y1 - y0 < 60:
            return None
        ok, buf = cv2.imencode(".png", im[y0:y1, x0:x1])
        return bytes(buf) if ok else None
    except Exception:
        return None  # cv2 不可用时视为失败，不展示未验证图片


_last_face_debug_ts = 0.0


async def capture_verify_qrcode(crawler) -> str:
    """
    截取刷脸验证页上"手机扫码进行刷脸"的二维码，返回 base64；失败返回 ""。

    三通道（全部经过二维码内容校验，截不到真码就不返回）：
      1. 全页截图 + OpenCV 定位二维码 → 精确裁剪（布局无关，最可靠）
      2. 验证 iframe 元素整体截图（内含二维码）
      3. 固定坐标裁剪兜底
    全部失败时把整页截图落盘 output/face_page_debug.png，便于诊断。
    """
    global _last_face_debug_ts
    page = crawler._page

    # 1) 全页截图 + QR 定位：直接确认"截到的真的是二维码"再裁剪
    full_img = None
    try:
        full_img = await page.screenshot(type="png")
        crop = _detect_qr_in_png(full_img)
        if crop:
            log("📸 刷脸二维码：全页截图+QR定位 成功")
            return base64.b64encode(crop).decode()
    except Exception:
        pass

    # 2) 验证 iframe 整体截图（二维码在 iframe 内部时整框展示）
    try:
        for sel in ("iframe[id*='captcha']", "iframe[src*='captcha']",
                    "iframe[src*='secsdk']", "iframe[class*='secsdk']"):
            el = await page.query_selector(sel)
            if not el:
                continue
            box = await el.bounding_box()
            if not box or box["width"] < 250 or box["height"] < 250:
                continue
            img = await el.screenshot(type="png")
            if _detect_qr_in_png(img):
                log("📸 刷脸二维码：验证 iframe 截图 成功")
                return base64.b64encode(img).decode()
    except Exception:
        pass

    # 3) 固定坐标兜底
    try:
        img = await page.screenshot(type="png", clip=FACE_QR_CLIP)
        crop = _detect_qr_in_png(img)
        if crop:
            log("📸 刷脸二维码：坐标裁剪 成功")
            return base64.b64encode(crop).decode()
    except Exception:
        pass

    # 全部失败：落盘整页截图供诊断（5s 节流）
    if full_img and time.time() - _last_face_debug_ts > 5:
        _last_face_debug_ts = time.time()
        try:
            out = Path("output")
            out.mkdir(exist_ok=True)
            (out / "face_page_debug.png").write_bytes(full_img)
            log("⚠️ 刷脸二维码三通道均未检出，整页截图已存 output/face_page_debug.png")
        except Exception:
            pass
    return ""


# ── 个人主页数据抓取（/user/self，需登录态）──

async def scrape_self_profile(crawler) -> dict:
    """
    抓包 + 抓取 https://www.douyin.com/user/self 个人主页

    双通道：
    1. 网络层拦截 API 响应（user/profile/self + aweme/post）—— 结构化最全
    2. DOM 提取兜底 —— API 改版时仍可用
    结果落盘 output/self_profile_*.json
    """
    page = crawler._page
    captured: list = []

    async def on_resp(resp):
        try:
            u = resp.url or ""
            if any(k in u for k in ("user/profile/self", "aweme/post",
                                    "user/profile/other", "user/favorite")):
                body = await resp.text()
                if body:
                    captured.append({"url": u[:160], "body": body[:80000]})
        except Exception:
            pass

    try:
        page.on("response", on_resp)
    except Exception:
        pass

    try:
        target = ("https://www.douyin.com/user/self"
                  "?from_tab_name=main&showSubTab=video&showTab=post")
        log(f"[抓取] 导航到个人主页...")
        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)

        # 滚动触发作品列表懒加载
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 900)")
            await asyncio.sleep(1.5)

        # DOM 提取（抖音用 data-e2e 属性锚点）
        dom = await page.evaluate("""() => {
            const txt = el => (el && el.innerText || '').replace(/\\s+/g, ' ').trim();
            const q = s => document.querySelector(s);
            const user = {};
            const info = q('[data-e2e="user-info"]');
            user.meta_text = txt(info).slice(0, 600);
            const nameEl = q('h1, [class*="nick"], [data-e2e="user-info"] h1');
            user.nickname = txt(nameEl).slice(0, 60);
            user.title = document.title;
            user.url = location.href;

            const videos = [];
            document.querySelectorAll(
                '[data-e2e="user-post-list"] li, [data-e2e="scroll-list-item"] li'
            ).forEach(li => {
                const a = li.querySelector('a[href*="/video/"], a[href*="/note/"]');
                const img = li.querySelector('img');
                if (a) videos.push({
                    href: a.href,
                    desc: txt(li).slice(0, 120),
                    cover: img ? img.src.slice(0, 200) : '',
                });
            });
            return {user, videos: videos.slice(0, 40)};
        }""")

        # 解析拦截到的 API 响应
        profile_api, post_api = None, None
        for c in captured:
            try:
                j = json.loads(c["body"])
            except Exception:
                continue
            if "user/profile" in c["url"] and not profile_api:
                profile_api = j
            elif "aweme/post" in c["url"] and not post_api:
                post_api = j

        user_info = {}
        if isinstance(profile_api, dict) and isinstance(profile_api.get("user"), dict):
            u = profile_api["user"]
            av = u.get("avatar_larger") or u.get("avatar_300x300") or {}
            user_info = {
                "nickname": u.get("nickname"),
                "douyin_id": u.get("unique_id") or u.get("short_id"),
                "signature": (u.get("signature") or "")[:200],
                "followers": u.get("follower_count"),
                "following": u.get("following_count"),
                "total_likes": u.get("total_favorited"),
                "aweme_count": u.get("aweme_count"),
                "avatar": (av.get("url_list") or [""])[0] if isinstance(av, dict) else "",
            }

        videos = []
        if isinstance(post_api, dict) and isinstance(post_api.get("aweme_list"), list):
            for a in post_api["aweme_list"][:40]:
                st = a.get("statistics") or {}
                videos.append({
                    "aweme_id": a.get("aweme_id"),
                    "url": f"https://www.douyin.com/video/{a.get('aweme_id')}",
                    "desc": (a.get("desc") or "")[:120],
                    "likes": st.get("digg_count"),
                    "comments": st.get("comment_count"),
                    "shares": st.get("share_count"),
                    "create_time": a.get("create_time"),
                })

        result = {
            "ok": True,
            "source": "api" if (user_info or videos) else "dom",
            "user": user_info or dom.get("user", {}),
            "dom_meta": (dom.get("user") or {}).get("meta_text", "")[:300],
            "videos": videos or dom.get("videos", []),
            "api_captured": len(captured),
            "page_title": dom.get("title", ""),
        }

        # 落盘（含原始 API 报文，供进一步分析 = 完整抓包产物）
        out = Path(sys_path) / "output" / f"self_profile_{int(time.time())}.json"
        out.write_text(json.dumps(
            {"summary": result, "raw_api": captured},
            ensure_ascii=False, indent=2), encoding="utf-8")
        result["saved_to"] = str(out)
        log(f"[抓取] 完成: 用户={result['user'].get('nickname', '?')} "
            f"作品={len(result['videos'])} API报文={len(captured)} → {out.name}")
        return result

    except Exception as e:
        log(f"[抓取] 失败: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        try:
            page.remove_listener("response", on_resp)
        except Exception:
            pass


async def check_success(crawler) -> bool:
    """综合判断登录是否成功：面板消失 + 登录 Cookie 存在"""
    p = await probe(crawler)
    if p.get("_error"):
        return False
    if p.get("panelVis"):
        return False
    return await has_login_cookie(crawler)


async def mark_success(crawler, msg: str = "登录成功！"):
    """标记登录成功，保存 Cookie，然后跳转到已登录的抖音官网"""
    global _app
    _app["state"] = STATE_SUCCESS
    _app["user_info"] = {"login": True}
    _app["message"] = msg
    cf = await crawler.save_cookies()
    _app["cookie_file"] = cf
    log(f"✅ {msg} Cookie: {cf}")

    # 跳转到登录后的抖音官网
    try:
        log("正在跳转抖音官网（已登录状态）...")
        await crawler.page.goto(
            "https://www.douyin.com/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await asyncio.sleep(4)
        _app["message"] = "登录成功！已进入抖音官网"
        log("✅ 已跳转到抖音官网（首页）")
    except Exception as e:
        log(f"跳转官网失败: {e}")

    return {"ok": True, "next": "success"}


async def poll_login_status():
    """
    轮询登录状态（重写版）

    关键修复：
    - 不再一检测到二维码"消失"就 reload（扫码后二维码会变成确认提示，
      旧逻辑误判为过期并刷新页面，直接冲掉扫码状态 —— 这是"扫码无反应"的根因）
    - 多重信号判定，优先级：验证码框 > 密码框 > 验证页 > 已扫码 > 成功
    - 登录成功以 Cookie 为准，最可靠
    """
    global _app

    last_sig = None
    qr_gone_since = None      # 二维码消失起始时间
    success_pending = 0       # 登录成功确认计数
    last_cap_save = 0         # 抓包自动落盘计时

    while True:
        try:
            # 抓包数据定期自动落盘（防重启丢失）
            if time.time() - last_cap_save > 30:
                last_cap_save = time.time()
                try:
                    cap = _app.get("verify_capture") or []
                    if cap:
                        auto = Path(sys_path) / "output" / "verify_capture_auto.json"
                        auto.write_text(json.dumps(cap, ensure_ascii=False),
                                        encoding="utf-8")
                except Exception:
                    pass

            crawler = _app.get("crawler")
            if not crawler or not crawler._page:
                await asyncio.sleep(2)
                continue

            # 操作进行中，让路
            if _app["busy"]:
                await asyncio.sleep(1)
                continue

            # 只在活跃状态轮询
            if _app["state"] not in (STATE_QR_READY, STATE_SCANNED,
                                     STATE_VERIFY_NEEDED, STATE_FACE_WAIT,
                                     STATE_INPUT_CODE, STATE_INPUT_PWD):
                await asyncio.sleep(1)
                continue

            p = await probe_all_frames(crawler)
            if p.get("_error"):
                # 导航中，短暂异常，忽略
                await asyncio.sleep(2)
                continue

            # 去重日志
            sig = (f"{p['panelVis']}|{p['qrVisible']}|{p['scanSuccess']}|"
                   f"{p['inVerify']}|{p['onScanPage']}|{p['hasCodeInput']}")
            if sig != last_sig:
                log(f"[探针] 面板={p['panelVis']} 二维码={p['qrVisible']} "
                    f"扫码页={p['onScanPage']} 已扫={p['scanSuccess']} "
                    f"验证={p['inVerify']}")
                if p["panelText"]:
                    log(f"        文字: {p['panelText'][:100]}")
                last_sig = sig

            # 保存调试信息供前端展示
            _app["debug"] = {
                "panel_text": p["panelText"][:200],
                "overlay_text": p.get("overlayText", "")[:150],
                "captcha_visible": p.get("captchaVisible", False),
                "verify_iframe": p.get("verifyIframe", False),
                "qr_visible": p["qrVisible"],
                "on_scan_page": p["onScanPage"],
                "scan_success": p["scanSuccess"],
                "in_verify": p["inVerify"],
                "has_code_input": p["hasCodeInput"],
                "has_pwd_input": p["hasPwdInput"],
            }

            # ═══ 状态判定（按优先级，扫码界面必须优先识别） ═══

            # 1) 登录成功：面板消失 + 登录 Cookie（最可靠）
            if not p["panelVis"]:
                success_pending += 1
                if success_pending >= 2:  # 连续 2 次确认，避免导航抖动
                    if await has_login_cookie(crawler):
                        success_pending = 0
                        await mark_success(crawler, "登录成功！")
                        continue
            else:
                success_pending = 0

            # 2) 网络层信号（比 DOM 灵敏，DOM 未必同步但接口一定有响应）
            net = _app.get("qr_net_status", "")

            # 2pre) MFA 锁定：一旦收到验证信号，最高优先级，
            #       禁止一切自动刷新，状态强制进入验证选择
            if net == "mfa_verify":
                if not _app.get("mfa_lock"):
                    _app["mfa_lock"] = True
                    log("🔒 MFA 锁定：禁止自动刷新，等待验证操作")
                if _app["state"] not in (STATE_VERIFY_NEEDED, STATE_SUCCESS,
                                         STATE_INPUT_CODE, STATE_INPUT_PWD,
                                         STATE_FACE_WAIT):
                    _app["state"] = STATE_VERIFY_NEEDED
                    # 立即填充默认验证选项（验证弹窗在隔离 iframe，
                    # 探针可能提取不到文字 —— 按钮必须无条件展示）
                    if not _app["verify_options"]:
                        _app["verify_options"] = [
                            "接收短信验证码", "手机刷脸验证",
                            "验证登录密码", "发送短信验证",
                        ]
                    _app["message"] = "检测到身份验证，请选择验证方式"
                    log(f"→ 网络层 MFA 信号：进入身份验证状态 选项={_app['verify_options']}")

            # 2a) 二维码已过期 → 延迟重取（给扫码确认的 MFA 响应留 8s 到达窗口）
            if (net == "expired" and _app["state"] in (STATE_QR_READY, STATE_SCANNED)
                    and not _app.get("mfa_lock")):
                if qr_gone_since is None:
                    qr_gone_since = time.time()
                    log("接口报告二维码过期，等待 8s 确认无后续信号...")
                elif time.time() - qr_gone_since > 8:
                    log("确认过期，重新获取二维码")
                    qr_gone_since = None
                    _app["qr_net_status"] = ""
                    await fetch_qrcode()
                    continue
                await asyncio.sleep(1)
                continue

            # 2b) 手机已确认 → 等页面落地（可能直接登录，也可能进验证）
            if net == "confirmed" and _app["state"] != STATE_SUCCESS:
                if _app["state"] != STATE_SCANNED:
                    _app["state"] = STATE_SCANNED
                    _app["message"] = "手机已确认，正在完成登录..."
                    log("→ 网络层：手机已确认，等待页面落地")
                await asyncio.sleep(1)
                continue

            # 2c) 已扫码，等手机点确认（DOM 常滞后，这里先落地状态）
            if net == "scanned" and _app["state"] == STATE_QR_READY:
                _app["state"] = STATE_SCANNED
                _app["message"] = "扫码成功，请在手机上点击「确认登录」"
                log("→ 网络层：已扫码，等待手机确认")
                qr_gone_since = None
                await asyncio.sleep(2)
                continue

            # 2c+) 验证子页面（刷脸验证页含一个新二维码，截取给前端展示）
            #      face_wait 状态下持续截取；verify 状态下若探针文字表明
            #      已在刷脸子页（含 扫一扫/二维码 等），也立即截取
            panel_txt = p.get("panelText") or ""
            on_face_subpage = any(k in panel_txt for k in
                                  ("刷脸", "人脸", "扫一扫", "扫描二维码"))
            if _app["state"] == STATE_FACE_WAIT or (
                    _app["state"] == STATE_VERIFY_NEEDED and on_face_subpage):
                fq = await capture_verify_qrcode(crawler)
                if fq and fq != _app.get("face_qrcode_b64"):
                    _app["face_qrcode_b64"] = fq
                    log("📸 已提取刷脸验证二维码")

            # 2c++) 验证选择页：动态抓取实体按钮（文本+全局坐标）
            if _app["state"] == STATE_VERIFY_NEEDED:
                vbs = await capture_verify_buttons(crawler)
                if vbs:
                    old = _app.get("verify_buttons") or []
                    if vbs != old:
                        _app["verify_buttons"] = vbs
                        log(f"🔘 实体按钮已抓取: "
                            + ", ".join(b["text"] for b in vbs))

            # 3) 身份验证页面（明确关键词，含 Shadow DOM 穿透检测）
            #    注意：刷脸二维码子页面的文字也含"刷脸"，会命中 VERIFY_HINTS，
            #    若此时已在 face_wait，绝不能降级回 verify（否则前端回落显示
            #    旧登录二维码 —— 这就是"刷脸时显示旧码"的根因）
            if p["inVerify"] and (p["panelVis"] or p.get("shadowVerify")):
                if _app["state"] == STATE_FACE_WAIT and on_face_subpage:
                    pass  # 已在刷脸二维码子页，保持 face_wait，不降级
                elif _app["state"] != STATE_VERIFY_NEEDED:
                    _app["state"] = STATE_VERIFY_NEEDED
                    _app["message"] = "检测到身份验证，请选择验证方式"
                    log("→ 检测到身份验证页面"
                        + ("（Shadow DOM）" if p.get("shadowVerify") else ""))

                    # 优先用 Shadow DOM 里解析出的选项（真正的验证弹窗内容）
                    KEYWORDS = ("短信", "密码", "刷脸", "人脸")
                    opts = p.get("shadowOptions") or []
                    if not opts:
                        opts = [t for t in p["items"]
                                if any(k in t for k in KEYWORDS)]
                    _app["verify_options"] = opts[:6] if opts else [
                        "接收短信验证码", "手机刷脸验证",
                        "验证登录密码", "发送短信验证",
                    ]
                    log(f"  验证选项: {_app['verify_options']}")
                qr_gone_since = None
                await asyncio.sleep(2)
                continue

            # 3) 扫码成功，等待手机确认
            if p["scanSuccess"] and p["panelVis"]:
                if _app["state"] != STATE_SCANNED:
                    _app["state"] = STATE_SCANNED
                    _app["message"] = "扫码成功，请在手机上确认"
                    log("→ 扫码成功，等待手机确认")
                qr_gone_since = None
                await asyncio.sleep(2)
                continue

            # 4) 仍在「扫码登录」主界面 → 等待扫码
            #    注意：该界面自带"验证码登录"tab 输入框，
            #    必须先在这里拦截，否则会误判为"已扫码等待输验证码"
            if p["panelVis"] and (p["qrVisible"] or p["onScanPage"]):
                if _app["state"] not in (STATE_QR_READY, STATE_SCANNED,
                                         STATE_VERIFY_NEEDED):
                    # 从刷脸/验证流程退回扫码页 = 该流程已中断
                    prev = _app["state"]
                    _app["state"] = STATE_QR_READY
                    _app["face_qrcode_b64"] = ""
                    _app["mfa_lock"] = False  # 已回到登录主界面，解除验证锁定
                    if prev == STATE_FACE_WAIT:
                        _app["message"] = "刷脸未完成或已超时，请重新扫码后再试"
                        log("→ 刷脸流程中断，退回扫码界面（旧刷脸码已清除）")
                    else:
                        _app["message"] = "请使用抖音 App 扫描二维码"
                        log("→ 回到扫码界面，等待扫码")
                qr_gone_since = None
                await asyncio.sleep(2)
                continue

            # 验证流程上下文：只有 MFA 锁定或页面确实处于验证弹窗时，
            # 输入框才代表"验证" —— 否则登录面板自带的"密码登录/验证码登录"
            # tab 会被误判成"请输入密码/验证码"（刷脸中断回退时的典型误判）
            in_verify_ctx = bool(p["inVerify"] or _app.get("mfa_lock"))

            # 5) 验证码输入框：验证流程上下文中，跨任意 frame 检出即算数
            #    （MFA 弹窗打开后登录面板可能已消失，不能要求 panelVis）
            if (p["hasCodeInput"] and in_verify_ctx and not p["onScanPage"]):
                if _app["state"] != STATE_INPUT_CODE:
                    _app["state"] = STATE_INPUT_CODE
                    _app["face_qrcode_b64"] = ""
                    _app["message"] = "请输入短信验证码"
                    log("→ 检测到验证码输入框（验证流程内）")
                qr_gone_since = None
                await asyncio.sleep(2)
                continue

            # 6) 密码输入框：同上（刷脸点错/中断跳到密码验证框时在此纠偏）
            if (p["hasPwdInput"] and in_verify_ctx and not p["onScanPage"]):
                if _app["state"] != STATE_INPUT_PWD:
                    prev = _app["state"]
                    _app["state"] = STATE_INPUT_PWD
                    _app["face_qrcode_b64"] = ""
                    if prev == STATE_FACE_WAIT:
                        _app["message"] = "当前是「验证登录密码」流程，请输入登录密码"
                        log("→ 刷脸状态纠偏：页面实际是密码验证框")
                    else:
                        _app["message"] = "请输入登录密码"
                    log("→ 检测到密码输入框（验证流程内）")
                qr_gone_since = None
                await asyncio.sleep(2)
                continue

            # 6) 二维码过期判定（严格条件，避免误杀扫码状态）
            #    仅在：等待扫码 + 二维码不可见 + 面板仍在 + 无任何进展信号
            if _app["state"] == STATE_QR_READY:
                if not p["qrVisible"] and p["panelVis"]:
                    now = time.time()
                    if qr_gone_since is None:
                        qr_gone_since = now
                    elif now - qr_gone_since > 20:
                        log("二维码确认失效，重新获取...")
                        qr_gone_since = None
                        await fetch_qrcode()
                else:
                    qr_gone_since = None

            await asyncio.sleep(2)

        except Exception as e:
            log(f"轮询异常: {e}")
            await asyncio.sleep(3)


async def do_verify_method(method: str, extra: dict = None):
    """
    点击对应的身份验证方式

    Args:
        method: sms / password / face
        extra: {phone: "xxx"} 或 {password: "xxx"}
    """
    global _app

    if _app["busy"]:
        return {"ok": False, "error": "正在处理中，请稍候"}

    _app["busy"] = True
    try:
        crawler = _app.get("crawler")
        if not crawler:
            return {"ok": False, "error": "浏览器未初始化"}

        extra = extra or {}
        log(f"执行验证方式: {method}")

        # 关键词映射（用于点击页面选项）
        label_map = {
            "sms": ["接收短信", "短信验证", "发送短信"],
            "password": ["验证登录密码", "密码"],
            "face": ["刷脸", "人脸"],
        }
        labels = label_map.get(method, [])

        # 第一优先：实体按钮坐标（前端从页面动态抓取的真实位置，trusted 点击）
        bx, by = extra.get("x"), extra.get("y")
        if bx and by:
            try:
                await crawler.page.mouse.click(int(bx), int(by))
                selected = f"实体按钮({bx},{by})"
                log(f"🔘 实体按钮点击: ({bx},{by}) method={method}")
            except Exception as e:
                log(f"实体按钮点击失败: {e}")
                selected = None

        # 第二优先：locator trusted 点击
        if not selected:
            selected = await trusted_click(crawler, labels)

        # 坐标点击兜底（隔离 iframe 中 JS 够不着时，鼠标坐标不受限制）
        if not selected:
            coords = VERIFY_COORDS.get(method)
            if coords:
                log(f"JS 点击未命中，改用坐标点击 {method} → {coords}")
                try:
                    await crawler.page.mouse.click(*coords)
                    selected = f"坐标点击:{method}"
                    await asyncio.sleep(2)
                except Exception as e:
                    log(f"坐标点击失败: {e}")

        if not selected:
            return {"ok": False, "error": f"未找到「{method}」对应的验证选项"}

        log(f"已点击选项: {selected}")
        await asyncio.sleep(2)

        # 根据方式执行后续
        if method == "sms":
            phone = extra.get("phone", "")
            if phone:
                _app["phone"] = phone
                await trusted_input(crawler, phone, hint="手机")
                await asyncio.sleep(1)
                send_hit = await trusted_click(
                    crawler, ["发送验证码", "获取验证码", "发送短信"]
                )
                if send_hit:
                    _app["state"] = STATE_INPUT_CODE
                    _app["message"] = f"验证码已发送至 {phone[:3]}****{phone[-4:]}"
                    log("验证码已发送，等待用户输入")
                    return {"ok": True, "next": "input_code", "selected": selected}
                else:
                    return {"ok": False, "error": "发送验证码失败"}
            else:
                _app["state"] = STATE_INPUT_CODE
                _app["message"] = "请输入手机号以接收验证码"
                return {"ok": True, "next": "need_phone"}

        elif method == "password":
            pwd = extra.get("password", "")
            if pwd:
                await trusted_input(crawler, pwd, exact_type="password")
                await asyncio.sleep(1)
                await trusted_click(crawler, ["验证", "登录", "提交", "确认"])
                await asyncio.sleep(5)
                if await check_success(crawler):
                    return await mark_success(crawler, "密码验证登录成功!")
                return {"ok": False, "error": "密码验证未通过，请重试或选择其他方式"}
            else:
                _app["state"] = STATE_INPUT_PWD
                _app["message"] = "请输入登录密码"
                return {"ok": True, "next": "input_pwd"}

        elif method == "face":
            # 清掉上一次的刷脸码，避免展示过期图
            _app["face_qrcode_b64"] = ""
            # 立即尝试截取刷脸页新二维码；截到了才进入 face_wait。
            # 截不到说明点完选项后页面并不是刷脸码页（坐标误触/流程变化），
            # 盲目进入 face_wait 会导致面板永远转圈 —— 交给监控循环纠偏
            try:
                await asyncio.sleep(2.5)
                fq = await capture_verify_qrcode(crawler)
                if fq:
                    _app["face_qrcode_b64"] = fq
                    _app["state"] = STATE_FACE_WAIT
                    _app["message"] = "请在手机上完成人脸识别"
                    log("📸 已提取刷脸验证二维码（点击后即时）")
                    return {"ok": True, "next": "face_wait"}
                else:
                    _app["state"] = STATE_VERIFY_NEEDED
                    _app["message"] = "未检测到刷脸二维码，请重新选择验证方式"
                    log("⚠️ 点击刷脸后页面无二维码，退回验证选择（可能误触其他选项）")
                    return {"ok": False,
                            "error": "未检测到刷脸二维码：页面未进入刷脸流程，"
                                     "请重新点击「手机刷脸验证」或选择其他方式"}
            except Exception as e:
                _app["state"] = STATE_VERIFY_NEEDED
                log(f"刷脸截码异常: {e}")
                return {"ok": False, "error": f"刷脸二维码提取失败: {e}"}

        return {"ok": False, "error": f"未知验证方式: {method}"}

    except Exception as e:
        log(f"执行验证失败: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _app["busy"] = False


async def do_submit_code(code: str):
    """提交短信验证码"""
    global _app

    if _app["busy"]:
        return {"ok": False, "error": "正在处理中"}

    _app["busy"] = True
    try:
        crawler = _app.get("crawler")
        if not crawler:
            return {"ok": False, "error": "浏览器未初始化"}

        log(f"提交验证码: {code}")

        # 第一通道：trusted 管道（跨 frame 定位 + 键盘输入）
        ok_in = await trusted_input(crawler, code, hint="验证码")
        await asyncio.sleep(1)
        hit = await trusted_click(crawler, ["验证", "登录", "提交", "确认"])

        # 第二通道：坐标兜底（"接收短信验证码"子页实测坐标：
        #   输入框中心 (717,377)、「验证」按钮 (809,608)，1440x900 视口）
        if not (ok_in and hit):
            log(f"trusted 未完全命中（输入={ok_in} 按钮={hit}），坐标兜底提交")
            try:
                await crawler.page.mouse.click(717, 377)
                await asyncio.sleep(0.4)
                await crawler.page.keyboard.type(code, delay=60)
                await asyncio.sleep(0.5)
                await crawler.page.mouse.click(809, 608)
            except Exception as e:
                log(f"坐标兜底失败: {e}")

        await asyncio.sleep(5)

        # 提交后 Cookie 可能延迟写入，多次确认
        for attempt in range(3):
            if await check_success(crawler):
                return await mark_success(crawler, "验证码登录成功!")
            await asyncio.sleep(2)

        # 未成功：回到验证选择，让用户重试
        p = await probe(crawler)
        if p.get("inVerify") or p.get("hasCodeInput"):
            _app["state"] = STATE_VERIFY_NEEDED
            _app["message"] = "验证码可能错误，请重试或选择其他方式"
            return {"ok": False, "error": "验证未通过，请重试"}
        return {"ok": False, "error": "登录状态未知，请检查验证码是否正确"}

    except Exception as e:
        log(f"提交验证码失败: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _app["busy"] = False


async def do_submit_password(pwd: str):
    """提交验证密码"""
    global _app

    if _app["busy"]:
        return {"ok": False, "error": "正在处理中"}

    _app["busy"] = True
    try:
        crawler = _app.get("crawler")
        if not crawler:
            return {"ok": False, "error": "浏览器未初始化"}

        log("提交验证密码")

        await trusted_input(crawler, pwd, exact_type="password")
        await asyncio.sleep(1)
        await trusted_click(crawler, ["验证", "登录", "提交", "确认"])
        await asyncio.sleep(5)

        if await check_success(crawler):
            return await mark_success(crawler, "密码验证登录成功!")

        _app["state"] = STATE_VERIFY_NEEDED
        return {"ok": False, "error": "密码验证失败，请重试或选择其他方式"}

    except Exception as e:
        log(f"提交密码失败: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _app["busy"] = False


async def do_send_sms(phone: str):
    """发送短信验证码"""
    global _app

    if _app["busy"]:
        return {"ok": False, "error": "正在处理中"}

    _app["busy"] = True
    try:
        crawler = _app.get("crawler")
        if not crawler:
            return {"ok": False, "error": "浏览器未初始化"}

        log(f"发送验证码到 {phone[:3]}****{phone[-4:]}")

        _app["phone"] = phone
        await trusted_input(crawler, phone, hint="手机")
        await asyncio.sleep(1)

        send_hit = await trusted_click(
            crawler, ["发送验证码", "获取验证码", "发送短信"]
        )
        if send_hit:
            _app["state"] = STATE_INPUT_CODE
            _app["message"] = f"验证码已发送至 {phone[:3]}****{phone[-4:]}"
            return {"ok": True, "next": "input_code"}
        return {"ok": False, "error": "发送失败，请检查手机号"}

    except Exception as e:
        log(f"发送验证码失败: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        _app["busy"] = False


async def do_refresh():
    """
    刷新二维码

    若已进入扫码/验证流程则拒绝，并提示用户用"重新开始"。
    """
    global _app
    if _app["busy"]:
        return {"ok": False, "error": "正在处理中，请稍候"}

    if _app["state"] in _NO_REFRESH_STATES:
        return {
            "ok": False,
            "error": "已扫码或进入验证流程，刷新会丢失进度。如需重来请点「重新开始」",
        }

    _app["busy"] = True
    try:
        log("手动刷新二维码")
        await fetch_qrcode()
        if _app["state"] == STATE_QR_READY:
            return {"ok": True}
        return {"ok": True}
    finally:
        _app["busy"] = False


async def do_reset():
    """重置到初始状态。

    已登录时禁止打回扫码流程（已登录页面没有登录按钮，会导致死循环），
    直接回到已登录的抖音官网。
    """
    global _app
    log("重置会话")

    crawler = _app.get("crawler")
    if crawler:
        try:
            if await has_login_cookie(crawler):
                log("检测到有效登录态，重置 = 回到抖音官网（不销毁登录）")
                _app["state"] = STATE_SUCCESS
                _app["user_info"] = {"login": True, "restored": True}
                _app["message"] = "已是登录状态，进入抖音官网"
                _app["mfa_lock"] = False
                _app["face_qrcode_b64"] = ""
                _app["verify_options"] = []
                _app["error"] = ""
                try:
                    await crawler.page.goto(
                        "https://www.douyin.com/",
                        wait_until="domcontentloaded", timeout=60000)
                    await asyncio.sleep(3)
                except Exception as e:
                    log(f"跳转官网失败: {e}")
                return {"ok": True}
        except Exception as e:
            log(f"登录态检查失败: {e}")

    # 未登录：正常重置并重新扫码
    _app["state"] = STATE_LOADING
    _app["verify_options"] = []
    _app["error"] = ""
    _app["user_info"] = {}
    _app["debug"] = {}
    _app["last_fetch"] = 0
    _app["mfa_lock"] = False
    _app["qr_net_status"] = ""
    _app["face_qrcode_b64"] = ""
    await fetch_qrcode(force=True)
    return {"ok": True}


async def do_logout():
    """退出登录：清空浏览器 Cookie、删除本地会话文件，回到扫码流程。

    注意：必须删除本地保存的 session_*.json，否则下次重启服务
    会通过 init_session 的恢复逻辑自动把登录态加回来。
    """
    global _app
    log("退出登录")

    crawler = _app.get("crawler")
    if not crawler:
        return {"ok": False, "error": "浏览器未初始化"}

    # 1. 清空浏览器内所有 Cookie（抖音登录态立即失效）
    try:
        await crawler.page.context.clear_cookies()
        log("已清空浏览器 Cookie")
    except Exception as e:
        log(f"清空 Cookie 失败: {e}")
        return {"ok": False, "error": f"清空 Cookie 失败: {e}"}

    # 2. 删除本地保存的会话文件（当前登录会话对应的那个）
    cf = _app.get("cookie_file")
    if cf:
        try:
            Path(cf).unlink(missing_ok=True)
            log(f"已删除本地会话文件: {Path(cf).name}")
        except Exception as e:
            log(f"删除会话文件失败: {e}")

    # 3. 重置面板状态
    _app["state"] = STATE_LOADING
    _app["user_info"] = {}
    _app["cookie_file"] = ""
    _app["message"] = "已退出登录，正在生成新二维码..."
    _app["error"] = ""
    _app["verify_options"] = []
    _app["debug"] = {}
    _app["mfa_lock"] = False
    _app["qr_net_status"] = ""
    _app["face_qrcode_b64"] = ""
    _app["last_fetch"] = 0

    # 4. 回到抖音首页（此时已无登录态），重新走扫码流程
    try:
        await crawler.page.goto(
            "https://www.douyin.com/",
            wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)
    except Exception as e:
        log(f"跳转官网失败: {e}")
    await fetch_qrcode(force=True)
    log("✅ 已退出登录，回到扫码流程")
    return {"ok": True}


# ════════════════════ Web UI ════════════════════

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>抖音登录控制台 - Crawl4AI</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body {
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif;
    background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);
    color:#fff; min-height:100vh;
    display:flex; justify-content:center; align-items:center; padding:20px;
}
.card {
    background:rgba(255,255,255,0.05); backdrop-filter:blur(10px);
    border:1px solid rgba(255,255,255,0.1); border-radius:20px;
    padding:36px; text-align:center; max-width:460px; width:100%;
    box-shadow:0 20px 60px rgba(0,0,0,0.4);
}
h1 { font-size:22px; margin-bottom:6px; color:#fe2c55; }
.subtitle { color:#9ca3af; font-size:13px; margin-bottom:22px; }

/* ── 左右布局：左侧实时画面 + 右侧控制台 ── */
.wrap {
    display:flex; gap:22px; align-items:flex-start;
    justify-content:center; flex-wrap:wrap; width:100%; max-width:1040px;
}
.live-panel {
    background:rgba(255,255,255,0.05); backdrop-filter:blur(10px);
    border:1px solid rgba(255,255,255,0.1); border-radius:20px;
    padding:20px; width:560px; max-width:100%;
    box-shadow:0 20px 60px rgba(0,0,0,0.4);
}
.live-panel h2 {
    font-size:15px; color:#25f4ee; margin-bottom:10px;
    display:flex; justify-content:space-between; align-items:center;
}
.live-badge {
    font-size:10px; background:rgba(239,68,68,.9); color:#fff;
    padding:2px 9px; border-radius:10px; animation:pulse 1.5s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.45} }
.live-img-box { background:#000; border-radius:10px; overflow:hidden; min-height:200px; }
.live-img-box img { display:block; width:100%; }
.live-meta { color:#9ca3af; font-size:11px; margin-top:8px; text-align:right; }

.qr-box {
    background:#fff; border-radius:14px; padding:18px;
    margin:16px auto; display:inline-block; position:relative;
}
.qr-box img { display:block; width:230px; height:230px; }
.placeholder {
    width:230px; height:230px; display:flex;
    align-items:center; justify-content:center;
    color:#666; font-size:13px; flex-direction:column; gap:8px;
}
.spinner {
    width:32px; height:32px; border:3px solid #eee;
    border-top:3px solid #fe2c55; border-radius:50%;
    animation:spin 1s linear infinite;
}
@keyframes spin { 0%{transform:rotate(0)} 100%{transform:rotate(360deg)} }

.status {
    display:inline-block; padding:6px 16px; border-radius:20px;
    font-size:12px; margin:12px 0;
}
.s-loading { background:rgba(245,158,11,.18); color:#fbbf24; }
.s-ready   { background:rgba(37,244,238,.15); color:#25f4ee; }
.s-verify  { background:rgba(254,44,85,.18); color:#fe2c55; }
.s-success { background:rgba(34,197,94,.18); color:#4ade80; }
.s-error   { background:rgba(220,38,38,.18); color:#f87171; }

.debug-panel {
    margin-top:10px; padding:10px 12px; border-radius:9px;
    background:rgba(0,0,0,.28); border:1px solid rgba(255,255,255,.07);
    font-size:11px; color:#888; line-height:1.7; text-align:left;
    display:none; word-break:break-all;
}
.debug-panel.show { display:block; }
.debug-panel .k { color:#25f4ee; }
.debug-panel .txt {
    margin-top:7px; padding-top:7px;
    border-top:1px solid rgba(255,255,255,.08);
    color:#9ca3af; max-height:76px; overflow-y:auto; font-size:10px;
}
.debug-toggle {
    margin-top:9px; font-size:10px; color:#555;
    cursor:pointer; text-decoration:underline;
}
.debug-toggle:hover { color:#888; }

.qr-meta { font-size:11px; color:#666; margin-top:9px; min-height:15px; }
.qr-meta .fresh   { color:#4ade80; }
.qr-meta .stale   { color:#fbbf24; }
.qr-meta .expired { color:#f87171; }
.qr-meta .net     { color:#25f4ee; display:block; margin-top:3px; font-size:10px; }

.scan-hint {
    margin-top:12px; padding:11px 14px; border-radius:10px;
    background:rgba(254,44,85,.12); border:1px solid rgba(254,44,85,.35);
    color:#fe2c55; font-size:12px; line-height:1.6; text-align:left;
    display:none;
}
.scan-hint.show { display:block; }

.verify-section { margin-top:18px; }
.section-title {
    font-size:13px; color:#9ca3af; margin-bottom:12px;
    padding-bottom:8px; border-bottom:1px solid rgba(255,255,255,.08);
}
.verify-btn {
    display:block; width:100%; padding:13px 18px; margin-bottom:9px;
    background:rgba(255,255,255,.07); border:1px solid rgba(255,255,255,.12);
    border-radius:10px; color:#fff; font-size:14px; cursor:pointer;
    transition:all .2s; text-align:left; position:relative;
}
.verify-btn:hover {
    background:rgba(254,44,85,.15); border-color:#fe2c55;
    transform:translateX(3px);
}
.verify-btn:active { transform:translateX(1px); }
.verify-btn .icon { margin-right:9px; font-size:15px; }
.verify-btn .arrow { position:absolute; right:16px; color:#666; }

.input-group { margin-top:16px; display:none; }
.input-group.show { display:block; }
.input-group input {
    width:100%; padding:12px 15px; border-radius:10px;
    border:1px solid rgba(255,255,255,.15); background:rgba(0,0,0,.25);
    color:#fff; font-size:14px; margin-bottom:9px; outline:none;
}
.input-group input:focus { border-color:#fe2c55; }
.input-group input::placeholder { color:#666; }
.submit-btn {
    width:100%; padding:12px; border:none; border-radius:10px;
    background:#fe2c55; color:#fff; font-size:14px;
    cursor:pointer; font-weight:600; transition:.2s;
}
.submit-btn:hover { background:#e61e4d; }
.submit-btn:disabled { background:#555; cursor:not-allowed; }

.actions { margin-top:16px; display:flex; gap:9px; }
.action-btn {
    flex:1; padding:10px; border-radius:9px;
    border:1px solid rgba(255,255,255,.12); background:rgba(255,255,255,.05);
    color:#9ca3af; font-size:12px; cursor:pointer; transition:.2s;
}
.action-btn:hover { background:rgba(255,255,255,.1); color:#fff; }

.info { margin-top:18px; padding-top:16px;
        border-top:1px solid rgba(255,255,255,.08);
        color:#666; font-size:11px; line-height:1.7; }
.info code {
    background:rgba(0,0,0,.3); padding:2px 6px;
    border-radius:4px; color:#25f4ee; font-size:10px;
}
.user-badge {
    margin-top:14px; padding:12px; border-radius:10px;
    background:rgba(34,197,94,.12); border:1px solid rgba(34,197,94,.3);
    font-size:13px; color:#4ade80;
}
.toast {
    position:fixed; top:20px; left:50%; transform:translateX(-50%);
    padding:11px 22px; border-radius:9px; font-size:13px;
    background:rgba(0,0,0,.9); color:#fff; z-index:1000;
    opacity:0; transition:opacity .3s; pointer-events:none;
}
.toast.show { opacity:1; }

/* ── 自动回复面板 ── */
.replier-panel {
    background:rgba(255,255,255,0.05); backdrop-filter:blur(10px);
    border:1px solid rgba(255,255,255,0.1); border-radius:20px;
    padding:22px; width:100%; max-width:1040px;
    box-shadow:0 20px 60px rgba(0,0,0,0.4); text-align:left;
    margin-top:22px;
}
.replier-panel h2 { font-size:15px; color:#fe2c55; margin-bottom:4px; display:flex; justify-content:space-between; align-items:center; }
.replier-sub { color:#9ca3af; font-size:11px; margin-bottom:14px; line-height:1.6; }
.replier-status { display:flex; gap:9px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
.rpill { padding:4px 12px; border-radius:12px; font-size:11px; }
.rpill.on  { background:rgba(34,197,94,.18); color:#4ade80; }
.rpill.off { background:rgba(255,255,255,.08); color:#888; }
.rpill.busy{ background:rgba(245,158,11,.18); color:#fbbf24; }
.rbtn { padding:8px 14px; border-radius:9px; border:1px solid rgba(255,255,255,.15); background:rgba(255,255,255,.06); color:#e5e7eb; font-size:12px; cursor:pointer; transition:.2s; }
.rbtn:hover { border-color:#fe2c55; color:#fff; background:rgba(254,44,85,.12); }
.rbtn.primary { background:#fe2c55; border-color:#fe2c55; color:#fff; font-weight:600; }
.rbtn.primary:hover { background:#e61e4d; }
.rbtn.warn { border-color:rgba(254,44,85,.5); color:#fca5a5; }
.rtable { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
.rtable th { text-align:left; color:#666; font-weight:normal; padding:6px 8px; border-bottom:1px solid rgba(255,255,255,.1); font-size:11px; }
.rtable td { padding:6px 8px; border-bottom:1px solid rgba(255,255,255,.05); }
.rtable input, .rtable select {
    width:100%; padding:7px 9px; border-radius:7px; border:1px solid rgba(255,255,255,.14);
    background:rgba(0,0,0,.25); color:#fff; font-size:12px; outline:none; min-width:70px;
}
.rtable input:focus, .rtable select:focus { border-color:#fe2c55; }
.rmeta { font-size:11px; color:#888; }
.rres { margin-top:10px; font-size:11px; color:#9ca3af; line-height:1.8; max-height:150px; overflow-y:auto;
        background:rgba(0,0,0,.25); border-radius:9px; padding:10px 12px; }
.rres .ok   { color:#4ade80; }
.rres .skip { color:#666; }
.rres .fail { color:#f87171; }
.rres .dry  { color:#fbbf24; }
.rres .replied { color:#4ade80; }
.rlog { margin-top:8px; font-size:11px; color:#888; max-height:120px; overflow-y:auto; line-height:1.7; }
</style>
</head>
<body>
<div class="toast" id="toast"></div>
<div class="wrap">

<!-- 左侧：浏览器实时画面 -->
<div class="live-panel">
    <h2>🖥️ 浏览器实时画面 <span class="live-badge">● LIVE</span></h2>
    <div class="live-img-box">
        <img id="live-panel-img" src="/api/screenshot" alt="实时画面加载中...">
    </div>
    <div class="live-meta" id="live-meta">每 1 秒自动刷新</div>
</div>

<div class="card">
    <h1 id="title">抖音登录控制台</h1>
    <p class="subtitle" id="subtitle">Crawl4AI V3 · 扫码 + 身份验证</p>

    <div class="qr-box" id="qr-box">
        <div class="placeholder">
            <div class="spinner"></div>
            <div>正在启动浏览器...</div>
        </div>
    </div>

    <div id="status" class="status s-loading">初始化中</div>
    <div id="qr-meta" class="qr-meta"></div>
    <div id="scan-hint" class="scan-hint"></div>
    <div id="debug-panel" class="debug-panel"></div>
    <div class="debug-toggle" onclick="toggleDebug()">🔍 页面探针信息</div>

    <!-- 身份验证按钮区 -->
    <div class="verify-section" id="verify-section" style="display:none">
        <div class="section-title">🛡️ 请选择验证方式</div>
        <div id="verify-buttons"></div>
    </div>

    <!-- 输入区 -->
    <div class="input-group" id="input-group">
        <input type="text" id="phone-input" placeholder="请输入手机号" style="display:none">
        <input type="text" id="code-input" placeholder="请输入短信验证码" style="display:none">
        <input type="password" id="pwd-input" placeholder="请输入登录密码" style="display:none">
        <button class="submit-btn" id="submit-btn">确认提交</button>
    </div>

    <div class="actions">
        <button class="action-btn" onclick="refreshQR()">🔄 刷新二维码</button>
        <button class="action-btn" onclick="resetSession()">↺ 重新开始</button>
    </div>

    <div id="user-badge"></div>

    <div class="info">
        ⏰ 二维码有效期约 3-5 分钟，过期自动刷新<br>
        📱 抖音 App → 扫一扫 → 扫描上方二维码<br>
        💡 扫码后若出现身份验证，直接点击上方按钮完成<br>
        <code id="cookie-info"></code>
    </div>
</div>

</div><!-- /wrap -->

<!-- 自动回复系统面板 -->
<div class="replier-panel" id="replier-panel">
    <h2>🤖 私信自动化回复系统 <span id="replier-pill" class="rpill off">加载中</span></h2>
    <div class="replier-sub">
        填写好友名字（需与消息列表显示一致）、回复内容、回复时间窗口与回复周期，保存后启动即可自动回复。<br>
        触发方式：<b>新消息</b> = 对方发来新消息且未回复时才回（防重复）；<b>每周期</b> = 每个回复周期主动发一条。回复时间窗口支持跨零点（如 22:00-06:00）。
    </div>
    <div class="replier-status">
        <button class="rbtn primary" onclick="replierStart()">▶ 启动自动回复</button>
        <button class="rbtn warn" onclick="replierControl('stop')">⏹ 停止</button>
        <button class="rbtn" onclick="replierRunOnce(true)">🧪 立即演练一轮</button>
        <button class="rbtn" onclick="replierRunOnce(false)">⚡ 立即真实执行一轮</button>
        <button class="rbtn primary" onclick="replierSave()">💾 保存配置</button>
        <button class="rbtn" onclick="replierAddRule()">＋ 添加规则</button>
        <span class="rmeta">检查间隔(分) <input id="replier-interval-g" type="number" min="1" value="10"
            style="width:56px;padding:6px 8px;border-radius:7px;border:1px solid rgba(255,255,255,.14);background:rgba(0,0,0,.25);color:#fff;"></span>
        <span class="rmeta" id="replier-meta"></span>
    </div>
    <table class="rtable">
        <thead><tr>
            <th style="width:13%">好友名字</th><th style="width:26%">回复内容</th>
            <th style="width:5%">启用</th><th style="width:11%">触发方式</th>
            <th style="width:14%">回复时间窗口</th><th style="width:11%">回复周期(分)</th>
            <th style="width:9%">每日上限</th><th style="width:6%"></th>
        </tr></thead>
        <tbody id="replier-rules"></tbody>
    </table>
    <div class="rres" id="replier-results">最近一轮执行结果将显示在这里</div>
    <div class="rlog" id="replier-log"></div>
</div>
<script>
let lastState = '';
let lastQrTs = 0;
let pendingAction = '';

function toast(msg, isError) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.background = isError ? 'rgba(220,38,38,.95)' : 'rgba(0,0,0,.9)';
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 2800);
}

function esc(s) {
    return String(s).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
}

function getIcon(text) {
    if (text.includes('短信')) return '📱';
    if (text.includes('刷脸') || text.includes('人脸')) return '👤';
    if (text.includes('密码')) return '🔑';
    return '✓';
}

function getMethod(text) {
    if (text.includes('短信')) return 'sms';
    if (text.includes('刷脸') || text.includes('人脸')) return 'face';
    if (text.includes('密码')) return 'password';
    return 'sms';
}

async function api(path, options) {
    const r = await fetch(path, options);
    return await r.json();
}

// 点击验证方式
// 实体按钮：携带页面真实坐标点击（后端直接 mouse.click，isTrusted=true）
async function clickEntity(x, y, method) {
    toast('🔘 正在点击实体按钮 [' + x + ',' + y + ']...');
    const d = await api('/api/verify', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({method: method, x: x, y: y})
    });
    if (!d.ok) toast(d.error || '点击未生效', true);
    else toast('已点击: ' + (d.selected || method));
    setTimeout(poll, 1000);
}

async function clickVerify(method) {
    if (method === 'sms') {
        // 短信需要先输手机号
        pendingAction = 'sms';
        showInput('phone');
        toast('请输入手机号以接收验证码');
        return;
    }
    if (method === 'password') {
        pendingAction = 'password';
        showInput('pwd');
        toast('请输入登录密码');
        return;
    }
    // 刷脸直接触发
    toast('正在唤起刷脸验证...');
    const d = await api('/api/verify', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({method:'face'})
    });
    if (!d.ok) toast(d.error || '操作失败', true);
    else toast('请在手机上完成人脸识别');
    poll();
}

function showInput(type) {
    const g = document.getElementById('input-group');
    const phone = document.getElementById('phone-input');
    const code = document.getElementById('code-input');
    const pwd = document.getElementById('pwd-input');
    phone.style.display = 'none';
    code.style.display = 'none';
    pwd.style.display = 'none';
    g.classList.add('show');

    if (type === 'phone') { phone.style.display='block'; phone.focus(); }
    if (type === 'code')  { code.style.display='block';  code.focus(); }
    if (type === 'pwd')   { pwd.style.display='block';   pwd.focus(); }
}

// 提交输入
document.getElementById('submit-btn').onclick = async function() {
    const phone = document.getElementById('phone-input');
    const code = document.getElementById('code-input');
    const pwd = document.getElementById('pwd-input');
    const btn = this;
    btn.disabled = true;

    try {
        if (phone.style.display === 'block' && phone.value) {
            toast('正在发送验证码...');
            const d = await api('/api/send-sms', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({phone: phone.value})
            });
            if (d.ok) { showInput('code'); toast('验证码已发送'); }
            else toast(d.error || '发送失败', true);

        } else if (code.style.display === 'block' && code.value) {
            toast('正在验证...');
            const d = await api('/api/submit-code', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({code: code.value})
            });
            if (d.ok) toast('验证成功！');
            else toast(d.error || '验证失败', true);

        } else if (pwd.style.display === 'block' && pwd.value) {
            toast('正在验证密码...');
            const d = await api('/api/submit-password', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({password: pwd.value})
            });
            if (d.ok) toast('验证成功！');
            else toast(d.error || '验证失败', true);
        }
    } finally {
        btn.disabled = false;
        phone.value = ''; code.value = ''; pwd.value = '';
        setTimeout(poll, 800);
    }
};

async function backToOptions() {
    toast('正在返回验证方式选择...');
    document.getElementById('scan-hint').classList.remove('show');
    await api('/api/back-to-options', {method:'POST'});
    setTimeout(poll, 800);
}

async function refreshQR() {
    toast('⏳ 正在刷新二维码...');
    const d = await api('/api/refresh', {method:'POST'});
    if (!d.ok) {
        toast(d.error || '❌ 刷新失败，请稍后重试', true);
    }
    // 成功提示由 poll 检测 qrcode_ts 变化后自动弹出
    setTimeout(poll, 600);
}

async function resetSession() {
    toast('正在重新开始...');
    document.getElementById('verify-section').style.display = 'none';
    document.getElementById('input-group').classList.remove('show');
    await api('/api/reset', {method:'POST'});
    setTimeout(poll, 2000);
}

async function logoutAccount() {
    if (!confirm('确定要退出当前账号吗？\\n\\n退出后将清空浏览器登录态，并删除本地保存的会话 Cookie，下次需要重新扫码登录。')) return;
    toast('⏳ 正在退出登录...');
    try {
        const d = await api('/api/logout', {method:'POST'});
        if (!d.ok) {
            toast(d.error || '❌ 退出失败，请稍后重试', true);
            return;
        }
        toast('✅ 已退出登录，正在生成新二维码');
        document.getElementById('verify-section').style.display = 'none';
        document.getElementById('input-group').classList.remove('show');
        document.getElementById('user-badge').innerHTML = '';
        document.getElementById('cookie-info').textContent = '';
    } catch(e) {
        toast('退出异常: ' + e, true);
    }
    setTimeout(poll, 2500);
}

// 渲染状态
function render(d) {
    const qrBox = document.getElementById('qr-box');
    const status = document.getElementById('status');
    const vs = document.getElementById('verify-section');
    const vb = document.getElementById('verify-buttons');
    const badge = document.getElementById('user-badge');
    const cookieInfo = document.getElementById('cookie-info');
    const ig = document.getElementById('input-group');

    // 二维码
    if (d.state === 'success') {
        qrBox.innerHTML = '<div style="position:relative;display:inline-block">'
            + '<img id="live-img" src="/api/screenshot" '
            + 'style="width:236px;border-radius:10px;display:block" />'
            + '<div style="position:absolute;top:8px;left:8px;background:rgba(34,197,94,.92);'
            + 'color:#fff;font-size:11px;padding:3px 10px;border-radius:12px">● LIVE 抖音官网</div>'
            + '</div>'
            + '<div style="margin-top:9px;display:flex;gap:8px">'
            + '<button class="action-btn" style="flex:1;color:#25f4ee" '
            + 'onclick="scrapeUser()">📊 抓取我的主页</button>'
            + '<a class="action-btn" style="flex:1;text-align:center;text-decoration:none" '
            + 'href="/api/cookies" download>⬇️ 下载 Cookie</a>'
            + '</div>'
            + '<div style="margin-top:8px">'
            + '<button class="action-btn" style="width:100%;color:#f87171;'
            + 'border-color:rgba(248,113,113,.35)" '
            + 'onclick="logoutAccount()">🚪 退出账号</button></div>'
            + '<div id="scrape-result" class="debug-panel" style="margin-top:9px"></div>';
        startLive();
    } else if (d.state === 'face_wait') {
        // 刷脸等待：只展示刷脸验证页二维码；
        // 没截到时绝不能回落显示旧登录二维码（会被误扫）
        if (d.face_qrcode) {
            qrBox.innerHTML = '<div style="position:relative;display:inline-block">'
                + '<img src="data:image/png;base64,' + d.face_qrcode + '" '
                + 'style="width:230px;height:auto;min-height:120px;border-radius:6px;background:#fff" />'
                + '<div style="position:absolute;top:6px;left:6px;background:rgba(168,85,247,.92);'
                + 'color:#fff;font-size:11px;padding:3px 10px;border-radius:12px">👤 刷脸验证码</div>'
                + '</div>';
        } else {
            qrBox.innerHTML = '<div class="placeholder"><div class="spinner"></div>'
                + '<div>正在提取刷脸验证二维码…<br>'
                + '<span style="font-size:11px;opacity:.6">请勿扫描旧登录码</span></div></div>';
        }
    } else if ((d.state === 'qr_ready' || d.state === 'scanned') && d.qrcode_b64) {
        // 登录二维码只在等待扫码/已扫码阶段展示
        qrBox.innerHTML = '<img src="data:image/png;base64,' + d.qrcode_b64 + '" />';
    } else if (d.state === 'verify' || d.state === 'input_code' || d.state === 'input_pwd') {
        qrBox.innerHTML = '<div class="placeholder"><div style="font-size:44px">🔐</div>'
            + '<div>请在下方完成验证操作</div></div>';
    } else if (d.state === 'error') {
        qrBox.innerHTML = '<div class="placeholder"><div style="font-size:44px">❌</div><div>' + esc(d.error || '出错了') + '</div></div>';
    }

    // 状态标签
    const stateMap = {
        'loading': ['初始化中', 's-loading'],
        'qr_ready': ['等待扫码', 's-ready'],
        'scanned': ['已扫码 · 待手机确认', 's-ready'],
        'verify': ['需要身份验证', 's-verify'],
        'input_code': ['等待输入验证码', 's-verify'],
        'input_pwd': ['等待输入密码', 's-verify'],
        'face_wait': ['等待刷脸验证', 's-verify'],
        'success': ['登录成功', 's-success'],
        'error': ['错误', 's-error'],
    };
    const [txt, cls] = stateMap[d.state] || ['未知', 's-loading'];
    status.className = 'status ' + cls;
    status.textContent = txt;

    // 验证按钮：state=verify 时无条件显示（选项空则用默认兜底）
    if (d.state === 'verify') {
        vs.style.display = 'block';
        // 优先渲染从页面抓取的实体按钮（携带真实坐标）
        const vbs = d.verify_buttons || [];
        if (vbs.length) {
            vb.innerHTML = vbs.map(b => {
                return '<button class="verify-btn" onclick="clickEntity(' + b.x + ',' + b.y + ',\\'' + getMethod(b.text) + '\\')">' +
                       '<span class="icon">' + getIcon(b.text) + '</span>' + esc(b.text) +
                       '<span class="arrow" style="font-size:10px;color:#4ade80">◉</span></button>';
            }).join('');
        } else {
            const opts = (d.verify_options && d.verify_options.length)
                ? d.verify_options
                : ['接收短信验证码', '手机刷脸验证', '验证登录密码', '发送短信验证'];
            vb.innerHTML = opts.map(opt => {
                const m = getMethod(opt);
                return '<button class="verify-btn" onclick="clickVerify(\\'' + m + '\\')">' +
                       '<span class="icon">' + getIcon(opt) + '</span>' + esc(opt) +
                       '<span class="arrow">›</span></button>';
            }).join('');
        }
    } else if (d.state === 'success') {
        vs.style.display = 'none';
        ig.classList.remove('show');
    } else if (d.state !== 'input_code' && d.state !== 'input_pwd' && d.state !== 'face_wait') {
        vs.style.display = 'none';
    }

    // 自动根据状态切换输入框
    if (d.state === 'input_code') {
        showInput('code');
    } else if (d.state === 'input_pwd') {
        showInput('pwd');
    } else if (d.state === 'success' || d.state === 'qr_ready') {
        ig.classList.remove('show');
    }

    // 用户信息
    if (d.state === 'success' && d.user_info) {
        const nick = d.user_info.nickname || '抖音用户';
        badge.innerHTML = '<div class="user-badge">👤 ' + esc(nick) + ' · 已登录</div>';
        if (d.cookie_file) {
            cookieInfo.textContent = 'Cookie: ' + d.cookie_file.split('/').pop();
        }
    } else {
        badge.innerHTML = '';
    }

    renderQrMeta(d);
    renderDebug(d.debug);
    renderNetLog(d.net_log);
}

let liveTimer = null;
function startLive() {
    if (liveTimer) return;
    liveTimer = setInterval(() => {
        const img = document.getElementById('live-img');
        if (img) img.src = '/api/screenshot?t=' + Date.now();
    }, 2500);
}

// 左侧实时画面：始终运行，1 秒一刷
setInterval(() => {
    const im = document.getElementById('live-panel-img');
    if (!im) return;
    im.src = '/api/screenshot?t=' + Date.now();
    const meta = document.getElementById('live-meta');
    if (meta) meta.textContent = '刷新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
}, 1000);

async function scrapeUser() {
    const box = document.getElementById('scrape-result');
    if (box) { box.classList.add('show'); box.innerHTML = '<div style="color:#fbbf24">⏳ 正在抓取个人主页（导航+滚动加载，约15秒）...</div>'; }
    toast('📊 正在抓取个人主页数据...');
    try {
        const d = await api('/api/scrape-user');
        if (!d.ok) {
            toast(d.error || '抓取失败', true);
            if (box) box.innerHTML = '<div style="color:#f87171">❌ ' + esc(d.error || '抓取失败') + '</div>';
            return;
        }
        const u = d.user || {};
        const vids = d.videos || [];
        toast('✅ 抓取成功: ' + (u.nickname || '用户') + ' · 作品 ' + vids.length + ' 条');
        let h = '<div style="color:#4ade80;font-weight:600">✅ ' + esc(u.nickname || d.page_title || '抓取成功')
              + '</div><div style="margin-top:5px;line-height:1.8">';
        if (u.douyin_id) h += '抖音号: ' + esc(u.douyin_id) + '<br>';
        if (u.followers != null) h += '粉丝: ' + esc(u.followers) + ' · 关注: ' + esc(u.following)
                                   + ' · 获赞: ' + esc(u.total_likes) + '<br>';
        if (u.signature) h += '简介: ' + esc(u.signature).slice(0, 60) + '<br>';
        h += '</div>';
        if (vids.length) {
            h += '<div style="margin-top:7px;padding-top:7px;border-top:1px solid rgba(255,255,255,.08)">'
               + '最近作品:</div>';
            vids.slice(0, 5).forEach(v => {
                h += '<div class="txt">· ' + esc((v.desc || '无文案').slice(0, 40))
                   + (v.likes != null ? ' ❤' + esc(v.likes) : '') + '</div>';
            });
        }
        h += '<div style="margin-top:6px;color:#25f4ee;font-size:10px">已保存: '
           + esc((d.saved_to || '').split('/').pop()) + ' · 拦截API报文 ' + esc(d.api_captured) + ' 条</div>';
        if (box) box.innerHTML = h;
    } catch(e) {
        toast('抓取异常: ' + e, true);
        if (box) box.innerHTML = '<div style="color:#f87171">❌ ' + esc(String(e)) + '</div>';
    }
}

function renderQrMeta(d) {
    const el = document.getElementById('qr-meta');
    const hint = document.getElementById('scan-hint');

    // 扫码后的关键提示
    if (d.state === 'face_wait') {
        hint.innerHTML = '👤 <b>人脸验证（扫码方式）</b><br>'
                       + '1️⃣ 打开抖音 App → 左上角「≡」→ 扫一扫<br>'
                       + '2️⃣ 扫描上方<b>「刷脸验证码」</b>（非登录码）→ 手机上完成人脸识别<br>'
                       + '<span onclick="backToOptions()" '
                       + 'style="color:#25f4ee;cursor:pointer;text-decoration:underline">'
                       + '无法验证？返回选择其他方式</span>';
        hint.classList.add('show');
    } else if (d.state === 'scanned') {
        hint.innerHTML = '📱 <b>扫码成功！请在手机上点击「确认登录」</b><br>'
                       + '只扫码不会跳转，必须在手机端点确认';
        hint.classList.add('show');
    } else if (d.state === 'qr_ready') {
        hint.innerHTML = '⚠️ <b>扫码前请勿点「刷新二维码」</b><br>'
                       + '每次刷新都会生成新码，扫旧码无效';
        hint.classList.add('show');
    } else {
        hint.classList.remove('show');
    }

    if (d.state === 'success') { el.innerHTML = ''; return; }

    let html = '';
    if (d.qrcode_ts) {
        const age = d.qr_age || 0;
        const left = Math.max(0, 180 - age);
        if (left <= 0) {
            html = '<span class="expired">⚠️ 二维码已过期，请点刷新</span>';
        } else if (left < 40) {
            html = '<span class="expired">⚠️ 剩余 ' + Math.ceil(left) + 's，请尽快扫描</span>';
        } else if (age > 120) {
            html = '<span class="stale">已生成 ' + Math.ceil(age) + 's · 剩余 '
                 + Math.ceil(left) + 's</span>';
        } else {
            html = '<span class="fresh">● 二维码新鲜 · 剩余 ' + Math.ceil(left) + 's</span>';
        }
    }
    if (d.qr_net_status) {
        const nsMap = {
            'pending': '待扫描', 'scanned': '✅ 已扫描',
            'confirmed': '✅ 手机已确认', 'expired': '已过期', 'cancelled': '已取消',
        };
        html += '<span class="net">接口状态: '
              + (nsMap[d.qr_net_status] || d.qr_net_status) + '</span>';
    }
    el.innerHTML = html;
}

let debugOn = false;
function toggleDebug() {
    debugOn = !debugOn;
    document.getElementById('debug-panel').classList.toggle('show', debugOn);
    document.querySelector('.debug-toggle').textContent =
        debugOn ? '🔍 隐藏页面探针信息' : '🔍 页面探针信息';
}

function renderDebug(dbg) {
    const panel = document.getElementById('debug-panel');
    if (!dbg || !Object.keys(dbg).length) {
        panel.innerHTML = '<div style="color:#666">暂无探针数据</div>';
        return;
    }
    const flag = (v) => v
        ? '<span style="color:#4ade80">✓</span>'
        : '<span style="color:#666">✗</span>';
    panel.innerHTML =
        '<div><span class="k">二维码可见</span> ' + flag(dbg.qr_visible) +
        ' &nbsp; <span class="k">扫码页</span> ' + flag(dbg.on_scan_page) +
        ' &nbsp; <span class="k">已扫码</span> ' + flag(dbg.scan_success) +
        ' &nbsp; <span class="k">验证页</span> ' + flag(dbg.in_verify) + '</div>' +
        '<div style="margin-top:4px"><span class="k">验证码框</span> ' + flag(dbg.has_code_input) +
        ' &nbsp; <span class="k">密码框</span> ' + flag(dbg.has_pwd_input) + '</div>' +
        (dbg.panel_text
            ? '<div class="txt">' + esc(dbg.panel_text) + '</div>'
            : '');
}

function renderNetLog(log) {
    const panel = document.getElementById('debug-panel');
    if (!log || !log.length) return;
    let h = '<div style="margin-top:7px;padding-top:7px;'
          + 'border-top:1px solid rgba(255,255,255,.08);color:#666">'
          + '最近接口:</div>';
    log.slice(-3).forEach(e => {
        const st = e.qr_status ? '<span style="color:#4ade80">[' + esc(e.qr_status) + ']</span> ' : '';
        h += '<div class="txt" style="border:none;padding:2px 0;margin:0">'
           + st + esc(e.url.split('/').slice(-2).join('/')) + '</div>';
    });
    panel.innerHTML += h;
}

async function poll() {
    try {
        const d = await api('/api/status');
        // 二维码更新提示（手动刷新或自动刷新成功后触发）
        if (d.qrcode_ts !== lastQrTs && lastQrTs && d.state === 'qr_ready') {
            toast('✅ 二维码已刷新，请重新扫描');
        }
        if (d.state !== lastState || d.qrcode_ts !== lastQrTs) {
            lastState = d.state;
            lastQrTs = d.qrcode_ts;
            render(d);
        }
    } catch(e) {}
}

setInterval(poll, 1800);
poll();

/* ── 自动回复系统 ── */
let replierRules = [];
let replierDirty = false;   // 用户编辑中，暂停从服务器覆盖

function renderReplierRules() {
    const tb = document.getElementById('replier-rules');
    if (!replierRules.length) {
        tb.innerHTML = '<tr><td colspan="9" style="color:#666;padding:14px">暂无规则，点击「＋ 添加规则」</td></tr>';
        return;
    }
    tb.innerHTML = replierRules.map((r, i) => `<tr>
        <td><input value="${esc(r.name || '')}" placeholder="好友名字" onchange="replierEdit(${i},'name',this.value)"></td>
        <td><input value="${esc(r.reply || '')}" placeholder="回复内容" onchange="replierEdit(${i},'reply',this.value)"></td>
        <td style="text-align:center"><input type="checkbox" ${r.active ? 'checked' : ''} onchange="replierEdit(${i},'active',this.checked)"></td>
        <td><select onchange="replierEdit(${i},'trigger',this.value)">
            <option value="new_message" ${r.trigger !== 'always' ? 'selected' : ''}>新消息</option>
            <option value="always" ${r.trigger === 'always' ? 'selected' : ''}>每周期</option>
        </select></td>
        <td><input value="${esc(r.active_hours || '')}" placeholder="09:00-22:00（空=全天）" onchange="replierEdit(${i},'active_hours',this.value)"></td>
        <td><input type="number" min="0" value="${r.min_gap_min ?? 60}" onchange="replierEdit(${i},'min_gap_min',+this.value)"></td>
        <td><input type="number" min="1" value="${r.max_per_day ?? 10}" onchange="replierEdit(${i},'max_per_day',+this.value)"></td>
        <td><button class="rbtn warn" onclick="replierDelRule(${i})">删除</button></td>
    </tr>`).join('');
}

function replierEdit(i, k, v) {
    if (!replierRules[i]) return;
    replierRules[i][k] = v;
    replierDirty = true;
}

function replierAddRule() {
    replierRules.push({ name: '', reply: '', active: true, trigger: 'new_message',
                        active_hours: '09:00-22:00', min_gap_min: 60, max_per_day: 10 });
    replierDirty = true;
    renderReplierRules();
}

function replierDelRule(i) {
    replierRules.splice(i, 1);
    replierDirty = true;
    renderReplierRules();
}

async function apiPost(url, body) {
    return await (await fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'},
                                     body: JSON.stringify(body || {}) })).json();
}

async function replierSave() {
    const cfg = {
        check_interval_min: +document.getElementById('replier-interval-g')?.value || undefined,
        rules: replierRules,
    };
    const r = await apiPost('/api/replier/config', cfg);
    if (r.ok) {
        replierRules = r.config.rules;
        replierDirty = false;
        renderReplierRules();
        toast('✅ 配置已保存');
    } else {
        toast('保存失败: ' + (r.error || ''), true);
    }
    return r.ok;
}

async function replierStart() {
    if (!replierRules.some(r => r.active)) { toast('请先添加并启用规则', true); return; }
    if (!(await replierSave())) return;
    const r = await apiPost('/api/replier/control', { action: 'start' });
    toast(r.ok ? '▶ 自动回复已启动' : '启动失败: ' + (r.error || ''), !r.ok);
}

async function replierControl(action) {
    const r = await apiPost('/api/replier/control', { action });
    toast(r.ok ? '⏹ 已停止' : '操作失败', !r.ok);
}

async function replierRunOnce(dry) {
    if (!(await replierSave())) return;
    const r = await apiPost('/api/replier/control', { action: 'run_once', dry });
    toast(r.ok ? (dry ? '🧪 演练轮已开始（不真实发送）' : '⚡ 真实执行轮已开始') : '失败: ' + (r.error || ''), !r.ok);
}

async function loadReplier() {
    let d;
    try { d = await (await fetch('/api/replier')).json(); } catch (e) { return; }
    if (!replierDirty) {
        replierRules = (d.config && d.config.rules) || [];
        const gi = document.getElementById('replier-interval-g');
        if (gi) gi.value = (d.config && d.config.check_interval_min) || 10;
        renderReplierRules();
    }
    const eng = d.engine || {};
    const pill = document.getElementById('replier-pill');
    if (eng.busy) { pill.textContent = '执行中…'; pill.className = 'rpill busy'; }
    else if (eng.enabled) { pill.textContent = '自动运行中'; pill.className = 'rpill on'; }
    else { pill.textContent = '未启动'; pill.className = 'rpill off'; }
    const t = ts => ts ? new Date(ts * 1000).toLocaleTimeString() : '-';
    const sess = (eng.session_file || '').split('/').pop() || '-';
    document.getElementById('replier-meta').textContent =
        `上次执行 ${t(eng.last_run_ts)} · 下次 ${eng.enabled ? t(eng.next_run_ts) : '-'} · 会话 ${sess}` +
        (eng.error ? ' · ⚠ ' + eng.error : '');
    const res = document.getElementById('replier-results');
    if (eng.last_results && eng.last_results.length) {
        res.innerHTML = eng.last_results.map(r =>
            `<div class="${r.action}">[${r.action}] ${esc(r.name || '')}: ${esc(r.reason || '')}</div>`).join('');
    }
    const lg = document.getElementById('replier-log');
    if (d.log && d.log.length) {
        lg.innerHTML = '<b>📨 发送日志</b><br>' + d.log.slice().reverse().map(e =>
            `${esc(e.time_str || '')} → ${esc(e.to)}: ${esc(e.reply)}`).join('<br>');
    }
}

setInterval(loadReplier, 3000);
loadReplier();
</script>
</body>
</html>"""


# ════════════════════ HTTP 服务器 ════════════════════


class ConsoleHandler(SimpleHTTPRequestHandler):
    """Web 控制台请求处理器"""

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", HTML_PAGE.encode("utf-8"))
        elif path == "/api/status":
            self._api_status()
        elif path == "/api/qrcode":
            self._api_qrcode()
        elif path == "/api/screenshot":
            self._api_screenshot()
        elif path == "/api/cookies":
            self._api_cookies()
        elif path == "/api/scrape-user":
            self._api_scrape_user()
        elif path == "/api/back-to-options":
            self._api_back_to_options()
        elif path == "/api/capture":
            self._api_capture()
        elif path == "/api/replier":
            _replier_api_get(self)
        else:
            self._respond(404, "text/plain", b"Not Found")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if path == "/api/verify":
            self._api_verify(data)
        elif path == "/api/send-sms":
            self._api_send_sms(data)
        elif path == "/api/submit-code":
            self._api_submit_code(data)
        elif path == "/api/submit-password":
            self._api_submit_password(data)
        elif path == "/api/refresh":
            self._api_refresh()
        elif path == "/api/reset":
            self._api_reset()
        elif path == "/api/scrape-user":
            self._api_scrape_user()
        elif path == "/api/capture":
            self._api_capture()
        elif path == "/api/logout":
            self._api_logout()
        elif path == "/api/replier/config":
            _replier_api_config(self, data)
        elif path == "/api/replier/control":
            _replier_api_control(self, data)
        else:
            self._respond(404, "application/json", b'{"ok":false,"error":"not found"}')

    # ── 接口实现 ──

    def _api_status(self):
        data = {
            "state": _app["state"],
            "qrcode_b64": _app["qrcode_b64"],
            "qrcode_ts": _app["qrcode_ts"],
            "message": _app["message"],
            "verify_options": _app["verify_options"],
            "user_info": _app["user_info"],
            "cookie_file": _app["cookie_file"],
            "error": _app["error"],
            "debug": _app["debug"],
            "qr_net_status": _app["qr_net_status"],
            "qr_age": round(time.time() - _app["qrcode_ts"], 1) if _app["qrcode_ts"] else 0,
            "net_log": _app["net_log"][-5:],
            "face_qrcode": _app["face_qrcode_b64"],
            "verify_buttons": _app["verify_buttons"],
        }
        self._respond(200, "application/json", json.dumps(data).encode())

    def _api_qrcode(self):
        if _app["qrcode_b64"]:
            img = base64.b64decode(_app["qrcode_b64"])
            self._respond(200, "image/png", img)
        else:
            self._respond(404, "text/plain", b"No QR code")

    def _api_screenshot(self):
        """返回服务端浏览器当前页面截图（调试利器）"""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _app["crawler"].page.screenshot(type="jpeg", quality=70),
                _main_loop,
            )
            img = fut.result(timeout=15)
            self._respond(200, "image/jpeg", img)
        except Exception as e:
            self._respond(500, "text/plain", f"screenshot failed: {e}".encode())

    def _api_cookies(self):
        """下载当前登录会话的 Cookie JSON"""
        cf = _app.get("cookie_file")
        if cf and Path(cf).exists():
            try:
                data = Path(cf).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{Path(cf).name}"')
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._respond(500, "text/plain", str(e).encode())
        else:
            self._respond(404, "text/plain", b"No cookies saved")

    def _api_back_to_options(self):
        """点击验证子页面底部的「选择其他验证方式」，返回验证选项列表"""
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._do_back_to_options(), _main_loop)
            result = fut.result(timeout=20)
            self._respond(200, "application/json", json.dumps(result).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    @staticmethod
    async def _do_back_to_options():
        global _app
        crawler = _app.get("crawler")
        if not crawler:
            return {"ok": False, "error": "浏览器未初始化"}
        log("返回验证方式选择页")
        try:
            await crawler.page.mouse.click(*BACK_TO_OPTIONS_COORD)
        except Exception as e:
            log(f"点击返回链接失败: {e}")
        await asyncio.sleep(2)
        _app["state"] = STATE_VERIFY_NEEDED
        _app["face_qrcode_b64"] = ""
        if not _app["verify_options"]:
            _app["verify_options"] = ["接收短信验证码", "手机刷脸验证",
                                      "验证登录密码", "发送短信验证"]
        _app["message"] = "请选择验证方式"
        return {"ok": True}

    def _api_capture(self):
        """导出验证流程全量抓包（REQ/RESP 时间线）并落盘"""
        try:
            cap = list(_app.get("verify_capture", []))
            out = Path(sys_path) / "output" / f"verify_capture_{int(time.time())}.json"
            out.write_text(json.dumps(cap, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            # 摘要（不含大 body）
            summary = [{
                "dir": e.get("dir"), "method": e.get("method", ""),
                "url": e.get("url", "")[:120],
                "status": e.get("status", ""),
                "post_len": len(e.get("post_data") or ""),
                "body_len": len(e.get("body") or ""),
                "time": time.strftime("%H:%M:%S", time.localtime(e.get("ts", 0))),
            } for e in cap]
            self._respond(200, "application/json", json.dumps({
                "ok": True, "count": len(cap),
                "saved_to": str(out), "timeline": summary,
            }, ensure_ascii=False).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_scrape_user(self):
        """抓取 /user/self 个人主页数据（需已登录）"""
        if _app["busy"]:
            self._respond(429, "application/json",
                          json.dumps({"ok": False, "error": "正在处理中，请稍候"}).encode())
            return
        if _app["state"] != STATE_SUCCESS:
            self._respond(400, "application/json",
                          json.dumps({"ok": False,
                                      "error": "请先完成登录（登录后才能访问个人主页）"}).encode())
            return
        if not _app.get("crawler"):
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": "浏览器未初始化"}).encode())
            return

        _app["busy"] = True
        try:
            fut = asyncio.run_coroutine_threadsafe(
                scrape_self_profile(_app["crawler"]), _main_loop)
            result = fut.result(timeout=120)
            self._respond(200, "application/json",
                          json.dumps(result, ensure_ascii=False).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())
        finally:
            _app["busy"] = False

    def _api_verify(self, data):
        method = data.get("method", "")
        extra = {k: v for k, v in data.items() if k != "method"}
        fut = asyncio.run_coroutine_threadsafe(
            do_verify_method(method, extra), _main_loop
        )
        try:
            result = fut.result(timeout=30)
            self._respond(200, "application/json", json.dumps(result).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_send_sms(self, data):
        phone = data.get("phone", "")
        fut = asyncio.run_coroutine_threadsafe(do_send_sms(phone), _main_loop)
        try:
            result = fut.result(timeout=30)
            self._respond(200, "application/json", json.dumps(result).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_submit_code(self, data):
        code = data.get("code", "")
        fut = asyncio.run_coroutine_threadsafe(do_submit_code(code), _main_loop)
        try:
            result = fut.result(timeout=30)
            self._respond(200, "application/json", json.dumps(result).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_submit_password(self, data):
        pwd = data.get("password", "")
        fut = asyncio.run_coroutine_threadsafe(do_submit_password(pwd), _main_loop)
        try:
            result = fut.result(timeout=30)
            self._respond(200, "application/json", json.dumps(result).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_refresh(self):
        try:
            fut = asyncio.run_coroutine_threadsafe(do_refresh(), _main_loop)
            result = fut.result(timeout=60)
            self._respond(200, "application/json", json.dumps(result).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_reset(self):
        try:
            fut = asyncio.run_coroutine_threadsafe(do_reset(), _main_loop)
            fut.result(timeout=60)
            self._respond(200, "application/json", b'{"ok":true}')
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _api_logout(self):
        """退出账号：清 Cookie + 删本地会话 + 重新生成二维码"""
        if _app["busy"]:
            self._respond(429, "application/json",
                          json.dumps({"ok": False, "error": "正在处理中，请稍候"}).encode())
            return
        _app["busy"] = True
        try:
            fut = asyncio.run_coroutine_threadsafe(do_logout(), _main_loop)
            result = fut.result(timeout=90)
            self._respond(200, "application/json",
                          json.dumps(result, ensure_ascii=False).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())
        finally:
            _app["busy"] = False

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # 静默 HTTP 日志


# ════════════════════ 启动入口 ════════════════════


def run_server(host="0.0.0.0", port=8765):
    """启动 Web 控制台"""
    global _main_loop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _main_loop = loop

    print("=" * 58)
    print("  🎮 抖音登录 Web 控制台 - Crawl4AI V3")
    print("=" * 58)

    # 初始化会话（启动浏览器 + 获取二维码）
    print("\n[*] 正在启动浏览器并获取二维码...")
    loop.run_until_complete(init_session())

    # 启动后台轮询任务
    loop.create_task(poll_login_status())

    # HTTP 服务器（守护线程）
    # 多线程：单个慢请求（如 refresh 最长等 90s）不能堵死 /api/status，
    # 否则控制台会整体"假死"（端口在监听但任何接口都不响应）
    class ReusableHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        allow_reuse_port = True
        daemon_threads = True

    try:
        server = ReusableHTTPServer((host, port), ConsoleHandler)
    except OSError as e:
        print(f"\n[!] 端口 {port} 被占用，尝试 {port+1}...")
        port += 1
        server = ReusableHTTPServer((host, port), ConsoleHandler)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print(f"\n{'='*58}")
    print(f"  🌐 控制台地址: http://localhost:{port}")
    print(f"{'='*58}")
    print(f"\n  📋 使用流程:")
    print(f"     1. 浏览器打开上面的地址")
    print(f"     2. 用抖音 App 扫描二维码")
    print(f"     3. 出现身份验证后，点击页面上的验证按钮")
    print(f"     4. 按提示输入手机号/验证码/密码")
    print(f"\n  🔌 API 接口:")
    print(f"     GET  /api/status          查询状态")
    print(f"     GET  /api/qrcode          二维码图片")
    print(f"     POST /api/verify          选择验证方式")
    print(f"     POST /api/send-sms        发送验证码")
    print(f"     POST /api/submit-code     提交验证码")
    print(f"     POST /api/submit-password 提交密码")
    print(f"     POST /api/refresh         刷新二维码")
    print(f"     POST /api/reset           重新开始")
    print(f"\n  按 Ctrl+C 停止\n")

    try:
        while True:
            try:
                loop.run_forever()
                break  # 正常退出（stop）
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # 任何未捕获异常不让进程静默死亡：打印并继续跑
                print(f"\n[!] 主循环异常（已捕获，服务继续）: {e}")
                import traceback
                traceback.print_exc()
                if loop.is_closed():
                    break
    except KeyboardInterrupt:
        print("\n[*] 正在停止...")
    finally:
        # 关闭浏览器
        try:
            if _app.get("crawler"):
                loop.run_until_complete(_app["crawler"].close())
        except Exception:
            pass
        try:
            server.shutdown()
            server.server_close()
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass
        print("[*] 已停止")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="抖音登录 Web 控制台")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", "-p", type=int, default=8765)
    a = p.parse_args()
    run_server(host=a.host, port=a.port)
