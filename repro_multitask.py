#!/usr/bin/env python3
"""复现多任务链式处理问题（dry-run，不真正发送）

只保留真实存在的两个会话，避免“不存在的好友”把列表滚到底、污染后续查找：
  - 1412      (trigger=always)        验证 always 模式
  - 吝邱桦    (trigger=new_message)   验证“对方新消息”抽取（此前在链式里报“会话为空”）
"""
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
        {"name": "吝邱桦", "reply": "收到", "active": True, "trigger": "new_message",
         "active_hours": "00:00-24:00", "min_gap_min": 0, "max_per_day": 99},
    ],
}


async def run():
    ck = D.find_latest_session()
    pw, browser, ctx = await D.start_browser(headless=True)
    await D.load_cookies(ctx, ck)
    page = await ctx.new_page()
    try:
        await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        if not D.has_login_cookie(await ctx.cookies()):
            print("LOGIN_EXPIRED"); return
        print("[OK] 登录态有效")
        if not await D.open_message_panel(page):
            print("PANEL_FAIL"); return
        await page.evaluate(D.REMOVE_MASK_JS)
        await page.wait_for_timeout(1500)
        st = {}
        # 跑两轮，验证任务之间是否干净隔离（第二轮不应再因残留状态失败）
        for rnd in range(2):
            print(f"\n===== 第 {rnd+1} 轮 =====")
            results = await R.run_round(page, TEST_CFG, st, True)
            print("RESULTS:")
            print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await browser.close(); await pw.stop()

asyncio.run(run())
