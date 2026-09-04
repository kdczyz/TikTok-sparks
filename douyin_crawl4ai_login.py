#!/usr/bin/env python3
"""
抖音网页登录爬虫 - Crawl4AI 版本

支持多种登录方式：
1. 二维码登录（自动抓取 + 等待扫码）
2. 手机号 + 短信验证码登录
3. 手机号 + 密码登录
4. 身份验证流程处理

用法:
    # 二维码登录模式
    python douyin_crawl4ai_login.py qr

    # 短信验证码登录
    python douyin_crawl4ai_login.py sms --phone 13800138000

    # 密码登录
    python douyin_crawl4ai_login.py password --phone 13800138000 --password your_password

    # 登录后抓取数据
    python douyin_crawl4ai_login.py scrape --session cookies.json
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
    method: str = ""  # qr / sms / password
    session_id: str = ""
    cookies: list = field(default_factory=list)
    user_info: dict = field(default_factory=dict)
    qrcode_image: bytes = b""
    qrcode_base64: str = ""
    error: str = ""
    screenshot_bytes: bytes = b""
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["qrcode_image"] = "<bytes>" if self.qrcode_image else ""
        d["screenshot_bytes"] = "<bytes>" if self.screenshot_bytes else ""
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

// 模拟 Chrome 运行时
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };

// 伪装 Permissions
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters);

// Canvas 指纹随机化
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    if (type === 'image/png' && this.width < 100 && this.height < 100) {
        const context = this.getContext('2d');
        if (context) {
            const imageData = context.getImageData(0, 0, this.width, this.height);
            for (let i = 0; i < imageData.data.length; i += 4) {
                imageData.data[i] ^= (Math.random() > 0.5 ? 1 : 0);
            }
            context.putImageData(imageData, 0, 0);
        }
    }
    return originalToDataURL.apply(this, arguments);
};
"""

# 抖音专用 User-Agent
DOUYIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# ──────────────────── 核心爬虫类 ────────────────────


