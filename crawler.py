#!/usr/bin/env python3
"""抖音登录二维码自动抓取器 - 核心模块"""
import asyncio
import base64
import json
import os
import time
from pathlib import Path
from dataclasses import dataclass, field
from playwright.async_api import async_playwright, Page, BrowserContext

# ── 反检测 JS ──
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
window.chrome = { runtime: {} };
"""

# ── 二维码 JS 提取 ──
EXTRACT_QR_JS = """() => {
    const scanCode = document.querySelector('#douyin_login_comp_scan_code');
    if (!scanCode) return { found: false, reason: 'no_scan_code_element' };
    const img = scanCode.querySelector('img');
    if (!img) return { found: false, reason: 'no_img_in_scan_code' };
    return {
        found: true,
        src: img.src,
        naturalWidth: img.naturalWidth,
        naturalHeight: img.naturalHeight,
        cssWidth: img.getBoundingClientRect().width,
        cssHeight: img.getBoundingClientRect().height,
    };
}"""

# ── 登录面板状态检查 JS ──
CHECK_PANEL_JS = """() => {
    const panel = document.querySelector('#login-panel-new');
    if (!panel) return { exists: false };
    const rect = panel.getBoundingClientRect();
    return {
        exists: true,
        visible: rect.width > 0 && rect.height > 0,
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        title: (document.querySelector('#douyin_login_comp_flat_panel_title') || {}).innerText || '',
        hasScanTab: !!panel.querySelector('#douyin_login_comp_scan_code'),
        hasMobileTab: !!panel.querySelector('#douyin_login_comp_mobile_code'),
    };
}"""


@dataclass
class QRCodeResult:
    """二维码抓取结果"""
    success: bool
    image_bytes: bytes = b""
    image_base64: str = ""
    natural_size: tuple = (0, 0)
    css_size: tuple = (0, 0)
    timestamp: float = 0.0
    error: str = ""
    panel_screenshot: bytes = b""
    scan_area_screenshot: bytes = b""
    save_path: str = ""

    def save(self, output_dir: str = ".") -> dict:
        """保存到文件，返回文件路径字典"""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = int(self.timestamp)
        paths = {}
        if self.image_bytes:
            p = out / f"qrcode_{ts}.png"
            p.write_bytes(self.image_bytes)
            paths["qrcode"] = str(p)
            # 也写一份固定名方便引用
            latest = out / "qrcode_latest.png"
            latest.write_bytes(self.image_bytes)
            paths["latest"] = str(latest)
        if self.panel_screenshot:
            p = out / f"login_panel_{ts}.png"
            p.write_bytes(self.panel_screenshot)
            paths["panel"] = str(p)
        if self.scan_area_screenshot:
            p = out / f"scan_area_{ts}.png"
            p.write_bytes(self.scan_area_screenshot)
            paths["scan_area"] = str(p)
        return paths


class DouyinQRLogin:
    """抖音二维码登录抓取器"""

    def __init__(self, output_dir: str = "./output", headless: bool = True):
        self.output_dir = output_dir
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context: BrowserContext = None
        self._page: Page = None

    async def start(self) -> None:
        """启动浏览器"""
        self._playwright = await async_playwright().start()
        # 自动读取系统/沙箱 HTTP 代理（如校园网 OpenClash fake-ip 环境），
        # Chromium 默认不读 HTTP_PROXY 环境变量，需显式通过 --proxy-server 传入。
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ]
        if proxy:
            launch_args.append(f"--proxy-server={proxy}")
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=launch_args,
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
            # fake-ip 代理常做 TLS 拦截，忽略自签证书错误以保证资源加载
            ignore_https_errors=True,
        )
        await self._context.add_init_script(STEALTH_JS)
        self._page = await self._context.new_page()

    async def stop(self) -> None:
        """关闭浏览器"""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    async def _open_page(self, url: str = "https://www.douyin.com/jingxuan") -> None:
        """打开抖音页面"""
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass  # 抖音经常超时但内容已加载
        await self._page.wait_for_timeout(5000)

    async def _click_login(self) -> bool:
        """点击登录按钮"""
        selectors = [
            'button:has-text("登录")',
            '.semi-button-primary:has-text("登录")',
            '#NYVFZNTd button',
        ]
        for sel in selectors:
            try:
                btn = self._page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(force=True, timeout=5000)
                    return True
            except Exception:
                continue
        # fallback: JS click
        try:
            await self._page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.includes('登录')) { b.click(); return true; }
                }
                return false;
            }""")
            return True
        except Exception:
            return False

    async def _wait_for_panel(self, timeout: float = 10.0) -> bool:
        """等待登录面板出现"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                info = await self._page.evaluate(CHECK_PANEL_JS)
                if info.get("exists") and info.get("hasScanTab"):
                    return True
            except Exception:
                pass
            await self._page.wait_for_timeout(500)
        return False

    async def _wait_for_qr_img(self, timeout: float = 15.0) -> bool:
        """等待二维码 <img> 元素出现并加载完成"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                data = await self._page.evaluate("""() => {
                    const img = document.querySelector('#douyin_login_comp_scan_code img');
                    if (!img) return null;
                    return {
                        src: img.src,
                        complete: img.complete,
                        naturalWidth: img.naturalWidth,
                    };
                }""")
                if data and data.get("src") and data.get("naturalWidth", 0) > 0:
                    return True
            except Exception:
                pass
            await self._page.wait_for_timeout(800)
        return False

    async def _extract_qr_image(self) -> dict:
        """提取二维码 base64 图片"""
        return await self._page.evaluate(EXTRACT_QR_JS)

    async def _capture_screenshots(self) -> tuple:
        """截取登录面板和扫码区域截图"""
        panel_bytes = b""
        scan_bytes = b""
        try:
            panel = await self._page.query_selector('#login-panel-new')
            if panel:
                panel_bytes = await panel.screenshot()
        except Exception:
            pass
        try:
            scan = await self._page.query_selector('#douyin_login_comp_scan_code')
            if scan:
                scan_bytes = await scan.screenshot()
        except Exception:
            pass
        return panel_bytes, scan_bytes

    async def fetch_qrcode(
        self,
        url: str = "https://www.douyin.com/jingxuan",
        save: bool = True,
    ) -> QRCodeResult:
        """
        完整流程：打开页面 → 点击登录 → 等待面板 → 提取二维码
        """
        result = QRCodeResult(success=False, timestamp=time.time())

        try:
            # 1. 打开页面
            await self._open_page(url)

            # 2. 点击登录
            clicked = await self._click_login()
            if not clicked:
                result.error = "无法找到或点击登录按钮"
                return result

            await self._page.wait_for_timeout(3000)

            # 3. 等待面板
            panel_ready = await self._wait_for_panel(timeout=15.0)
            if not panel_ready:
                result.error = "登录面板未出现"
                return result

            # 3.5 等待二维码图片加载
            qr_ready = await self._wait_for_qr_img(timeout=15.0)
            if not qr_ready:
                result.error = "二维码图片未加载"
                return result

            # 4. 提取二维码
            qr_info = await self._extract_qr_image()
            if not qr_info.get("found"):
                result.error = f"二维码未找到: {qr_info.get('reason', 'unknown')}"
                return result

            src = qr_info["src"]
            if not src.startswith("data:image/"):
                result.error = f"非预期的图片格式: {src[:80]}"
                return result

            header, b64_data = src.split(",", 1)
            img_bytes = base64.b64decode(b64_data)

            result.success = True
            result.image_bytes = img_bytes
            result.image_base64 = b64_data
            result.natural_size = (qr_info["naturalWidth"], qr_info["naturalHeight"])
            result.css_size = (qr_info["cssWidth"], qr_info["cssHeight"])

            # 5. 截图
            panel_bytes, scan_bytes = await self._capture_screenshots()
            result.panel_screenshot = panel_bytes
            result.scan_area_screenshot = scan_bytes

            # 6. 保存
            if save:
                paths = result.save(self.output_dir)
                result.save_path = paths.get("latest", "")

        except Exception as e:
            result.error = str(e)

        return result

    async def fetch_qrcode_loop(
        self,
        interval: float = 30.0,
        url: str = "https://www.douyin.com/jingxuan",
        on_result=None,
    ):
        """
        循环抓取二维码（二维码有效期约 3-5 分钟）
        on_result: async callback(QRCodeResult) -> bool (返回 False 退出循环)
        """
        while True:
            result = await self.fetch_qrcode(url=url, save=True)
            if on_result:
                should_continue = await on_result(result)
                if not should_continue:
                    break
            if not result.success:
                print(f"[WARN] 抓取失败: {result.error}, {interval}s 后重试...")
            else:
                print(f"[OK] 二维码已更新 ({result.natural_size[0]}x{result.natural_size[1]})")
            await asyncio.sleep(interval)
