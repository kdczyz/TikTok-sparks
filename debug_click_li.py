#!/usr/bin/env python3
"""尝试用 Playwright locator 点击消息 LI"""
import asyncio, sys, json
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
    print("URL =", page.url)
    # 使用 Playwright 的 get_by_text / locator 点击
    try:
        li = page.locator("li.yOm_yUgK").first
        cnt = await li.count()
        print("LI_COUNT =", cnt)
        if cnt:
            box = await li.bounding_box()
            print("LI_BOX =", box)
            await li.click(timeout=5000)
            print("CLICK_OK")
    except Exception as e:
        print("LI_CLICK_FAIL =", e)
    await page.wait_for_timeout(6000)
    print("URL_AFTER =", page.url)
    print("PROBE =", json.dumps(await page.evaluate(D.PANEL_PROBE_JS), ensure_ascii=False))
    # 检查是否打开了 iframe / 新元素
    frames = [f.url for f in page.frames]
    print("FRAMES =", frames)
    await page.screenshot(path=str(PROJ/"output"/"dbg_click_li.png"), full_page=False)
    await browser.close(); await pw.stop()

asyncio.run(run())
