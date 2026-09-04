#!/usr/bin/env python3
"""
抖音网页登录爬虫 - Crawl4AI V3 最终版

架构设计：
┌─────────────────────────────────────────────┐
│              Crawl4AI V3                     │
│                                              │
│  ┌──────────────┐   ┌───────────────────┐   │
│  │ Anti-Detect  │   │  Patchright       │   │
│  │ Config       │──▶│  (Playwright fork) │   │
│  └──────────────┘   │  Browser Control  │   │
│                     └───────────────────┘   │
│                                              │
│  支持功能:                                   │
│  ✅ 二维码扫码登录                            │
│  ✅ 短信验证码登录                            │
│  ✅ 密码登录                                 │
│  ✅ 身份验证处理（截图中的界面）               │
│  ✅ Cookie 持久化                             │
│  ✅ 登录后数据抓取                            │
└─────────────────────────────────────────────┘

用法:
    # 二维码登录（推荐）
    python douyin_crawl4ai_v3.py qr [--visible]

    # 短信验证码登录
    python douyin_crawl4ai_v3.py sms --phone 13800138000

    # 密码登录
    python douyin_crawl4ai_v3.py password --phone 13800138000 --password xxx

    # 处理身份验证（对应你的截图）
    python douyin_crawl4ai_v3.py verify --method sms --phone 13800138000

    # 登录后抓取数据
    python douyin_crawl4ai_v3.py scrape --cookies output/session_xxx.json
"""
import asyncio
import base64
import json
import os
import sys
import time
import argparse
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Awaitable


# ──────────────────── 数据模型 ────────────────────


@dataclass
class LoginResult:
    """登录结果"""
    success: bool = False
    method: str = ""
    session_id: str = ""
    cookies: list = field(default_factory=list)
    user_info: dict = field(default_factory=dict)
    qrcode_image: bytes = b""
    qrcode_base64: str = ""
    error: str = ""
    screenshot_bytes: bytes = b""
    timestamp: float = 0.0
    cookie_file: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["qrcode_image"] = f"<{len(self.qrcode_image)} bytes>" if self.qrcode_image else ""
        d["screenshot_bytes"] = f"<{len(self.screenshot_bytes)} bytes>" if self.screenshot_bytes else ""
        return d


@dataclass
class ScrapedData:
    """抓取的数据"""
    url: str = ""
    title: str = ""
    markdown: str = ""
    html: str = ""
    videos: list = field(default_factory=list)
    images: list = field(default_factory=list)
    links: dict = field(default_factory=dict)
    timestamp: float = 0.0


# ──────────────────── 反检测配置 ────────────────────

STEALTH_JS = """
// 反 webdriver 检测
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

// Chrome 运行时伪装
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };

// Permissions API 伪装
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
    p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : _origQuery(p);

// WebGL 供应商伪装
try {
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) {
        const ext = gl.getExtension('WEBGL_debug_renderer_info');
        if (ext) {
            // 保持默认值，不做额外修改
        }
    }
} catch(e) {}
"""

DOUYIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ──────────────────── 核心爬虫类 V3 ────────────────────


