#!/usr/bin/env python3
"""
抖音网页登录爬虫 - Crawl4AI 增强版

基于 Crawl4AI 的反检测能力 + Patchright 浏览器自动化
支持截图中的所有登录/验证方式：
  ✅ 二维码扫码登录
  ✅ 短信验证码登录
  ✅ 密码登录
  ✅ 身份验证处理（短信/密码/刷脸）

用法:
    # 二维码登录（推荐）
    python douyin_crawl4ai_v2.py qr [--visible]

    # 短信验证码登录
    python douyin_crawl4ai_v2.py sms --phone 13800138000

    # 密码登录
    python douyin_crawl4ai_v2.py password --phone 13800138000 --password xxx

    # 处理身份验证页面
    python douyin_crawl4ai_v2.py verify --method sms --phone 13800138000

    # 登录后抓取数据
    python douyin_crawl4ai_v2.py scrape --cookies output/session_cookies.json
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
from typing import Optional, Callable, Awaitable, Any

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


# ──────────────────── 反检测脚本 ────────────────────

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
    p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : _origQuery(p);
"""

DOUYIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ──────────────────── 核心爬虫类 ────────────────────


class DouyinCrawlerV2:
    """
    抖音登录爬虫 V2 - Crawl4AI 增强版

    使用策略：
    1. 通过 Crawl4AI 创建带反检测的浏览器实例
    2. 获取底层 Patchright (Playwright fork) 的 Page 对象
    3. 使用原生 Playwright API 进行精确的交互操作
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
        self.session_id = f"douyin_{int(time.time())}"
        self._crawler = None
        self._page = None  # 底层 Page 对象

    async def _init_crawler(self):
        """初始化 Crawl4AI 并获取底层 Page 对象"""
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )

        browser_config = BrowserConfig(
            headless=self.headless,
            viewport_width=1440,
            viewport_height=900,
            user_agent=DOUYIN_UA,
            proxy_config={"server": proxy} if proxy else None,
        )

        self._crawler = AsyncWebCrawler(config=browser_config)
        await self._crawler.start()

        # 获取底层浏览器上下文和页面
        # Crawl4AI 内部使用 Patchright，可以通过这种方式获取
        browser = self._crawler.browser
        if browser:
            context = browser.contexts[0] if browser.contexts else await browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=DOUYIN_UA,
                locale="zh-CN",
            )
            # 注入反检测脚本
            await context.add_init_script(STEALTH_JS)
            self._page = await context.new_page()
        else:
            raise RuntimeError("无法创建浏览器实例")

        return self._page

    async def close(self):
        """关闭资源"""
        if self._crawler:
            await self._crawler.close()
            self._crawler = None
            self._page = None

    async def __aenter__(self):
        await self._init_crawler()
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def page(self):
        """获取底层 Page 对象"""
        if self._page is None:
            raise RuntimeError("请先调用 _init_crawler() 或使用 async with")
        return self._page

    # ────── 页面操作方法 ──────

    async def open_douyin(self, url: str = None) -> bool:
        """打开抖音页面"""
        url = url or self.DOUYIN_URL
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print(f"[OK] 页面已打开: {url}")
            await asyncio.sleep(5)  # 等待 JS 渲染
            return True
        except Exception as e:
            print(f"[WARN] 页面加载异常: {e}，继续...")
            await asyncio.sleep(5)
            return True

    async def click_login_button(self) -> bool:
        """点击登录按钮"""
        selectors = [
            'button:has-text("登录")',
            '.semi-button-primary:has-text("登录")',
            'text="登录"',
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

        # Fallback: JS 点击
        clicked = await self.page.evaluate("""() => {
            const btns = document.querySelectorAll('button');
            for (const b of btns) {
                if (b.textContent.includes('登录')) {
                    b.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            print("[OK] 已通过 JS 点击登录按钮")
            return True

        print("[WARN] 未找到登录按钮")
        return False

    async def wait_for_panel(self, timeout: float = 15.0) -> bool:
        """等待登录面板出现"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                panel = self.page.locator('#login-panel-new').first
                if await panel.count() > 0 and await panel.is_visible():
                    box = await panel.bounding_box()
                    print(f"[OK] 登录面板已出现 ({int(box['width'])}x{int(box['height'])})")
                    return True
            except Exception:
                pass

            # 也检查其他可能的 selector
            try:
                alt_panel = self.page.locator('[class*="login-panel"]').first
                if await alt_panel.count() > 0 and await alt_panel.is_visible():
                    print("[OK] 登录面板已出现 (alternative)")
                    return True
            except Exception:
                pass

            await asyncio.sleep(0.8)

        print("[WARN] 等待登录面板超时")
        return False

    async def extract_qrcode(self) -> tuple[bool, bytes, str]:
        """
        提取二维码图片
        Returns: (success, image_bytes, error_message)
        """
        try:
            # 方法1: 从 img src 提取 base64
            info = await self.page.evaluate("""() => {
                const scanCode = document.querySelector('#douyin_login_comp_scan_code');
                if (!scanCode) return { found: false, reason: 'no_scan_code_element' };
                const img = scanCode.querySelector('img');
                if (!img) return { found: false, reason: 'no_img_in_scan_code' };
                return {
                    found: true,
                    src: img.src,
                    naturalWidth: img.naturalWidth,
                    naturalHeight: img.naturalHeight,
                };
            }""")

            if not info.get("found"):
                return False, b"", info.get("reason", "unknown")

            src = info["src"]
            if src.startswith("data:image/"):
                _, b64_data = src.split(",", 1)
                img_bytes = base64.b64decode(b64_data)
                print(f"[OK] 二维码提取成功 ({info['naturalWidth']}x{info['naturalHeight']})")
                return True, img_bytes, ""
            else:
                return False, b"", f"非预期的图片格式: {src[:60]}"

        except Exception as e:
            return False, b"", str(e)

    async def screenshot_panel(self) -> bytes:
        """截取登录面板截图"""
        try:
            panel = self.page.locator('#login-panel-new').first
            if await panel.count() > 0:
                return await panel.screenshot()
        except Exception:
            pass
        return b""

    async def check_login_status(self) -> dict:
        """检查是否已登录"""
        return await self.page.evaluate("""() => {
            const panel = document.querySelector('#login-panel-new');
            const panelVisible = panel && panel.offsetParent !== null;

            // 检查用户信息元素
            const avatar = document.querySelector('[class*="avatar"], [class*="Avatar"]');
            const nickname = document.querySelector('[class*="nickname"], [class*="Nickname"]');

            // 检查身份验证页面
            const verifyPage = !!document.querySelector('[class*="identity-verify"]')
                             || !!document.querySelector('[class*="IdentityVerify"]')
                             || document.body.innerText.includes('身份验证');

            return {
                logged_in: !panelVisible && !verifyPage,
                panel_visible: panelVisible,
                verify_page: verifyPage,
                has_avatar: !!avatar,
                nickname: nickname?.textContent?.trim() || '',
                url: location.href,
            };
        }""")

    async def input_phone(self, phone: str) -> bool:
        """输入手机号"""
        try:
            # 查找手机号输入框
            phone_input = await self.page.evaluate("""(phone) => {
                const inputs = document.querySelectorAll('input');
                for (const inp of inputs) {
                    const ph = (inp.placeholder || '').toLowerCase();
                    const type = inp.type || '';
                    if (ph.includes('手机') || ph.includes('phone') || ph.includes('号码') || type === 'tel') {
                        // React 合成事件
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, phone);
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
                // fallback: 第一个可见输入框
                for (const inp of inputs) {
                    if (inp.offsetParent !== null && inp.type !== 'hidden') {
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, phone);
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
                return false;
            }""", phone)
            return phone_input
        except Exception as e:
            print(f"[ERROR] 输入手机号失败: {e}")
            return False

    async def input_password(self, password: str) -> bool:
        """输入密码"""
        try:
            result = await self.page.evaluate("""(pwd) => {
                const pwdInput = document.querySelector('input[type="password"]');
                if (!pwdInput) return false;
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(pwdInput, pwd);
                pwdInput.dispatchEvent(new Event('input', { bubbles: true }));
                pwdInput.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""", password)
            return result
        except Exception as e:
            print(f"[ERROR] 输入密码失败: {e}")
            return False

    async def click_send_sms(self) -> bool:
        """点击发送验证码"""
        try:
            result = await self.page.evaluate("""() => {
                const elements = document.querySelectorAll('button, [role="button"], div[class*="send"], span[class*="send"], div[class*="code"]');
                for (const el of elements) {
                    const text = el.textContent || '';
                    if ((text.includes('发送') || text.includes('获取')) && text.includes('验证')) {
                        el.click();
                        return { clicked: true, text: text.trim() };
                    }
                }
                return { clicked: false };
            }""")
            if result.get("clicked"):
                print(f"[OK] 已点击: {result.get('text')}")
                return True
            return False
        except Exception as e:
            print(f"[ERROR] 发送验证码失败: {e}")
            return False

    async def input_sms_code(self, code: str) -> bool:
        """输入短信验证码"""
        try:
            result = await self.page.evaluate("""(code) => {
                const inputs = document.querySelectorAll('input');
                let codeInput = null;
                for (const inp of inputs) {
                    if ((inp.placeholder || '').includes('验证码') || (inp.placeholder || '').includes('code')) {
                        codeInput = inp;
                        break;
                    }
                }
                if (!codeInput && inputs.length >= 2) codeInput = inputs[1];
                if (!codeInput) return false;

                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(codeInput, code);
                codeInput.dispatchEvent(new Event('input', { bubbles: true }));
                codeInput.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""", code)
            return result
        except Exception as e:
            print(f"[ERROR] 输入验证码失败: {e}")
            return False

    async def click_submit(self) -> bool:
        """点击提交/登录按钮"""
        selectors = [
            'button[type="submit"]',
            'button:has-text("登录")',
            'button:has-text("提交")',
            '[class*="submit"]',
            '[class*="login-btn"]',
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
        print("[WARN] 未找到提交按钮")
        return False

    async def save_cookies(self) -> str:
        """保存 Cookies 到文件"""
        try:
            cookies = await self.context_cookies()
            cookie_file = str(self.output_dir / f"session_{self.session_id}.json")
            with open(cookie_file, "w", encoding="utf-8") as f:
                json.dump({
                    "session_id": self.session_id,
                    "timestamp": time.time(),
                    "url": self.page.url,
                    "cookies": cookies,
                }, f, ensure_ascii=False, indent=2)
            print(f"[OK] Cookies 已保存: {cookie_file}")
            return cookie_file
        except Exception as e:
            print(f"[ERROR] 保存 Cookies 失败: {e}")
            return ""

    async def context_cookies(self) -> list:
        """获取当前上下文的 Cookies"""
        if self._crawler and self._crawler.browser:
            contexts = self._crawler.browser.contexts
            if contexts:
                return await contexts[0].cookies()
        return []

    async def load_cookies(self, filepath: str) -> bool:
        """从文件加载 Cookies"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            cookies = data.get("cookies", [])
            if not cookies:
                print("[ERROR] 文件中没有有效的 Cookies")
                return False

            if self._crawler and self._crawler.browser:
                contexts = self._crawler.browser.contexts
                if contexts:
                    await contexts[0].add_cookies(cookies)
                    print(f"[OK] 已加载 {len(cookies)} 条 Cookie")
                    return True
            return False
        except Exception as e:
            print(f"[ERROR] 加载 Cookies 失败: {e}")
            return False

    # ────── 登录流程实现 ──────

    async def login_qr(
        self,
        on_qrcode: Optional[Callable[[bytes], Awaitable[None]]] = None,
        poll_interval: float = 3.0,
        timeout: float = 180.0,
    ) -> LoginResult:
        """
        二维码登录完整流程

        Args:
            on_qrcode: 回调函数，接收二维码图片 bytes
            poll_interval: 扫码状态轮询间隔
            timeout: 总超时时间
        """
        result = LoginResult(method="qr", timestamp=time.time())

        try:
            # Step 1: 打开页面
            print("\n[1/6] 正在打开抖音精选页...")
            await self.open_douyin()

            # Step 2: 点击登录
            print("[2/6] 点击登录按钮...")
            if not await self.click_login_button():
                result.error = "无法点击登录按钮"
                return result
            await asyncio.sleep(3)

            # Step 3: 等待面板
            print("[3/6] 等待登录面板...")
            if not await self.wait_for_panel(timeout=15):
                result.error = "登录面板未出现"
                # 截图调试
                debug_ss = self.output_dir / "debug_no_panel.png"
                await self.page.screenshot(path=str(debug_ss))
                print(f"[DEBUG] 截图已保存: {debug_ss}")
                return result

            # Step 4: 切换到二维码 tab（如果需要）
            print("[4/6] 准备提取二维码...")
            try:
                qr_tab = self.page.locator('#douyin_login_comp_scan_code').first
                if await qr_tab.count() > 0:
                    if not await qr_tab.is_visible():
                        # 尝试点击二维码 tab
                        await self.page.evaluate("""() => {
                            const tabs = document.querySelectorAll('[class*="tab"], li[class*="tab"], [role="tab"]');
                            for (const t of tabs) {
                                if (t.textContent.includes('扫码') || t.textContent.includes('QR')) {
                                    t.click();
                                    break;
                                }
                            }
                        }""")
                        await asyncio.sleep(2)
            except Exception:
                pass

            # Step 5: 提取二维码
            print("[5/6] 提取二维码...")
            success, img_bytes, err = await self.extract_qrcode()

            if not success:
                result.error = f"二维码提取失败: {err}"
                # 截图面板
                panel_ss = await self.screenshot_panel()
                if panel_ss:
                    ps_path = self.output_dir / "debug_panel.png"
                    ps_path.write_bytes(panel_ss)
                    print(f"[DEBUG] 面板截图: {ps_path}")
                return result

            result.qrcode_image = img_bytes
            result.qrcode_base64 = base64.b64encode(img_bytes).decode()

            # 保存二维码
            qr_path = self.output_dir / "qrcode_v2.png"
            qr_path.write_bytes(img_bytes)
            print(f"[OK] 二维码已保存: {qr_path}")

            # 保存面板截图
            panel_ss = await self.screenshot_panel()
            if panel_ss:
                p_path = self.output_dir / "panel_v2.png"
                p_path.write_bytes(panel_ss)
                result.screenshot_bytes = panel_ss

            # 回调通知
            if on_qrcode:
                await on_qrcode(img_bytes)

            # Step 6: 等待扫码
            print(f"\n[6/6] ⏳ 等待扫码（超时 {timeout:.0f}s）...")
            print("   请用抖音 App 扫描上方二维码\n")

            start = time.time()
            while time.time() - start < timeout:
                status = await self.check_login_status()

                if status.get("logged_in"):
                    result.success = True
                    result.user_info = status
                    cookie_file = await self.save_cookies()
                    result.cookie_file = cookie_file
                    print(f"\n{'='*50}")
                    print(f"  ✅ 扫码登录成功!")
                    print(f"  用户: {status.get('nickname', 'N/A')}")
                    print(f"  Session: {self.session_id}")
                    print(f"{'='*50}\n")
                    return result

                if status.get("verify_page"):
                    print("\n[INFO] 检测到身份验证页面，可能需要额外验证")
                    print("[INFO] 可用命令: python ... verify --method sms --phone XXX")
                    # 继续等待，用户可能手动完成验证
                    pass

                # 检查二维码是否过期
                qr_ok, _, _ = await self.extract_qrcode()
                if not qr_ok:
                    print("[WARN] 二维码已过期，刷新中...")
                    # 刷新页面重新获取
                    await self.open_douyin()
                    await self.click_login_button()
                    await self.wait_for_panel()
                    success, img_bytes, err = await self.extract_qrcode()
                    if success:
                        result.qrcode_image = img_bytes
                        result.qrcode_base64 = base64.b64encode(img_bytes).decode()
                        qr_path.write_bytes(img_bytes)
                        if on_qrcode:
                            await on_qrcode(img_bytes)
                        print("[OK] 二维码已刷新")

                elapsed = time.time() - start
                remaining = timeout - elapsed
                if int(remaining) % 10 == 0 and int(remaining) > 0:
                    print(f"   ⏱️  剩余 {int(remaining)}s...")

                await asyncio.sleep(poll_interval)

            result.error = "超时：未在规定时间内完成扫码"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def login_sms(self, phone: str) -> LoginResult:
        """短信验证码登录"""
        result = LoginResult(method="sms", timestamp=time.time())

        try:
            print("\n[1/5] 打开抖音页面...")
            await self.open_douyin()

            print("[2/5] 点击登录...")
            await self.click_login_button()
            await asyncio.sleep(3)

            print("[3/5] 等待面板...")
            if not await self.wait_for_panel():
                result.error = "登录面板未出现"
                return result

            # 切换到短信登录 tab
            print("[INFO] 切换到短信登录模式...")
            await self.page.evaluate("""() => {
                const tabs = document.querySelectorAll('li, [class*="tab"], [role="tab"], div[class*="tab-item"]');
                for (const t of tabs) {
                    const txt = t.textContent || '';
                    if (txt.includes('短信') || txt.includes('手机号')) {
                        t.click();
                        return true;
                    }
                }
                return false;
            }""")
            await asyncio.sleep(2)

            print(f"[4/5] 输入手机号 {phone[:3]}****{phone[-4:]}...")
            if not await self.input_phone(phone):
                result.error = "输入手机号失败"
                return result
            await asyncio.sleep(1)

            print("[5/5] 发送验证码...")
            if not await self.click_send_sms():
                result.error = "发送验证码失败"
                return result

            print(f"\n✅ 验证码已发送至 {phone[:3]}****{phone[-4:]}")
            print("请在下方输入收到的验证码:\n")

            # 等待用户输入验证码
            code = input("  > 验证码: ").strip()
            if code:
                print("[INFO] 输入验证码并提交...")
                await self.input_sms_code(code)
                await asyncio.sleep(1)
                await self.click_submit()
                await asyncio.sleep(5)

                status = await self.check_login_status()
                if status.get("logged_in"):
                    result.success = True
                    result.user_info = status
                    result.cookie_file = await self.save_cookies()
                    print(f"\n✅ 短信验证码登录成功!")
                elif status.get("verify_page"):
                    result.error = "触发了身份验证，请手动完成或使用 verify 命令"
                else:
                    result.error = "登录状态未知，请检查验证码是否正确"
            else:
                result.error = "未输入验证码"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def login_password(self, phone: str, password: str) -> LoginResult:
        """密码登录"""
        result = LoginResult(method="password", timestamp=time.time())

        try:
            print("\n[1/5] 打开抖音页面...")
            await self.open_douyin()

            print("[2/5] 点击登录...")
            await self.click_login_button()
            await asyncio.sleep(3)

            print("[3/5] 等待面板...")
            if not await self.wait_for_panel():
                result.error = "登录面板未出现"
                return result

            # 切换到密码登录
            print("[INFO] 切换到密码登录模式...")
            await self.page.evaluate("""() => {
                const tabs = document.querySelectorAll('li, [class*="tab"], [role="tab"]');
                for (const t of tabs) {
                    if (t.textContent.includes('密码')) {
                        t.click();
                        return true;
                    }
                }
                return false;
            }""")
            await asyncio.sleep(2)

            print(f"[4/5] 输入账号信息...")
            await self.input_phone(phone)
            await asyncio.sleep(1)

            print("[5/5] 输入密码并登录...")
            if not await self.input_password(password):
                result.error = "输入密码失败"
                return result

            await asyncio.sleep(1)
            await self.click_submit()
            await asyncio.sleep(5)

            status = await self.check_login_status()
            if status.get("logged_in"):
                result.success = True
                result.user_info = status
                result.cookie_file = await self.save_cookies()
                print(f"\n✅ 密码登录成功!")
            elif status.get("verify_page"):
                result.error = "触发身份验证！建议使用 QR 或 SMS 方式登录"
                print("\n⚠️  触发了身份验证（如截图所示）")
                print("   可选方案:")
                print("   1. 使用二维码登录: python ... qr")
                print("   2. 完成身份验证: python ... verify --method sms")
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
        处理身份验证页面（对应截图中的界面）

        验证选项：
        - 接收短信验证码
        - 手机刷脸验证
        - 验证登录密码
        - 发送短信验证
        """
        result = LoginResult(method=f"verify_{method}", timestamp=time.time())

        try:
            # 检查是否在验证页面
            status = await self.check_login_status()
            if not status.get("verify_page"):
                # 可能还没打开页面，先尝试打开
                print("[INFO] 正在打开抖音页面...")
                await self.open_douyin()
                await self.click_login_button()
                await asyncio.sleep(3)
                status = await self.check_login_status()

            if not status.get("verify_page"):
                print("[INFO] 当前不在身份验证页面")
                print("[INFO] 请先执行登录操作触发验证流程")
                result.error = "不在身份验证页面"
                return result

            print(f"\n{'='*50}")
            print(f"  📋 身份验证页面检测到")
            print(f"{'='*50}")

            # 显示可用选项
            options = await self.page.evaluate("""() => {
                const opts = [];
                const items = document.querySelectorAll('li, div[class*="option"], div[class*="item"], [class*="verify-option"]');
                for (const item of items) {
                    const text = item.textContent.trim().replace(/\\s+/g, ' ');
                    if (text.length < 50 && (text.includes('短信') || text.includes('密码') || text.includes('刷脸'))) {
                        opts.push(text);
                    }
                }
                return opts;
            }""")

            if options:
                print(f"\n  可选验证方式:")
                for i, opt in enumerate(options, 1):
                    print(f"    {i}. {opt}")

            # 根据选择的方法点击对应选项
            click_js_map = {
                "sms": """() => {
                    const items = document.querySelectorAll('li, div[class*="item"], [role="button"]');
                    for (const el of items) {
                        const t = el.textContent.trim();
                        if ((t.includes('短信') || t.includes('接收')) && !t.includes('发送')) {
                            el.click();
                            return t;
                        }
                    }
                    return null;
                }""",
                "password": """() => {
                    const items = document.querySelectorAll('li, div[class*="item"], [role="button"]');
                    for (const el of items) {
                        const t = el.textContent.trim();
                        if (t.includes('密码') && !t.includes('短信')) {
                            el.click();
                            return t;
                        }
                    }
                    return null;
                }""",
                "face": """() => {
                    const items = document.querySelectorAll('li, div[class*="item"], [role="button"]');
                    for (const el of items) {
                        const t = el.textContent.trim();
                        if (t.includes('刷脸') || t.includes('人脸')) {
                            el.click();
                            return t;
                        }
                    }
                    return null;
                }""",
            }

            click_js = click_js_map.get(method)
            if click_js:
                selected = await self.page.evaluate(click_js)
                if selected:
                    print(f"\n[OK] 已选择: {selected}")
                    await asyncio.sleep(2)

                    # 执行后续操作
                    if method == "sms" and phone:
                        print(f"[INFO] 输入手机号: {phone[:3]}****{phone[-4:]}")
                        await self.input_phone(phone)
                        await asyncio.sleep(1)
                        if await self.click_send_sms():
                            print("[OK] 验证码已发送，请查看手机")
                            code = input("  > 验证码: ").strip()
                            if code:
                                await self.input_sms_code(code)
                                await self.click_submit()

                    elif method == "password" and password:
                        print("[INFO] 请输入密码完成验证...")
                        await self.input_password(password)
                        await asyncio.sleep(1)
                        await self.click_submit()

                    elif method == "face":
                        print("\n[INFO] 请在手机上完成人脸识别")
                        print("[INFO] 等待验证完成...")

                    result.success = True
                else:
                    result.error = f"未找到 '{method}' 验证选项"
            else:
                result.error = f"不支持的验证方式: {method}"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def scrape_feed(self, url: str = None, max_items: int = 20) -> ScrapedData:
        """登录后抓取推荐流数据"""
        data = ScrapedData(url=url or self.DOUYIN_URL, timestamp=time.time())

        try:
            target_url = url or self.DOUYIN_URL
            print(f"\n[抓取] 目标: {target_url}")

            # 滚动加载内容
            for i in range(max_items // 5):  # 每次滚动约加载5条
                await self.page.evaluate(f"window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(2)

            # 用 Crawl4AI 提取内容
            from crawl4ai import CrawlerRunConfig
            config = CrawlerRunConfig(
                session_id=self.session_id,
                page_timeout=30000,
                markdown_generator=None,  # 使用默认
            )

            # 直接从当前页面获取内容
            html_content = await self.page.content()
            data.html = html_content
            data.title = await self.page.title()

            # 提取文本内容
            text_content = await self.page.evaluate("""() => {
                // 提取视频信息
                const videos = [];
                document.querySelectorAll('video, [data-e2e="feed-item-video"]').forEach(v => {
                    videos.push({
                        src: v.src || v.currentSrc || '',
                        poster: v.poster || '',
                    });
                });

                // 提取图片
                const images = [];
                document.querySelectorAll('img[src*="byteimg"]').forEach(img => {
                    images.push(img.src);
                });

                return JSON.stringify({ videos, images, url: location.href });
            }""")

            try:
                extracted = json.loads(text_content)
                data.videos = extracted.get("videos", [])
                data.images = extracted.get("images", [])
            except (json.JSONDecodeError, TypeError):
                pass

            # 保存结果
            md_path = self.output_dir / f"scrape_v2_{int(time.time())}.md"
            md_path.write_text(f"# {data.title}\n\nURL: {data.url}\n\n抓取时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n---\n\n", encoding="utf-8")
            print(f"[OK] 数据已保存: {md_path}")
            print(f"[OK] 视频: {len(data.videos)}, 图片: {len(data.images)}")

        except Exception as e:
            print(f"[ERROR] 抓取失败: {e}")

        return data


# ──────────────────── CLI 入口 ────────────────────


async def cmd_qr(args):
    """二维码登录"""
    print("=" * 55)
    print("  🔐 抖音二维码登录 - Crawl4AI V2")
    print("=" * 55)

    async def on_qrcode(img_bytes: bytes):
        qr_path = Path(args.output) / "qrcode_live.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(img_bytes)
        print(f"\n📱 二维码已更新: {qr_path}")
        print("   请用抖音 App 扫描此二维码\n")

    async with DouyinCrawlerV2(output_dir=args.output, headless=not args.visible) as client:
        result = await client.login_qr(
            on_qrcode=on_qrcode,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )

        if not result.success:
            print(f"\n❌ 登录失败: {result.error}")
            sys.exit(1)


async def cmd_sms(args):
    """短信登录"""
    print("=" * 55)
    print("  📱 抖音短信验证码登录 - Crawl4AI V2")
    print("=" * 55)

    async with DouyinCrawlerV2(output_dir=args.output, headless=not args.visible) as client:
        result = await client.login_sms(phone=args.phone)

        if not result.success:
            print(f"\n❌ 登录失败: {result.error}")


async def cmd_password(args):
    """密码登录"""
    print("=" * 55)
    print("  🔑 抖音密码登录 - Crawl4AI V2")
    print("=" * 55)

    async with DouyinCrawlerV2(output_dir=args.output, headless=not args.visible) as client:
        result = await client.login_password(phone=args.phone, password=args.password)

        if not result.success:
            print(f"\n❌ 登录失败: {result.error}")


async def cmd_verify(args):
    """身份验证"""
    print("=" * 55)
    print("  🛡️  抖音身份验证处理 - Crawl4AI V2")
    print("=" * 55)

    async with DouyinCrawlerV2(output_dir=args.output, headless=not args.visible) as client:
        result = await client.handle_identity_verify(
            method=args.method,
            phone=args.phone or "",
            password=args.password or "",
        )

        if result.success:
            print("\n✅ 操作已完成，请关注浏览器中的后续提示")
        else:
            print(f"\n❌ 操作失败: {result.error}")


async def cmd_scrape(args):
    """数据抓取"""
    print("=" * 55)
    print("  📊 抖音数据抓取 - Crawl4AI V2")
    print("=" * 55)

    async with DouyinCrawlerV2(output_dir=args.output) as client:
        if args.cookies and Path(args.cookies).exists():
            await client.load_cookies(args.cookies)

        data = await client.scrape_feed(url=args.url, max_items=args.max_items)

        if data.markdown or data.videos:
            print(f"\n✅ 抓取完成!")
            print(f"   标题: {data.title}")


def main():
    parser = argparse.ArgumentParser(
        description="抖音网页登录爬虫 - Crawl4AI V2 增强版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
╔════════════════════════════════════════════╗
║  示例                                        ║
╠════════════════════════════════════════════╣
║  # 二维码登录（推荐）                         ║
║  python %(prog)s qr                          ║
║                                              ║
║  # 短信验证码登录                            ║
║  python %(prog)s sms --phone 13800138000     ║
║                                              ║
║  # 密码登录                                  ║
║  python %(prog)s password --phone 138... \   ║
║                  --password your_pass        ║
║                                              ║
║  # 处理身份验证（截图中的界面）               ║
║  python %(prog)s verify --method sms \       ║
║                  --phone 13800138000         ║
║                                              ║
║  # 登录后抓取数据                            ║
║  python %(prog)s scrape \                    ║
║       --cookies output/session_xxx.json      ║
╚════════════════════════════════════════════╝
        """,
    )
    sub = parser.add_subparsers(dest="command", help="功能模块")

    # QR
    p = sub.add_parser("qr", help="📸 二维码扫码登录")
    p.add_argument("--output", "-o", default="./output", help="输出目录")
    p.add_argument("--visible", action="store_true", help="显示浏览器窗口")
    p.add_argument("--poll-interval", type=float, default=3.0, help="轮询间隔(秒)")
    p.add_argument("--timeout", type=float, default=180.0, help="超时(秒)")

    # SMS
    p = sub.add_parser("sms", help="📱 短信验证码登录")
    p.add_argument("--phone", required=True, help="手机号")
    p.add_argument("--output", "-o", default="./output")
    p.add_argument("--visible", action="store_true")

    # Password
    p = sub.add_parser("password", help="🔑 密码登录")
    p.add_argument("--phone", required=True, help="手机号")
    p.add_argument("--password", required=True, help="密码")
    p.add_argument("--output", "-o", default="./output")
    p.add_argument("--visible", action="store_true")

    # Verify
    p = sub.add_parser("verify", help="🛡️  处理身份验证")
    p.add_argument("--method", choices=["sms", "password", "face"], default="sms",
                   help="验证方式: sms=短信, password=密码, face=刷脸")
    p.add_argument("--phone", help="手机号（短信方式需要）")
    p.add_argument("--password", help="密码（密码方式需要）")
    p.add_argument("--output", "-o", default="./output")
    p.add_argument("--visible", action="store_true")

    # Scrape
    p = sub.add_parser("scrape", help="📊 登录后抓取数据")
    p.add_argument("--url", default="https://www.douyin.com/jingxuan")
    p.add_argument("--cookies", help="Cookie 文件路径")
    p.add_argument("--output", "-o", default="./output")
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
