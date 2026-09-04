#!/usr/bin/env python3
"""验证：两个 always 任务在同一个循环里依次打开各自聊天并到达发送阶段（dry-run），
证明多任务「连在一起」不会互相污染/串台。"""
import asyncio, sys, json
from pathlib import Path
PROJ = Path("/Users/a1412/Desktop/火花/douyin_qr_login")
sys.path.insert(0, str(PROJ))
import dm_scraper as D
import auto_replier as R

TEST_CFG = {
    "check_interval_min": 10,
    "rules": [
        {"name": "1412", "reply": "你好~", "active": True, "trigger": "always",
         "active_hours": "00:00-24:00", "min_gap_min": 0, "max_per_day": 99},
        {"name": "吝邱桦", "reply": "收到", "active": True, "trigger": "always",
         "active_hours": "00:00-24:00", "min_gap_min": 0, "max_per_day": 99},
    ],
}

async def run():
    pw, browser, ctx = await D.start_browser(headless=True)
    await D.load_cookies(ctx, D.find_latest_session())
    page = await ctx.new_page()
    try:
        await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        if not D.has_login_cookie(await ctx.cookies()):
            print("LOGIN_EXPIRED"); return
        if not await D.open_message_panel(page):
            print("PANEL_FAIL"); return
        await page.evaluate(D.REMOVE_MASK_JS)
        await page.wait_for_timeout(1500)
        st = {}
        for rnd in range(2):
            print(f"\n===== 第 {rnd+1} 轮（两任务都应到达发送阶段）=====")
            results = await R.run_round(page, TEST_CFG, st, True)
            print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await browser.close(); await pw.stop()

asyncio.run(run())