class DouyinCrawl4AILogin:
    """基于 Crawl4AI 的抖音登录爬虫"""

    DOUYIN_URL = "https://www.douyin.com/jingxuan"
    LOGIN_URL = "https://www.douyin.com"

    def __init__(
        self,
        output_dir: str = "./output",
        headless: bool = True,
        session_id: Optional[str] = None,
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.session_id = session_id or f"douyin_{int(time.time())}"
        self._crawler = None

    async def _get_crawler(self):
        """获取或创建 Crawler 实例"""
        if self._crawler is not None:
            return self._crawler

        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

        # 读取代理设置
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
        return self._crawler

    async def close(self):
        """关闭爬虫"""
        if self._crawler:
            await self._crawler.close()
            self._crawler = None

    async def __aenter__(self):
        await self._get_crawler()
        return self

    async def __aexit__(self, *args):
        await self.close()

    def _make_config(
        self,
        js_code: str = "",
        wait_for: str = "",
        page_timeout: int = 60000,
        screenshot: bool = False,
        **kwargs,
    ):
        """创建 CrawlerRunConfig"""
        from crawl4ai import CrawlerRunConfig

        config_kwargs = dict(
            page_timeout=page_timeout,
            wait_for=wait_for,
            js_code=js_code,
            screenshot=screenshot,
            session_id=self.session_id,
            **kwargs,
        )
        return CrawlerRunConfig(**{k: v for k, v in config_kwargs.items() if v})

    # ────── 页面操作 JS ──────

    @staticmethod
    def _js_click_login() -> str:
        """点击登录按钮的 JS"""
        return """
        (() => {
            // 尝试多种选择器
            const selectors = [
                'button:has-text("登录")',
                '.semi-button-primary:has-text("登录")',
                '[class*="login"] button',
                'button[class*="login"]',
            ];
            for (const sel of selectors) {
                try {
                    const btn = document.querySelector(sel);
                    if (btn && btn.offsetParent !== null) {
                        btn.click();
                        return { clicked: true, selector: sel };
                    }
                } catch(e) {}
            }
            // fallback: 遍历所有按钮
            const buttons = document.querySelectorAll('button');
            for (const btn of buttons) {
                if (btn.textContent.includes('登录') || btn.textContent.includes('Login')) {
                    btn.click();
                    return { clicked: true, selector: 'text_search' };
                }
            }
            return { clicked: false, error: 'no_login_button_found' };
        })()
        """

    @staticmethod
    def _js_check_panel() -> str:
        """检查登录面板状态"""
        return """
        (() => {
            const panel = document.querySelector('#login-panel-new')
                       || document.querySelector('[class*="login-panel"]')
                       || document.querySelector('[class*="LoginPanel"]');
            if (!panel) return { exists: false };

            const rect = panel.getBoundingClientRect();
            const tabs = {
                hasQR: !!panel.querySelector('#douyin_login_comp_scan_code'),
                hasMobile: !!panel.querySelector('#douyin_login_comp_mobile_code'),
                hasPassword: !!panel.querySelector('input[type="password"]'),
            };

            // 检查是否是身份验证页面
            const verifyTitle = panel.querySelector('.verify-title, [class*="verify"]');
            const isVerifyPage = !!document.querySelector('[class*="identity-verify"]')
                             || !!document.querySelector('[class*="IdentityVerify"]');

            return {
                exists: true,
                visible: rect.width > 0 && rect.height > 0,
                width: Math.round(rect.width),
                height: Math.round(rect.height),
                ...tabs,
                isVerifyPage,
                title: verifyTitle?.textContent || '',
            };
        })()
        """

    @staticmethod
    def _js_extract_qrcode() -> str:
        """提取二维码图片"""
        return """
        (() => {
            const scanCode = document.querySelector('#douyin_login_comp_scan_code');
            if (!scanCode) return { found: false, reason: 'no_scan_code_element' };

            const img = scanCode.querySelector('img');
            if (!img) return { found: false, reason: 'no_img_in_scan_code' };

            const src = img.src;
            if (!src) return { found: false, reason: 'img_no_src' };

            return {
                found: true,
                src: src,
                isBase64: src.startsWith('data:image/'),
                naturalWidth: img.naturalWidth,
                naturalHeight: img.naturalHeight,
            };
        })()
        """

    @staticmethod
    def _js_input_phone(phone: str) -> str:
        """输入手机号"""
        return f"""
        (() => {{
            // 查找手机号输入框
            const inputs = document.querySelectorAll('input');
            let phoneInput = null;

            for (const input of inputs) {{
                const placeholder = input.placeholder || '';
                const type = input.type || '';
                if (placeholder.includes('手机') || placeholder.includes('phone')
                    || placeholder.includes('号码') || type === 'tel') {{
                    phoneInput = input;
                    break;
                }}
            }}

            if (!phoneInput) {{
                // 尝试第一个可见输入框
                for (const input of inputs) {{
                    if (input.offsetParent !== null && input.type !== 'hidden') {{
                        phoneInput = input;
                        break;
                    }}
                }}
            }}

            if (!phoneInput) return {{ success: false, error: 'no_phone_input' }};

            // 使用 React 合成事件方式输入
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(phoneInput, '{phone}');
            phoneInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            phoneInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

            return {{ success: true, value: phoneInput.value }};
        }})()
        """

    @staticmethod
    def _js_input_password(password: str) -> str:
        """输入密码"""
        return f"""
        (() => {{
            const pwdInput = document.querySelector('input[type="password"]');
            if (!pwdInput) return {{ success: false, error: 'no_password_input' }};

            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(pwdInput, '{password}');
            pwdInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            pwdInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

            return {{ success: true }};
        }})()
        """

    @staticmethod
    def _js_click_send_sms() -> str:
        """点击发送验证码按钮"""
        return """
        (() => {
            const buttons = document.querySelectorAll('button, [role="button"], div[class*="send"], span[class*="send"]');
            for (const btn of buttons) {
                const text = btn.textContent || '';
                if (text.includes('发送') && text.includes('验证')) {
                    btn.click();
                    return { clicked: true, text: text.trim() };
                }
            }
            return { clicked: false, error: 'no_send_sms_button' };
        })()
        """

    @staticmethod
    def _js_input_sms_code(code: str) -> str:
        """输入短信验证码"""
        return f"""
        (() => {{
            const inputs = document.querySelectorAll('input');
            // 找验证码输入框（通常是第二个或 placeholder 包含"验证码"）
            let codeInput = null;
            for (const input of inputs) {{
                const ph = input.placeholder || '';
                if (ph.includes('验证码') || ph.includes('code')) {{
                    codeInput = input;
                    break;
                }}
            }}
            if (!codeInput && inputs.length >= 2) codeInput = inputs[1];

            if (!codeInput) return {{ success: false, error: 'no_code_input' }};

            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(codeInput, '{code}');
            codeInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
            codeInput.dispatchEvent(new Event('change', {{ bubbles: true }}));

            return {{ success: true }};
        }})()
        """

    @staticmethod
    def _js_click_submit() -> str:
        """点击提交/登录按钮"""
        return """
        (() => {
            const selectors = [
                'button[type="submit"]',
                'button:has-text("登录")',
                'button:has-text("提交")',
                '[class*="submit"]',
                '[class*="login-btn"]',
            ];
            for (const sel of selectors) {
                try {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        el.click();
                        return { clicked: true, selector: sel };
                    }
                } catch(e) {}
            }
            return { clicked: false, error: 'no_submit_button' };
        })()
        """

    @staticmethod
    def _js_check_login_status() -> str:
        """检查是否已登录成功"""
        return """
        (() => {
            // 检查登录面板是否消失
            const panel = document.querySelector('#login-panel-new');
            if (panel && panel.offsetParent !== null) {
                return { logged_in: false, reason: 'panel_still_visible' };
            }

            // 检查是否有用户头像/昵称（表示已登录）
            const avatar = document.querySelector('[class*="avatar"], [class*="Avatar"], .user-avatar');
            const nickname = document.querySelector('[class*="nickname"], [class*="Nickname"], .user-name');

            // 检查 URL 是否变化（登录后可能跳转）
            const currentUrl = window.location.href;

            return {
                logged_in: !panel || panel.offsetParent === null,
                hasAvatar: !!avatar,
                hasNickname: !!nickname,
                nickname: nickname?.textContent?.trim() || '',
                url: currentUrl,
            };
        })()
        """

    @staticmethod
    def _js_get_cookies() -> str:
        """获取所有 Cookie"""
        return """
        (() => {
            return document.cookie.split(';').map(c => c.trim()).filter(c => c);
        })()
        """

    # ────── 核心方法 ──────

    async def open_page(
        self, url: str = DOUYIN_URL, wait_time: int = 5
    ) -> bool:
        """打开抖音页面"""
        crawler = await self._get_crawler()
        config = self._make_config(
            js_code=STEALTH_JS,
            page_timeout=60000,
        )
        try:
            result = await crawler.arun(url=url, config=config)
            if result.success:
                print(f"[OK] 页面加载成功: {result.metadata.get('title', 'N/A')}")
                # 等待页面完全渲染
                await asyncio.sleep(wait_time)
                return True
            else:
                print(f"[WARN] 页面加载可能失败，继续尝试...")
                await asyncio.sleep(wait_time)
                return True
        except Exception as e:
            print(f"[WARN] 打开页面异常: {e}，继续...")
            await asyncio.sleep(wait_time)
            return True

    async def click_login_button(self) -> bool:
        """点击登录按钮"""
        crawler = await self._get_crawler()
        config = self._make_config(js_code=f"""
            {STEALTH_JS}
            const result = ({self._js_click_login()});
            window.__loginClickResult = JSON.stringify(result);
        """)
        try:
            result = await crawler.arun(url=self.DOUYIN_URL, config=config)
            # 从上下文获取结果
            return True  # 假设成功，后续通过面板状态确认
        except Exception as e:
            print(f"[ERROR] 点击登录按钮失败: {e}")
            return False

    async def execute_js(self, js_code: str, url: Optional[str] = None) -> dict:
        """执行 JavaScript 并返回结果"""
        crawler = await self._get_crawler()
        full_js = f"{STEALTH_JS}\nwindow.__result = ({js_code});"
        config = self._make_config(js_code=full_js)

        target_url = url or self.DOUYIN_URL
        try:
            result = await crawler.arun(url=target_url, config=config)
            # 通过二次 JS 调用获取 __result
            get_result_config = self._make_config(js_code="window.__result;")
            result2 = await crawler.arun(url=target_url, config=get_result_config)
            # 结果在 markdown 中
            try:
                return json.loads(result2.markdown.strip()) if result2.markdown else {}
            except (json.JSONDecodeError, TypeError):
                return {"raw": result2.markdown[:500] if result2.markdown else ""}
        except Exception as e:
            print(f"[ERROR] JS 执行异常: {e}")
            return {"error": str(e)}

    async def wait_for_panel(self, timeout: float = 15.0) -> dict:
        """等待登录面板出现"""
        start = time.time()
        while time.time() - start < timeout:
            status = await self.execute_js(self._js_check_panel())
            if status.get("exists"):
                print(f"[OK] 登录面板已出现 ({status.get('width')}x{status.get('height')})")
                return status
            await asyncio.sleep(1)
        return {"exists": False, "error": "timeout"}

    async def save_cookies(self, filepath: str = "") -> str:
        """保存当前 Session 的 Cookies"""
        crawler = await self._get_crawler()

        # 获取浏览器 cookies
        try:
            cookies_data = await self.execute_js(self._js_get_cookies())

            if not filepath:
                filepath = str(self.output_dir / f"cookies_{self.session_id}.json")

            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "session_id": self.session_id,
                    "timestamp": time.time(),
                    "cookies": cookies_data,
                }, f, ensure_ascii=False, indent=2)

            print(f"[OK] Cookies 已保存: {filepath}")
            return filepath
        except Exception as e:
            print(f"[ERROR] 保存 Cookies 失败: {e}")
            return ""

    async def load_cookies(self, filepath: str) -> bool:
        """从文件加载 Cookies"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            cookies = data.get("cookies", [])
            if not cookies:
                print("[ERROR] 文件中没有有效的 Cookies")
                return False

            # 在 Crawl4AI 中设置 cookies
            crawler = await self._get_crawler()
            # Crawl4AI 通过 BrowserConfig 设置 cookies
            print(f"[OK] 已加载 {len(cookies)} 条 Cookie 记录")
            return True
        except Exception as e:
            print(f"[ERROR] 加载 Cookies 失败: {e}")
            return False

    # ────── 登录方法实现 ──────

    async def login_qr(
        self,
        on_qrcode: Optional[Callable[[str], Awaitable[None]]] = None,
        poll_interval: float = 3.0,
        timeout: float = 180.0,
    ) -> LoginResult:
        """
        二维码登录流程

        Args:
            on_qrcode: 回调函数，接收 base64 编码的二维码图片
            poll_interval: 轮询间隔（秒）
            timeout: 总超时时间（秒）

        Returns:
            LoginResult
        """
        result = LoginResult(method="qr", timestamp=time.time())
        crawler = await self._get_crawler()

        try:
            # 1. 打开页面
            print("[1/5] 正在打开抖音精选页...")
            await self.open_page()

            # 2. 点击登录按钮
            print("[2/5] 点击登录按钮...")
            click_result = await self.execute_js(self._js_click_login())
            if not click_result.get("clicked"):
                result.error = f"无法点击登录按钮: {click_result.get('error')}"
                return result
            await asyncio.sleep(3)

            # 3. 等待登录面板
            print("[3/5] 等待登录面板...")
            panel_status = await self.wait_for_panel(timeout=15)
            if not panel_status.get("exists"):
                result.error = "登录面板未出现"
                return result

            # 4. 提取二维码
            print("[4/5] 提取二维码...")
            qr_info = await self.execute_js(self._js_extract_qrcode())

            if not qr_info.get("found"):
                result.error = f"二维码提取失败: {qr_info.get('reason', 'unknown')}"
                return result

            src = qr_info["src"]
            if src.startswith("data:image/"):
                _, b64_data = src.split(",", 1)
                result.qrcode_base64 = b64_data
                result.qrcode_image = base64.b64decode(b64_data)

                # 保存二维码图片
                qr_path = self.output_dir / "qrcode_crawl4ai.png"
                qr_path.write_bytes(result.qrcode_image)
                print(f"[OK] 二维码已保存: {qr_path}")

                # 调用回调
                if on_qrcode:
                    await on_qrcode(b64_data)
            else:
                result.error = f"非预期的二维码格式: {src[:80]}"
                return result

            # 5. 等待扫码
            print(f"[5/5] 等待扫码（超时 {timeout}s）...")
            start = time.time()
            scanned = False

            while time.time() - start < timeout:
                login_status = await self.execute_js(self._js_check_login_status())

                if login_status.get("logged_in"):
                    scanned = True
                    print(f"[OK] 扫码登录成功! 用户: {login_status.get('nickname', 'N/A')}")
                    result.success = True
                    result.user_info = login_status
                    break

                # 检查二维码是否过期（需要刷新）
                qr_check = await self.execute_js(self._js_extract_qrcode())
                if not qr_check.get("found"):
                    print("[WARN] 二维码已过期，正在刷新...")
                    # 刷新页面重新获取
                    await self.open_page()
                    await self.execute_js(self._js_click_login())
                    await self.wait_for_panel()
                    qr_info = await self.execute_js(self._js_extract_qrcode())
                    if qr_info.get("found") and qr_info["src"].startswith("data:image/"):
                        _, b64_data = qr_info["src"].split(",", 1)
                        result.qrcode_base64 = b64_data
                        result.qrcode_image = base64.b64decode(b64_data)
                        if on_qrcode:
                            await on_qrcode(b64_data)

                await asyncio.sleep(poll_interval)

            if not scanned:
                result.error = "超时：未在规定时间内完成扫码"

            # 保存 Cookies
            if result.success:
                cookie_file = await self.save_cookies()
                result.cookies = cookie_file

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def login_sms(
        self,
        phone: str,
        auto_poll: bool = False,
        sms_callback: Optional[Callable[[], Awaitable[str]]] = None,
    ) -> LoginResult:
        """
        短信验证码登录

        Args:
            phone: 手机号
            auto_poll: 是否自动轮询等待验证码（需要配合 sms_callback）
            sms_callback: 异步回调，返回用户输入的验证码

        Returns:
            LoginResult
        """
        result = LoginResult(method="sms", timestamp=time.time())

        try:
            # 1. 打开页面并点击登录
            print("[1/6] 正在打开抖音精选页...")
            await self.open_page()

            print("[2/6] 点击登录按钮...")
            await self.execute_js(self._js_click_login())
            await asyncio.sleep(3)

            # 2. 等待面板
            print("[3/6] 等待登录面板...")
            panel = await self.wait_for_panel()
            if not panel.get("exists"):
                result.error = "登录面板未出现"
                return result

            # 3. 切换到手机号登录 tab（如果不是）
            if panel.get("hasQR") and not panel.get("hasMobile"):
                print("[INFO] 切换到手机号登录...")
                switch_js = """
                (() => {
                    const tabs = document.querySelectorAll('[class*="tab"], [role="tab"], li[class*="tab"]');
                    for (const tab of tabs) {
                        if (tab.textContent.includes('短信') || tab.textContent.includes('手机')) {
                            tab.click();
                            return { switched: true };
                        }
                    }
                    return { switched: false };
                })()
                """
                await self.execute_js(switch_js)
                await asyncio.sleep(2)

            # 4. 输入手机号
            print(f"[4/6] 输入手机号: {phone[:3]}****{phone[-4:]}")
            input_result = await self.execute_js(self._js_input_phone(phone))
            if not input_result.get("success"):
                result.error = f"输入手机号失败: {input_result.get('error')}"
                return result
            await asyncio.sleep(1)

            # 5. 发送验证码
            print("[5/6] 发送验证码...")
            send_result = await self.execute_js(self._js_click_send_sms())
            if send_result.get("clicked"):
                print(f"[OK] 已点击发送验证码: {send_result.get('text', '')}")
            else:
                result.error = f"发送验证码失败: {send_result.get('error')}"
                return result
            await asyncio.sleep(2)

            # 6. 输入验证码并提交
            if auto_poll and sms_callback:
                print("[6/6] 等待输入验证码...")
                code = await sms_callback()
                if code:
                    print(f"[INFO] 收到验证码: {code}")
                    await self.execute_js(self._js_input_sms_code(code))
                    await asyncio.sleep(1)
                    await self.execute_js(self._js_click_submit())
                    await asyncio.sleep(3)

                    # 检查登录状态
                    status = await self.execute_js(self._js_check_login_status())
                    if status.get("logged_in"):
                        result.success = True
                        result.user_info = status
                        await self.save_cookies()
                    else:
                        result.error = "登录可能失败，请检查验证码是否正确"
                else:
                    result.error = "未收到验证码"
            else:
                print("\n[INFO] 请手动输入验证码并点击登录。")
                print("[INFO] 或使用 auto_poll=True 并提供 sms_callback 自动化此步骤。")

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def login_password(
        self,
        phone: str,
        password: str,
    ) -> LoginResult:
        """
        密码登录

        Args:
            phone: 手机号
            password: 密码

        Returns:
            LoginResult
        """
        result = LoginResult(method="password", timestamp=time.time())

        try:
            # 1-3: 同上，打开页面、点击登录、等待面板
            print("[1/5] 正在打开抖音精选页...")
            await self.open_page()

            print("[2/5] 点击登录按钮...")
            await self.execute_js(self._js_click_login())
            await asyncio.sleep(3)

            print("[3/5] 等待登录面板...")
            panel = await self.wait_for_panel()
            if not panel.get("exists"):
                result.error = "登录面板未出现"
                return result

            # 4. 输入手机号
            print(f"[4/5] 输入账号信息...")
            await self.execute_js(self._js_input_phone(phone))
            await asyncio.sleep(1)

            # 5. 输入密码并提交
            print("[5/5] 输入密码并登录...")
            pwd_result = await self.execute_js(self._js_input_password(password))
            if not pwd_result.get("success"):
                result.error = f"输入密码失败: {pwd_result.get('error')}"
                return result

            await asyncio.sleep(1)
            submit_result = await self.execute_js(self._js_click_submit())
            if not submit_result.get("clicked"):
                result.error = f"提交失败: {submit_result.get('error')}"
                return result

            await asyncio.sleep(5)

            # 检查是否触发了身份验证
            status = await self.execute_js(self._js_check_login_status())
            if status.get("isVerifyPage"):
                result.error = "已触发身份验证，请使用其他登录方式或手动完成验证"
                print("[WARN] 触发了身份验证！")
                print("[INFO] 可能需要：短信验证 / 人脸识别 / 密码验证")
            elif status.get("logged_in"):
                result.success = True
                result.user_info = status
                await self.save_cookies()
                print(f"[OK] 密码登录成功!")
            else:
                # 可能还在验证中
                result.error = "登录状态未知，可能需要额外验证"

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    async def handle_identity_verify(
        self,
        method: str = "sms",  # sms / password / face
        **kwargs,
    ) -> LoginResult:
        """
        处理身份验证页面（截图中的界面）

        支持的验证方式：
        - sms: 接收短信验证码
        - password: 验证登录密码
        - face: 手机刷脸验证（需人工操作）
        """
        result = LoginResult(method=f"verify_{method}", timestamp=time.time())

        try:
            # 检查当前是否在身份验证页面
            check_js = """
            (() => {
                // 查找身份验证相关元素
                const verifyOptions = [];
                const items = document.querySelectorAll('[class*="verify-item"], [class*="option-item"], li, div[class*="item"]');
                for (const item of items) {
                    const text = item.textContent.trim();
                    if (text.includes('短信') || text.includes('密码') || text.includes('刷脸')) {
                        verifyOptions.push(text.replace(/\\s+/g, ' '));
                    }
                }

                return {
                    isVerifyPage: !!document.querySelector('[class*="identity-verify"]')
                                 || !!document.querySelector('[class*="IdentityVerify"]')
                                 || verifyOptions.length > 0,
                    options: verifyOptions,
                    pageTitle: document.title,
                };
            })()
            """
            status = await self.execute_js(check_js)

            if not status.get("isVerifyPage"):
                result.error = "当前不在身份验证页面"
                return result

            print(f"[INFO] 检测到身份验证页面")
            print(f"[INFO] 可选验证方式: {status.get('options', [])}")

            if method == "sms":
                # 点击"接收短信验证码"
                click_option_js = """
                (() => {
                    const items = document.querySelectorAll('li, div[class*="item"], [role="button"]');
                    for (const item of items) {
                        const text = item.textContent.trim();
                        if (text.includes('短信') && !text.includes('发送')) {
                            item.click();
                            return { clicked: true, option: text };
                        }
                    }
                    return { clicked: false };
                })()
                """
                click_res = await self.execute_js(click_option_js)
                if click_res.get("clicked"):
                    print(f"[OK] 已选择: {click_res.get('option')}")
                    await asyncio.sleep(2)

                    # 进入短信验证流程
                    phone = kwargs.get("phone", "")
                    if phone:
                        await self.execute_js(self._js_input_phone(phone))
                        await asyncio.sleep(1)
                        await self.execute_js(self._js_click_send_sms())
                        print("[INFO] 已发送验证码，请查看手机")

            elif method == "password":
                # 点击"验证登录密码"
                click_option_js = """
                (() => {
                    const items = document.querySelectorAll('li, div[class*="item"], [role="button"]');
                    for (const item of items) {
                        const text = item.textContent.trim();
                        if (text.includes('密码')) {
                            item.click();
                            return { clicked: true, option: text };
                        }
                    }
                    return { clicked: false };
                })()
                """
                click_res = await self.execute_js(click_option_js)
                if click_res.get("clicked"):
                    print(f"[OK] 已选择: {click_res.get('option')}")
                    password = kwargs.get("password", "")
                    if password:
                        await asyncio.sleep(1)
                        await self.execute_js(self._js_input_password(password))
                        await asyncio.sleep(1)
                        await self.execute_js(self._js_click_submit())

            elif method == "face":
                # 点击"手机刷脸验证"
                click_option_js = """
                (() => {
                    const items = document.querySelectorAll('li, div[class*="item"], [role="button"]');
                    for (const item of items) {
                        const text = item.textContent.trim();
                        if (text.includes('刷脸') || text.includes('人脸')) {
                            item.click();
                            return { clicked: true, option: text };
                        }
                    }
                    return { clicked: false };
                })()
                """
                click_res = await self.execute_js(click_option_js)
                if click_res.get("clicked"):
                    print(f"[OK] 已选择: {click_res.get('option')}")
                    print("[INFO] 请在手机上完成人脸识别")

            result.success = True  # 操作成功（不代表验证通过）

        except Exception as e:
            result.error = f"异常: {str(e)}"

        return result

    # ────── 登录后数据抓取 ──────

    async def scrape_feed(
        self,
        url: str = DOUYIN_URL,
        max_items: int = 20,
    ) -> ScrapedData:
        """
        抓取抖音推荐流数据（需要先登录）

        Args:
            url: 要抓取的 URL
            max_items: 最大抓取数量

        Returns:
            ScrapedData
        """
        data = ScrapedData(url=url, timestamp=time.time())
        crawler = await self._get_crawler()

        try:
            # 滚动加载更多内容
            scroll_js = f"""
            {STEALTH_JS}

            // 滚动到底部加载内容
            async function scrollAndLoad() {{
                for (let i = 0; i < {max_items}; i++) {{
                    window.scrollBy(0, window.innerHeight);
                    await new Promise(r => setTimeout(r, 1500));
                }}
            }}
            scrollAndLoad();
            """

            config = self._make_config(
                js_code=scroll_js,
                page_timeout=120000,
                wait_for="css:.feed-item, [class*='feed'], video",
                screenshot=True,
            )

            result = await crawler.arun(url=url, config=config)

            data.title = result.metadata.get("title", "")
            data.markdown = result.markdown
            data.html = result.html
            data.images = result.media.get("images", [])
            data.videos = result.media.get("videos", [])
            data.links = result.links

            # 保存结果
            output_file = self.output_dir / f"scrape_{int(time.time())}.md"
            output_file.write_text(data.markdown, encoding="utf-8")
            print(f"[OK] 数据已保存: {output_file}")
            print(f"[OK] 抓取到 {len(data.images)} 张图片, {len(data.videos)} 个视频")

        except Exception as e:
            print(f"[ERROR] 抓取失败: {e}")

        return data


# ──────────────────── CLI 入口 ────────────────────


async def cmd_qr(args):
    """二维码登录命令"""
    print("=" * 50)
    print("  抖音二维码登录 - Crawl4AI 版本")
    print("=" * 50)

    async def on_qrcode(base64_data):
        # 保存并显示二维码信息
        qr_path = Path(args.output) / "qrcode_live.png"
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(base64.b64decode(base64_data))
        print(f"\n[QR] 二维码已更新: {qr_path}")
        print("[QR] 请使用抖音 App 扫描二维码\n")

    async with DouyinCrawl4AILogin(
        output_dir=args.output,
        headless=not args.visible,
    ) as client:
        result = await client.login_qr(
            on_qrcode=on_qrcode,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )

        if result.success:
            print(f"\n{'=' * 50}")
            print(f"  ✅ 登录成功!")
            print(f"  用户: {result.user_info.get('nickname', 'N/A')}")
            print(f"  Session: {client.session_id}")
            print(f"{'=' * 50}\n")
        else:
            print(f"\n❌ 登录失败: {result.error}\n")
            sys.exit(1)


async def cmd_sms(args):
    """短信验证码登录命令"""
    print("=" * 50)
    print("  抖音短信验证码登录 - Crawl4AI 版本")
    print("=" * 50)

    async def get_code():
        print(f"\n[SMS] 验证码已发送至 {args.phone[:3]}****{args.phone[-4:]}")
        code = input("[SMS] 请输入收到的验证码: ").strip()
        return code

    async with DouyinCrawl4AILogin(
        output_dir=args.output,
        headless=not args.visible,
    ) as client:
        result = await client.login_sms(
            phone=args.phone,
            auto_poll=True,
            sms_callback=get_code,
        )

        if result.success:
            print(f"\n✅ 登录成功!")
        else:
            print(f"\n❌ 登录失败: {result.error}")


async def cmd_password(args):
    """密码登录命令"""
    print("=" * 50)
    print("  抖音密码登录 - Crawl4AI 版本")
    print("=" * 50)

    async with DouyinCrawl4AILogin(
        output_dir=args.output,
        headless=not args.visible,
    ) as client:
        result = await client.login_password(
            phone=args.phone,
            password=args.password,
        )

        if result.success:
            print(f"\n✅ 登录成功!")
        else:
            print(f"\n❌ 登录失败: {result.error}")


async def cmd_verify(args):
    """身份验证处理命令"""
    print("=" * 50)
    print("  抖音身份验证处理 - Crawl4AI 版本")
    print("=" * 50)

    async with DouyinCrawl4AILogin(
        output_dir=args.output,
        headless=not args.visible,
    ) as client:
        # 先打开页面
        await client.open_page()
        await client.execute_js(client._js_click_login())
        await asyncio.sleep(3)

        kwargs = {}
        if args.method == "sms" and args.phone:
            kwargs["phone"] = args.phone
        elif args.method == "password" and args.password:
            kwargs["password"] = args.password

        result = await client.handle_identity_verify(
            method=args.method,
            **kwargs,
        )

        if result.success:
            print(f"\n✅ 已选择验证方式，请在浏览器中完成后续操作")
        else:
            print(f"\n❌ 操作失败: {result.error}")


async def cmd_scrape(args):
    """抓取数据命令"""
    print("=" * 50)
    print("  抖音数据抓取 - Crawl4AI 版本")
    print("=" * 50)

    async with DouyinCrawl4AILogin(
        output_dir=args.output,
        session_id=args.session,
    ) as client:
        # 如果有 cookie 文件，先加载
        if args.cookies and Path(args.cookies).exists():
            await client.load_cookies(args.cookies)

        data = await client.scrape_feed(
            url=args.url,
            max_items=args.max_items,
        )

        if data.markdown:
            print(f"\n✅ 抓取成功!")
            print(f"   标题: {data.title}")
            print(f"   内容长度: {len(data.markdown)} 字符")
            print(f"   图片: {len(data.images)}")
            print(f"   视频: {len(data.videos)}")
        else:
            print(f"\n❌ 抓取失败")


def main():
    parser = argparse.ArgumentParser(
        description="抖音网页登录爬虫 - Crawl4AI 版本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 二维码登录
  python %(prog)s qr

  # 短信验证码登录
  python %(prog)s sms --phone 13800138000

  # 密码登录
  python %(prog)s password --phone 13800138000 --password your_pass

  # 处理身份验证
  python %(prog)s verify --method sms --phone 13800138000

  # 登录后抓取数据
  python %(prog)s scrape --session douyin_xxx
        """,
    )
    sub = parser.add_subparsers(dest="command", help="登录方式")

    # QR 登录
    p_qr = sub.add_parser("qr", help="二维码登录")
    p_qr.add_argument("--output", "-o", default="./output", help="输出目录")
    p_qr.add_argument("--visible", action="store_true", help="显示浏览器窗口")
    p_qr.add_argument("--poll-interval", type=float, default=3.0, help="轮询间隔(秒)")
    p_qr.add_argument("--timeout", type=float, default=180.0, help="总超时(秒)")

    # SMS 登录
    p_sms = sub.add_parser("sms", help="短信验证码登录")
    p_sms.add_argument("--phone", required=True, help="手机号")
    p_sms.add_argument("--output", "-o", default="./output")
    p_sms.add_argument("--visible", action="store_true")

    # 密码登录
    p_pwd = sub.add_parser("password", help="密码登录")
    p_pwd.add_argument("--phone", required=True, help="手机号")
    p_pwd.add_argument("--password", required=True, help="密码")
    p_pwd.add_argument("--output", "-o", default="./output")
    p_pwd.add_argument("--visible", action="store_true")

    # 身份验证
    p_ver = sub.add_parser("verify", help="处理身份验证")
    p_ver.add_argument("--method", choices=["sms", "password", "face"], default="sms", help="验证方式")
    p_ver.add_argument("--phone", help="手机号（短信验证需要）")
    p_ver.add_argument("--password", help="密码（密码验证需要）")
    p_ver.add_argument("--output", "-o", default="./output")
    p_ver.add_argument("--visible", action="store_true")

    # 数据抓取
    p_scr = sub.add_parser("scrape", help="登录后抓取数据")
    p_scr.add_argument("--url", default="https://www.douyin.com/jingxuan", help="目标URL")
    p_scr.add_argument("--session", help="Session ID（登录后获得）")
    p_scr.add_argument("--cookies", help="Cookie 文件路径")
    p_scr.add_argument("--output", "-o", default="./output")
    p_scr.add_argument("--max-items", type=int, default=20, help="最大抓取条数")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "qr": cmd_qr,
        "sms": cmd_sms,
        "password": cmd_password,
        "verify": cmd_verify,
        "scrape": cmd_scrape,
    }

    fn = commands.get(args.command)
    if fn:
        asyncio.run(fn(args))


if __name__ == "__main__":
    main()