class DouyinCrawlerV3:
    """
    抖音登录爬虫 V3 - 基于 Patchright (Crawl4AI 内核)

    使用 Crawl4AI 的反检测能力，同时保留完整的浏览器控制能力。
    """

    DOUYIN_URL = "https://www.douyin.com/jingxuan"

    def __init__(
        self,
        output_dir: str = "./output",
        headless: bool = True,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.session_id = f"douyin_v3_{int(time.time())}"
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def start_browser(self):
        """启动带反检测的浏览器（使用 Patchright）"""
        from patchright.async_api import async_playwright

        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )

        self._playwright = await async_playwright().start()

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
        ]
        if proxy:
            launch_args.append(f"--proxy-server={proxy}")

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=DOUYIN_UA,
            locale="zh-CN",
            ignore_https_errors=True,
        )

        # 注入反检测脚本
        await self._context.add_init_script(STEALTH_JS)

        self._page = await self._context.new_page()
        print(f"[OK] 浏览器已启动 (headless={self.headless})")

    async def close(self):
        """关闭浏览器"""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    async def __aenter__(self):
        await self.start_browser()
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def page(self):
        if not self._page:
            raise RuntimeError("浏览器未初始化，请先调用 start_browser() 或使用 async with")
        return self._page

    # ────── 基础操作 ──────

    async def open_douyin(self, url: str = None) -> bool:
        """打开抖音页面"""
        target = url or self.DOUYIN_URL
        try:
            await self.page.goto(target, wait_until="domcontentloaded", timeout=60000)
            print(f"[OK] 页面已打开: {target}")
            await asyncio.sleep(1.2)  # 等待 JS 渲染（调用方会轮询就绪，不宜过长）
            return True
        except Exception as e:
            print(f"[WARN] 页面加载异常: {e}")
            await asyncio.sleep(1.2)
            return True

    async def click_login_button(self) -> bool:
        """点击登录按钮"""
        selectors = [
            'button:has-text("登录")',
            '.semi-button-primary:has-text("登录")',
            '[class*="login-btn"]',
            'button[class*="login"]',
        ]

        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=5000)
                    print(f"[OK] 已点击登录按钮 ({sel})")
                    return True
            except Exception:
                continue

        # Fallback: JS click
        clicked = await self.page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                const t = b.textContent || '';
                if (t.includes('登录') || t.toLowerCase().includes('login')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            print("[OK] 已通过 JS 点击登录按钮")
            return True

        print("[WARN] 未找到登录按钮，尝试截图调试...")
        debug_path = self.output_dir / "debug_no_login_btn.png"
        await self.page.screenshot(path=str(debug_path))
        return False

    async def wait_for_panel(self, timeout: float = 15.0) -> dict:
        """等待登录面板出现"""
        start = time.time()
        while time.time() - start < timeout:
            info = await self.page.evaluate("""() => {
                const panel = document.querySelector('#login-panel-new')
                             || document.querySelector('[class*="login-panel"]');
                if (!panel) return { found: false };
                const rect = panel.getBoundingClientRect();
                return {
                    found: true,
                    visible: rect.width > 0 && rect.height > 0,
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    hasQR: !!panel.querySelector('#douyin_login_comp_scan_code'),
                    hasMobile: !!panel.querySelector('#douyin_login_comp_mobile_code'),
                };
            }""")
            if info.get("found") and info.get("visible"):
                print(f"[OK] 登录面板已出现 ({info['width']}x{info['height']})")
                return info
            await asyncio.sleep(0.8)

        print("[WARN] 等待登录面板超时")
        return {"found": False}

    async def extract_qrcode(self) -> tuple[bool, bytes, str]:
        """提取二维码图片 Returns: (success, image_bytes, error)"""
        try:
            info = await self.page.evaluate("""() => {
                const scanCode = document.querySelector('#douyin_login_comp_scan_code');
                if (!scanCode) return { found: false, reason: 'no_scan_code' };
                const img = scanCode.querySelector('img');
                if (!img) return { found: false, reason: 'no_img' };
                return {
                    found: true,
                    src: img.src,
                    w: img.naturalWidth,
                    h: img.naturalHeight,
                };
            }""")
            if not info.get("found"):
                return False, b"", info.get("reason", "unknown")

            src = info["src"]
            if src.startswith("data:image/"):
                _, b64 = src.split(",", 1)
                img_bytes = base64.b64decode(b64)
                print(f"[OK] 二维码提取成功 ({info['w']}x{info['h']})")
                return True, img_bytes, ""
            if src.startswith("http"):
                # v4 兜底：二维码是远程 URL 时直接在页面内抓字节。
                # v3 直接报"非 data URL"失败，会触发调用方昂贵的整页重取流程。
                data = await self.page.evaluate("""async (url) => {
                    try {
                        const r = await fetch(url, {credentials: 'include'});
                        if (!r.ok) return '';
                        const buf = await r.arrayBuffer();
                        let bin = '';
                        const arr = new Uint8Array(buf);
                        for (let i = 0; i < arr.length; i += 0x8000) {
                            bin += String.fromCharCode.apply(
                                null, arr.subarray(i, i + 0x8000));
                        }
                        return btoa(bin);
                    } catch (e) { return ''; }
                }""", src)
                if data:
                    try:
                        img_bytes = base64.b64decode(data)
                        print(f"[OK] 二维码提取成功-网络抓取 ({info['w']}x{info['h']})")
                        return True, img_bytes, ""
                    except Exception:
                        pass
                return False, b"", f"网络图片抓取失败: {src[:60]}"
            return False, b"", f"非 data URL: {src[:60]}"
        except Exception as e:
            return False, b"", str(e)

    async def screenshot_element(self, selector: str) -> bytes:
        """截取指定元素"""
        try:
            el = self.page.locator(selector).first
            if await el.count() > 0:
                return await el.screenshot()
        except Exception:
            pass
        return b""

    async def check_login_status(self) -> dict:
        """检查登录状态"""
        return await self.page.evaluate("""() => {
            const panel = document.querySelector('#login-panel-new');
            const panelVis = panel && panel.offsetParent !== null;
            const verifyPage = !!document.querySelector('[class*="identity-verify"]')
                           || (document.body.innerText || '').includes('身份验证')
                           || !!document.querySelector('.verify-title');

            let nickname = '';
            const nickEl = document.querySelector('[class*="nickname"], [class*="Nickname"]');
            if (nickEl) nickname = nickEl.textContent.trim();

            return {
                logged_in: !panelVis && !verifyPage,
                panel_visible: panelVis,
                verify_page: verifyPage,
                nickname: nickname,
                url: location.href,
            };
        }""")

    # ────── 输入操作 ──────

    async def input_text(self, selector_hint: str, text: str) -> bool:
        """通用输入方法"""
        return await self.page.evaluate("""({hint, text}) => {
            const inputs = document.querySelectorAll('input');
            let target = null;

            // 按 placeholder/type 匹配
            for (const inp of inputs) {
                const ph = (inp.placeholder || '').toLowerCase();
                const type = (inp.type || '').toLowerCase();
                if (ph.includes(hint.toLowerCase()) || type === hint.toLowerCase()) {
                    target = inp;
                    break;
                }
            }

            // fallback: 第一个可见输入框
            if (!target) {
                for (const inp of inputs) {
                    if (inp.offsetParent !== null && inp.type !== 'hidden') {
                        target = inp;
                        break;
                    }
                }
            }

            if (!target) return { success: false, error: 'no_input_found' };

            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(target, text);
            target.dispatchEvent(new Event('input', { bubbles: true }));
            target.dispatchEvent(new Event('change', { bubbles: true }));
            return { success: true, value: target.value };
        }""", {"hint": selector_hint, "text": text})

    async def input_phone(self, phone: str) -> bool:
        return await self.input_text("手机号", phone)

    async def input_password(self, password: str) -> bool:
        result = await self.page.evaluate("""(pwd) => {
            const input = document.querySelector('input[type="password"]');
            if (!input) return false;
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, pwd);
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }""", password)
        return result

    async def input_sms_code(self, code: str) -> bool:
        return await self.input_text("验证码", code)

    async def click_send_sms_button(self) -> bool:
        """点击发送验证码"""
        result = await self.page.evaluate("""() => {
            const els = document.querySelectorAll('button, [role="button"], div[class*="send"], span[class*="send"]');
            for (const el of els) {
                const t = (el.textContent || '');
                if ((t.includes('发送') || t.includes('获取')) && t.includes('验证')) {
                    el.click();
                    return { ok: true, text: t.trim() };
                }
            }
            return { ok: false };
        }""")
        if result and result.get("ok"):
            print(f"[OK] 已点击: {result.get('text')}")
            return True
        return False

    async def click_submit_button(self) -> bool:
        """点击提交/登录按钮"""
        selectors = [
            'button[type="submit"]',
            'button:has-text("登录")',
            'button:has-text("提交")',
            '[class*="submit-btn"]',
        ]
        for sel in selectors:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(timeout=3000)
                    print(f"[OK] 已点击提交 ({sel})")
                    return True
            except Exception:
                continue
        return False

    # ────── Cookie 管理 ──────

    async def save_cookies(self) -> str:
        """保存 Cookies"""
        try:
            cookies = await self._context.cookies()
            filepath = str(self.output_dir / f"session_{self.session_id}.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "session_id": self.session_id,
                    "timestamp": time.time(),
                    "url": self.page.url,
                    "cookies": cookies,
                }, f, ensure_ascii=False, indent=2)
            print(f"[OK] Cookies 已保存: {filepath}")
            return filepath
        except Exception as e:
            print(f"[ERROR] 保存 Cookies 失败: {e}")
            return ""

    async def load_cookies(self, filepath: str) -> bool:
        """加载 Cookies"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookies = data.get("cookies", [])
            if cookies:
                await self._context.add_cookies(cookies)
                print(f"[OK] 已加载 {len(cookies)} 条 Cookie")
                return True
            return False
        except Exception as e:
            print(f"[ERROR] 加载 Cookies 失败: {e}")
            return False

    # ════════════════ 登录流程实现 ════════════════

    async def login_qr(
        self,
        on_qrcode: Optional[Callable[[bytes], Awaitable[None]]] = None,
        poll_interval: float = 3.0,
        timeout: float = 180.0,
    ) -> LoginResult:
        """
        🔲 二维码扫码登录

        流程: 打开页面 → 点击登录 → 等待面板 → 提取二维码 → 等待扫码 → 完成
        """
        result = LoginResult(method="qr", timestamp=time.time())

        try:
            # Step 1: 打开页面
            print("\n" + "─" * 55)
            print(" [1/6] 正在打开抖音精选页...")
            print("─" * 55)
            await self.open_douyin()

            # Step 2: 点击登录
            print("\n[2/6] 点击登录按钮...")
            if not await self.click_login_button():
                result.error = "无法点击登录按钮"
                return result
            await asyncio.sleep(3)

            # Step 3: 等待面板
            print("\n[3/6] 等待登录面板...")
            panel_info = await self.wait_for_panel(timeout=15)
            if not panel_info.get("found"):
                result.error = "登录面板未出现"
                # 调试截图
                ds = self.output_dir / "debug_no_panel.png"
                await self.page.screenshot(path=str(ds))
                print(f"[DEBUG] 截图: {ds}")
                return result

            # 确保在二维码 tab
            if not panel_info.get("hasQR"):
                print("[INFO] 切换到二维码 tab...")
                await self.page.evaluate("""() => {
                    const tabs = document.querySelectorAll('[class*="tab"], li, [role="tab"]');
                    for (const t of tabs) {
                        if ((t.textContent||'').includes('扫码')) { t.click(); return; }
                    }
                }""")
                await asyncio.sleep(2)

            # Step 4: 提取二维码
            print("\n[4/6] 📸 提取二维码...")
            success, img_bytes, err = await self.extract_qrcode()

            if not success:
                result.error = f"二维码提取失败: {err}"
                ps = await self.screenshot_element("#login-panel-new")
                if ps:
                    pp = self.output_dir / "debug_panel.png"
                    pp.write_bytes(ps)
                    print(f"[DEBUG] 面板截图: {pp}")
                return result

            result.qrcode_image = img_bytes
            result.qrcode_base64 = base64.b64encode(img_bytes).decode()

            # 保存二维码文件
            qr_path = self.output_dir / "qrcode_v3.png"
            qr_path.write_bytes(img_bytes)
            print(f"[OK] 二维码已保存: {qr_path}")

            # 面板截图
            panel_ss = await self.screenshot_element("#login-panel-new")
            if panel_ss:
                pp = self.output_dir / "panel_v3.png"
                pp.write_bytes(panel_ss)
                result.screenshot_bytes = panel_ss

            # 回调
            if on_qrcode:
                await on_qrcode(img_bytes)

            # Step 5: 等待扫码
            print(f"\n{'='*55}")
            print(f" [5/6] ⏳  等待扫码（超时 {timeout:.0f} 秒）")
            print(f"{'='*55}")
            print(f" 📱 请用抖音 App 扫描上方二维码\n")

            start = time.time()
            while time.time() - start < timeout:
                status = await self.check_login_status()

                if status.get("logged_in"):
                    result.success = True
                    result.user_info = status
                    cf = await self.save_cookies()
                    result.cookie_file = cf
                    print(f"\n{'='*55}")
                    print(f"  ✅ 扫码登录成功!")
                    print(f"  👤 用户: {status.get('nickname', 'N/A')}")
                    print(f"  🆔 Session: {self.session_id}")
                    print(f"  🍪 Cookie: {cf}")
                    print(f"{'='*55}\n")
                    return result

                if status.get("verify_page"):
                    print("\n⚠️  检测到身份验证页面")
                    print("   如需处理，请运行: ... verify --method sms --phone XXX")

                # 检查二维码是否过期
                qr_ok, _, _ = await self.extract_qrcode()
                if not qr_ok:
                    print("\n🔄 二维码已过期，自动刷新...")
                    await self.open_douyin()
                    await self.click_login_button()
                    await self.wait_for_panel()
                    s2, ib2, _ = await self.extract_qrcode()
                    if s2:
                        result.qrcode_image = ib2
                        result.qrcode_base64 = base64.b64encode(ib2).decode()
                        qr_path.write_bytes(ib2)
                        if on_qrcode:
                            await on_qrcode(ib2)
                        print("[OK] 二维码已刷新")

                # 进度提示
                elapsed = int(time.time() - start)
                remain = int(timeout - elapsed)
                if remain > 0 and remain % 15 == 0:
                    print(f"   ⏱️  剩余 {remain} 秒...")

                await asyncio.sleep(poll_interval)

            result.error = "超时：未在规定时间内完成扫码"

        except Exception as e:
            result.error = f"异常: {str(e)}"
            import traceback
            traceback.print_exc()

        return result

    async def login_sms(self, phone: str) -> LoginResult:
        """
        📱 短信验证码登录

        流程: 打开页面 → 登录 → 面板 → 切换短信tab → 输入手机号 → 发送验证码 → 输入验证码 → 提交
        """
        result = LoginResult(method="sms", timestamp=time.time())

        try:
            print("\n" + "─" * 55)
            print(" [1/6] 打开抖音页面...")
            print("─" * 55)
            await self.open_douyin()

            print("\n[2/6] 点击登录...")
            await self.click_login_button()
            await asyncio.sleep(3)

            print("\n[3/6] 等待面板...")
            if not await self.wait_for_panel():
                result.error = "登录面板未出现"
                return result

            # 切换到短信登录
            print("\n[INFO] 切换到短信验证码模式...")
            await self.page.evaluate("""() => {
                const tabs = document.querySelectorAll('li, [class*="tab"], [role="tab"], div[class*="tab-item"]');
                for (const t of tabs) {
                    const txt = t.textContent || '';
                    if (txt.includes('短信') || txt.includes('手机号')) { t.click(); return; }
                }
            }""")
            await asyncio.sleep(2)

            # 输入手机号
            print(f"\n[4/6] 输入手机号: {phone[:3]}****{phone[-4:]}")
            if not await self.input_phone(phone):
                result.error = "输入手机号失败"
                return result
            await asyncio.sleep(1)

            # 发送验证码
            print("\n[5/6] 发送短信验证码...")
            if not await self.click_send_sms_button():
                result.error = "发送验证码失败"
                return result

            print(f"\n{'='*55}")
            print(f" ✅ 验证码已发送至 {phone[:3]}****{phone[-4:]}")
            print(f"{'='*55}")

            # 等待用户输入
            code = input("\n  请输入收到的验证码: ").strip()
            if not code:
                result.error = "未输入验证码"
                return result

            # 输入并提交
            print("\n[6/6] 提交验证码...")
            await self.input_sms_code(code)
            await asyncio.sleep(1)
            await self.click_submit_button()
            await asyncio.sleep(5)

            status = await self.check_login_status()
            if status.get("logged_in"):
                result.success = True
                result.user_info = status
                result.cookie_file = await self.save_cookies()
                print(f"\n✅ 短信验证码登录成功! 用户: {status.get('nickname', 'N/A')}")
            elif status.get("verify_page"):
                result.error = "触发身份验证，请手动完成或使用 verify 命令"
            else:
                result.error = "登录状态未知，请检查验证码"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def login_password(self, phone: str, password: str) -> LoginResult:
        """
        🔑 密码登录

        注意: 密码登录可能触发身份验证（如截图所示）
        """
        result = LoginResult(method="password", timestamp=time.time())

        try:
            print("\n" + "─" * 55)
            print(" [1/5] 打开抖音页面...")
            print("─" * 55)
            await self.open_douyin()

            print("\n[2/5] 点击登录...")
            await self.click_login_button()
            await asyncio.sleep(3)

            print("\n[3/5] 等待面板...")
            if not await self.wait_for_panel():
                result.error = "登录面板未出现"
                return result

            # 切换到密码登录
            print("\n[INFO] 切换到密码登录模式...")
            await self.page.evaluate("""() => {
                const tabs = document.querySelectorAll('li, [class*="tab"], [role="tab"]');
                for (const t of tabs) {
                    if ((t.textContent||'').includes('密码')) { t.click(); return; }
                }
            }""")
            await asyncio.sleep(2)

            # 输入账号密码
            print(f"\n[4/5] 输入账号信息...")
            await self.input_phone(phone)
            await asyncio.sleep(1)

            print("[5/5] 输入密码并提交...")
            if not await self.input_password(password):
                result.error = "输入密码失败"
                return result

            await asyncio.sleep(1)
            await self.click_submit_button()
            await asyncio.sleep(5)

            status = await self.check_login_status()
            if status.get("logged_in"):
                result.success = True
                result.user_info = status
                result.cookie_file = await self.save_cookies()
                print(f"\n✅ 密码登录成功!")
            elif status.get("verify_page"):
                result.error = "触发身份验证！建议使用 QR 或 SMS 方式"
                print(f"\n{'='*55}")
                print(f" ⚠️  触发了身份验证（就是你截图中显示的界面）")
                print(f"{'='*55}")
                print(f" 可选方案:")
                print(f"   1. 使用二维码登录: python ... qr")
                print(f"   2. 完成身份验证: python ... verify --method sms --phone {phone}")
                print(f"{'='*55}")
            else:
                result.error = "登录状态未知"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def handle_identity_verify(
        self,
        method: str = "sms",
        phone: str = "",
        password: str = "",
    ) -> LoginResult:
        """
        🛡️  处理身份验证页面

        这是你截图中显示的界面！包含以下选项：
        ├─ 接收短信验证码
        ├─ 手机刷脸验证
        ├─ 验证登录密码
        └─ 发送短信验证

        Args:
            method: "sms" | "password" | "face"
            phone: 手机号（sms 方式需要）
            password: 密码（password 方式需要）
        """
        result = LoginResult(method=f"verify_{method}", timestamp=time.time())

        try:
            # 检查当前状态
            status = await self.check_login_status()
            if not status.get("verify_page"):
                print("[INFO] 当前不在身份验证页面，尝试打开...")
                await self.open_douyin()
                await self.click_login_button()
                await asyncio.sleep(3)
                status = await self.check_login_status()

            if not status.get("verify_page"):
                # 尝试执行一次密码登录来触发验证
                if phone and password:
                    print("[INFO] 尝试登录以触发验证流程...")
                    await self.input_phone(phone)
                    await self.input_password(password)
                    await self.click_submit_button()
                    await asyncio.sleep(5)
                    status = await self.check_login_status()

            if not status.get("verify_page"):
                result.error = "无法进入身份验证页面，请先执行登录操作"
                print("[ERROR] 不在身份验证页面")
                print("[HINT] 先运行: python ... password --phone XXX --password XXX")
                return result

            print(f"\n{'╔'+ '═'*53 +'╗'}")
            print(f"║ {'🛡️  身份验证页面':^51} ║")
            print(f"{'╠'+ '═'*53 +'╣'}")

            # 显示可用选项
            options = await self.page.evaluate("""() => {
                const opts = [];
                const items = document.querySelectorAll('li, div[class*="option"], div[class*="item"], [class*="menu-item"]');
                for (const item of items) {
                    const t = item.textContent.trim().replace(/\\s+/g, ' ');
                    if (t.length < 50 && (t.includes('短信') || t.includes('密码') || t.includes('刷脸'))) {
                        opts.push(t);
                    }
                }
                return opts;
            }""")

            if options:
                print(f"║ {'可用验证方式:':^51} ║")
                for i, opt in enumerate(options, 1):
                    print(f"║   {i}. {opt:<47} ║")

            print(f"{'╚'+ '═'*53 +'╝'}\n")

            # 选择验证方式
            method_labels = {
                "sms": ["接收短信", "短信验证"],
                "password": ["验证登录密码", "密码"],
                "face": ["刷脸", "人脸"],
            }
            labels = method_labels.get(method, [])

            click_js = f"""() => {{
                const items = document.querySelectorAll('li, div[class*="item"], [role="button"], [class*="option"]');
                for (const el of items) {{
                    const t = el.textContent.trim();
                    if ({(' || ').join([f't.includes("{l}")' for l in labels])}) {{
                        el.click();
                        return t;
                    }}
                }}
                return null;
            }}"""

            selected = await self.page.evaluate(click_js)
            if selected:
                print(f"[OK] 已选择: {selected}")
                await asyncio.sleep(2)

                # 根据方式执行后续操作
                if method == "sms" and phone:
                    print(f"\n[INFO] 输入手机号: {phone[:3]}****{phone[-4:]}")
                    await self.input_phone(phone)
                    await asyncio.sleep(1)
                    if await self.click_send_sms_button():
                        print("[OK] 验证码已发送")
                        code = input("  > 验证码: ").strip()
                        if code:
                            await self.input_sms_code(code)
                            await self.click_submit_button()

                elif method == "password" and password:
                    print("\n[INFO] 输入验证密码...")
                    await self.input_password(password)
                    await asyncio.sleep(1)
                    await self.click_submit_button()

                elif method == "face":
                    print("\n[INFO] 请在手机上完成人脸识别")
                    print("[INFO] 等待中...")

                result.success = True
                print(f"\n✅ 操作已完成，请关注浏览器中的后续提示")
            else:
                result.error = f"未找到 '{method}' 对应的验证选项"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def scrape_feed(self, url: str = None, max_items: int = 20) -> ScrapedData:
        """登录后抓取数据"""
        data = ScrapedData(url=url or self.DOUYIN_URL, timestamp=time.time())
        try:
            target = url or self.DOUYIN_URL
            print(f"\n[抓取] 目标: {target}")

            # 滚动加载
            for i in range(max_items // 5):
                await self.page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(2)

            data.title = await self.page.title()
            data.html = await self.page.content()

            # 提取媒体信息
            extracted = await self.page.evaluate("""() => {
                const videos = [];
                document.querySelectorAll('video').forEach(v => {
                    videos.push({ src: v.src || '', poster: v.poster || '' });
                });
                const images = [];
                document.querySelectorAll('img[src*="byteimg"]').forEach(img => {
                    images.push(img.src);
                });
                return JSON.stringify({ videos, images, url: location.href });
            }""")

            try:
                parsed = json.loads(extracted)
                data.videos = parsed.get("videos", [])
                data.images = parsed.get("images", [])
            except (json.JSONDecodeError, TypeError):
                pass

            # 保存
            out = self.output_dir / f"scrape_v3_{int(time.time())}.md"
            out.write_text(f"# {data.title}\n\nURL: {data.url}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n视频: {len(data.videos)}\n图片: {len(data.images)}\n",
                          encoding="utf-8")
            print(f"[OK] 数据已保存: {out}")

        except Exception as e:
            print(f"[ERROR] 抓取失败: {e}")

        return data


# ──────────────────── CLI 入口 ────────────────────


async def cmd_qr(args):
    """二维码登录命令"""
    print("=" * 55)
    print("  🔐 抖音二维码登录 - Crawl4AI V3")
    print("=" * 55)

    async def on_qrcode(img: bytes):
        p = Path(args.output) / "qrcode_live.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(img)
        print(f"\n📱 二维码已更新: {p}")
        print("   用抖音 App 扫描此二维码\n")

    async with DouyinCrawlerV3(output_dir=args.output, headless=not args.visible) as c:
        r = await c.login_qr(on_qrcode=on_qrcode, poll_interval=args.poll_interval, timeout=args.timeout)
        if not r.success:
            print(f"\n❌ 登录失败: {r.error}")
            sys.exit(1)


async def cmd_sms(args):
    """短信登录命令"""
    print("=" * 55)
    print("  📱 抖音短信验证码登录 - Crawl4AI V3")
    print("=" * 55)
    async with DouyinCrawlerV3(output_dir=args.output, headless=not args.visible) as c:
        r = await c.login_sms(args.phone)
        if not r.success:
            print(f"\n❌ 登录失败: {r.error}")


async def cmd_password(args):
    """密码登录命令"""
    print("=" * 55)
    print("  🔑 抖音密码登录 - Crawl4AI V3")
    print("=" * 55)
    async with DouyinCrawlerV3(output_dir=args.output, headless=not args.visible) as c:
        r = await c.login_password(args.phone, args.password)
        if not r.success:
            print(f"\n❌ 登录失败: {r.error}")


async def cmd_verify(args):
    """身份验证命令"""
    print("=" * 55)
    print("  🛡️  抖音身份验证处理 - Crawl4AI V3")
    print("=" * 55)
    async with DouyinCrawlerV3(output_dir=args.output, headless=not args.visible) as c:
        r = await c.handle_identity_verify(
            method=args.method,
            phone=args.phone or "",
            password=args.password or "",
        )
        if not r.success:
            print(f"\n❌ 操作失败: {r.error}")


async def cmd_scrape(args):
    """数据抓取命令"""
    print("=" * 55)
    print("  📊 抖音数据抓取 - Crawl4AI V3")
    print("=" * 55)
    async with DouyinCrawlerV3(output_dir=args.output) as c:
        if args.cookies and Path(args.cookies).exists():
            await c.load_cookies(args.cookies)
        d = await c.scrape_feed(url=args.url, max_items=args.max_items)
        if d.videos or d.images:
            print(f"\n✅ 抓取完成! 视频:{len(d.videos)} 图片:{len(d.images)}")


def main():
    parser = argparse.ArgumentParser(
        description="抖音网页登录爬虫 - Crawl4AI V3 (基于 Patchright 反检测)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  # 二维码登录（推荐）
  python %(prog)s qr --visible

  # 短信验证码登录
  python %(prog)s sms --phone 13800138000

  # 密码登录（可能触发身份验证）
  python %(prog)s password --phone 13800138000 --password your_pass

  # 处理身份验证（你截图中的界面）
  python %(prog)s verify --method sms --phone 13800138000

  # 登录后抓取数据
  python %(prog)s scrape --cookies output/session_v3_xxx.json
""",
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("qr", help="二维码扫码登录")
    p.add_argument("-o", "--output", default="./output")
    p.add_argument("--visible", action="store_true")
    p.add_argument("--poll-interval", type=float, default=3.0)
    p.add_argument("--timeout", type=float, default=180.0)

    p = sub.add_parser("sms", help="短信验证码登录")
    p.add_argument("--phone", required=True)
    p.add_argument("-o", "--output", default="./output")
    p.add_argument("--visible", action="store_true")

    p = sub.add_parser("password", help="密码登录")
    p.add_argument("--phone", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("-o", "--output", default="./output")
    p.add_argument("--visible", action="store_true")

    p = sub.add_parser("verify", help="处理身份验证")
    p.add_argument("--method", choices=["sms", "password", "face"], default="sms")
    p.add_argument("--phone", default="")
    p.add_argument("--password", default="")
    p.add_argument("-o", "--output", default="./output")
    p.add_argument("--visible", action="store_true")

    p = sub.add_parser("scrape", help="登录后抓取数据")
    p.add_argument("--url", default="https://www.douyin.com/jingxuan")
    p.add_argument("--cookies", default="")
    p.add_argument("-o", "--output", default="./output")
    p.add_argument("--max-items", type=int, default=20)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    cmds = {"qr": cmd_qr, "sms": cmd_sms, "password": cmd_password,
            "verify": cmd_verify, "scrape": cmd_scrape}
    fn = cmds.get(args.command)
    if fn:
        asyncio.run(fn(args))


if __name__ == "__main__":
    main()
