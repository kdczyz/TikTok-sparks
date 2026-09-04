#!/usr/bin/env python3
import asyncio, sys, json
from pathlib import Path
PROJ = Path("/Users/a1412/Desktop/火花/douyin_qr_login")
sys.path.insert(0, str(PROJ))
import dm_scraper as D
import auto_replier as R

async def run():
    ck = D.find_latest_session()
    pw, browser, ctx = await D.start_browser(headless=True)
    await D.load_cookies(ctx, ck)
    page = await ctx.new_page()
    await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)
    await D.open_message_panel(page)
    await page.evaluate(D.REMOVE_MASK_JS)
    await page.wait_for_timeout(2000)

    # 1) 打开 1412（always）→ 回列表
    r = await D.find_conversation_row(page, "1412")
    await page.mouse.click(r["x"], r["y"])
    for _ in range(10):
        await page.wait_for_timeout(1000)
        if await page.evaluate(D.IN_CHAT_JS): break
    print("1412 IN_CHAT =", await page.evaluate(D.IN_CHAT_JS))
    back = await page.evaluate(D.BACK_ARROW_JS)
    if back: await page.mouse.click(back["x"], back["y"]); await page.wait_for_timeout(1500)
    print("after 1412 back IN_CHAT =", await page.evaluate(D.IN_CHAT_JS))

    # 2) Vsover 多次未命中（find_conversation_row 会把列表滚到底）
    for i in range(3):
        rr = await D.find_conversation_row(page, "Vsover")
        print(f"Vsover find#{i} =", rr)

    # 3) 用真实的 process_rule 流程：Vsover 失败后 ensure_list_view
    ok_lv = await R.ensure_list_view(page)
    print("Vsover后 ensure_list_view =", ok_lv)
    if not ok_lv:
        await D.open_message_panel(page)
        await page.evaluate(D.REMOVE_MASK_JS)
        ok_lv = await R.ensure_list_view(page)
        print("重开后 ensure_list_view =", ok_lv)
    # 再找并点击 吝邱桦
    rl = await D.find_conversation_row(page, "吝邱桦")
    print("吝邱桦_ROW =", rl)
    if rl:
        await page.mouse.click(rl["x"], rl["y"])
        for _ in range(12):
            await page.wait_for_timeout(1000)
            if await page.evaluate(D.IN_CHAT_JS): break
        print("吝邱桦 IN_CHAT =", await page.evaluate(D.IN_CHAT_JS),
              "TITLE =", await page.evaluate(D.CHAT_TITLE_JS))
    await browser.close(); await pw.stop()

asyncio.run(run())
