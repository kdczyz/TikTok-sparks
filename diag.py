#!/usr/bin/env python3
"""诊断：为什么登录面板没出现"""
import asyncio
from playwright.async_api import async_playwright

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
window.chrome = { runtime: {} };
"""

CHECK_JS = """() => {
    const panel = document.querySelector('#login-panel-new');
    const scan = document.querySelector('#douyin_login_comp_scan_code');
    const btns = [...document.querySelectorAll('button')].map(b => b.textContent.trim()).filter(t => t && t.length < 20);
    return {
        url: location.href,
        title: document.title,
        hasPanel: !!panel,
        panelVisible: panel ? panel.getBoundingClientRect().width > 0 : false,
        hasScan: !!scan,
        buttons: btns.slice(0, 30),
    };
}"""


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
            locale="zh-CN",
        )
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/jingxuan",
                            wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[goto 异常] {type(e).__name__}: {e}")
        await page.wait_for_timeout(5000)

        info = await page.evaluate(CHECK_JS)
        print("[打开页面后]")
        for k, v in info.items():
            print(f"  {k}: {v}")
        await page.screenshot(path="./output/diag_1_loaded.png")

        # 尝试点击登录
        try:
            btn = page.locator('button:has-text("登录")').first
            cnt = await btn.count()
            vis = await btn.is_visible() if cnt else False
            print(f"\n[登录按钮] count={cnt} visible={vis}")
            if cnt and vis:
                await btn.click(force=True, timeout=5000)
                print("[已点击]")
        except Exception as e:
            print(f"[点击异常] {e}")
        await page.wait_for_timeout(3000)

        info2 = await page.evaluate(CHECK_JS)
        print("\n[点击登录后]")
        for k, v in info2.items():
            print(f"  {k}: {v}")
        await page.screenshot(path="./output/diag_2_after_click.png")
        await browser.close()


asyncio.run(main())
