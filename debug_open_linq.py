#!/usr/bin/env python3
"""诊断：单独打开 吝邱桦 会话，打印原始抽取结果 + 右半屏抽屉 innerText，定位“会话为空”根因。"""
import asyncio, sys, json
from pathlib import Path
PROJ = Path("/Users/a1412/Desktop/火花/douyin_qr_login")
sys.path.insert(0, str(PROJ))
import dm_scraper as D
import auto_replier as R

DUMP_JS = """
() => {
  // 取右半屏（聊天窗）整体文本与结构概览
  const out = [];
  const drawers = [];
  for (const el of document.querySelectorAll('div')) {
    const r = el.getBoundingClientRect();
    if (r.x > window.innerWidth * 0.5 && r.width > 200 && r.height > 200) {
      drawers.push(el);
    }
  }
  // 取最大的那个（聊天主区）
  drawers.sort((a,b)=> b.getBoundingClientRect().height - a.getBoundingClientRect().height);
  const main = drawers[0];
  if (!main) return {err: 'no_right_drawer'};
  const r = main.getBoundingClientRect();
  const texts = [];
  const w = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const t = (w.currentNode.textContent||'').trim();
    if (t && t.length <= 200) texts.push(t);
  }
  return {
    drawer_rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
    leaf_text_count: texts.length,
    sample_texts: texts[:40],
  };
}
"""

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

        ok = await R.open_conversation(page, "吝邱桦")
        print("open_conversation 返回:", ok)
        await page.wait_for_timeout(3000)  # 给消息加载时间
        print("IN_CHAT:", await page.evaluate(D.IN_CHAT_JS))
        print("\n--- EXTRACT_CHAT_JS 原始 ---")
        ex = await page.evaluate(D.EXTRACT_CHAT_JS)
        print(json.dumps(ex, ensure_ascii=False, indent=2)[:2000])
        print("\n--- 右半屏抽屉概览 ---")
        dump = await page.evaluate(DUMP_JS)
        print(json.dumps(dump, ensure_ascii=False, indent=2)[:2000])
        # 标题（软校验）
        print("\n--- CHAT_TITLE_JS ---")
        print(await page.evaluate(D.CHAT_TITLE_JS))
    finally:
        await browser.close(); await pw.stop()

asyncio.run(run())
