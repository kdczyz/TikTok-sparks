#!/usr/bin/env python3
"""验证修复后的 open_message_panel 能打开面板"""
import asyncio, sys
from pathlib import Path
PROJ = Path("/Users/a1412/Desktop/火花/douyin_qr_login")
sys.path.insert(0, str(PROJ))
import dm_scraper as D

async def run():
    ck = D.find_latest_session()
    pw, browser, ctx = await D.start_browser(headless=True)
    await D.load_cookies(ctx, ck)
    page = await ctx.new_page()
    await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)
    await page.evaluate(D.REMOVE_MASK_JS)
    ok = await D.open_message_panel(page)
    print("OPEN_RESULT =", ok)
    await page.screenshot(path=str(PROJ/"output"/"verify_panel.png"), full_page=False)
    await browser.close(); await pw.stop()

asyncio.run(run())
