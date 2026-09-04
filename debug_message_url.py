#!/usr/bin/env python3
"""测试直接访问消息页面 URL"""
import asyncio, sys, json
from pathlib import Path

PROJ = Path("/Users/a1412/Desktop/火花/douyin_qr_login")
sys.path.insert(0, str(PROJ))
import dm_scraper as D

async def run(url="https://www.douyin.com/message"):
    ck = D.find_latest_session()
    pw, browser, ctx = await D.start_browser(headless=True)
    await D.load_cookies(ctx, ck)
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(6000)
    print("URL =", page.url)
    print("TITLE =", await page.title())
    probe = await page.evaluate(D.PANEL_PROBE_JS)
    print("PROBE =", json.dumps(probe, ensure_ascii=False))
    # 探测会话列表（左右两种布局）
    rows = await page.evaluate(D.LIST_ROWS_JS)
    print("ROWS =", json.dumps(rows[:5], ensure_ascii=False))
    await page.screenshot(path=str(PROJ/"output"/"dbg_message_url.png"), full_page=False)
    await browser.close(); await pw.stop()

asyncio.run(run())
