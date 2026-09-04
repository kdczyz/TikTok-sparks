#!/usr/bin/env python3
"""一次性：给好友「1412」发送一条测试消息（复用 dm_scraper + auto_replier 链路）"""
import asyncio
import json
import sys
from pathlib import Path

PROJ = Path("/Users/a1412/Desktop/火花/douyin_qr_login")
sys.path.insert(0, str(PROJ))
import dm_scraper as D
import auto_replier as AR

TARGET = "1412"
MSG = "【测试】这是一条测试消息，收到请忽略~"


async def run():
    ck = D.find_latest_session()
    if not ck:
        print("[ERROR] 未找到 session cookie 文件")
        return
    print(f"[OK] 使用 cookie: {ck}")

    pw, browser, ctx = await D.start_browser(headless=True)
    n = await D.load_cookies(ctx, ck)
    print(f"[OK] 已加载 {n} 条 Cookie")
    page = await ctx.new_page()
    try:
        await page.goto("https://www.douyin.com/jingxuan",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        if not D.has_login_cookie(await ctx.cookies()):
            print("[ERROR] 登录态失效，请重新扫码")
            return
        print("[OK] 登录态有效")

        # 打开消息面板：点击顶部导航的「消息」图标
        await page.evaluate(D.REMOVE_MASK_JS)
        await page.wait_for_timeout(2000)
        try:
            # 优先用 class 匹配当前顶部栏图标
            msg_btn = page.locator("li.yOm_yUgK").first
            if await msg_btn.count() == 0:
                # fallback：包含“消息”文字的可见 li
                msg_btn = page.locator("li:has-text('消息')").first
            await msg_btn.click(timeout=5000)
            await page.wait_for_timeout(2500)
            print("[OK] 已点击消息图标")
        except Exception as e:
            print(f"[ERROR] 点击消息图标失败: {e}")
            await page.screenshot(path=str(PROJ / "output" / "test_send_fail.png"))
            return
        await page.evaluate(D.REMOVE_MASK_JS)
        await page.wait_for_timeout(1500)

        row = await D.find_conversation_row(page, TARGET)
        if not row:
            print(f"[ERROR] 会话列表中未找到「{TARGET}」")
            await page.screenshot(path=str(PROJ / "output" / "test_send_norow.png"))
            return

        sent, msg = False, "聊天窗未能打开"
        for attempt in range(3):
            await page.evaluate(D.REMOVE_MASK_JS)
            if attempt > 0:
                row = await D.find_conversation_row(page, TARGET)
                if not row:
                    break
            await page.mouse.click(row["x"], row["y"])
            data = {"found": False, "msgs": []}
            for _ in range(10):
                await page.wait_for_timeout(1000)
                data = await page.evaluate(D.EXTRACT_CHAT_JS)
                if data.get("found"):
                    break
            if data.get("found"):
                sent, msg = await AR.send_reply(page, MSG)
                # 验证：直接在当前聊天窗读取消息，不返回列表
                await page.wait_for_timeout(2000)
                verify = await page.evaluate(D.EXTRACT_CHAT_JS)
                (PROJ / "output" / "test_send_verify.json").write_text(
                    json.dumps(verify, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                await page.screenshot(path=str(PROJ / "output" / "test_send_ok.png"), full_page=False)
                # 返回列表，便于下次运行
                back = await page.evaluate(D.BACK_ARROW_JS)
                if back:
                    await page.mouse.click(back["x"], back["y"])
                break

        if sent:
            print(f"[OK] 发送结果: {msg}（给 {TARGET}，内容: {MSG!r}）")
            await page.wait_for_timeout(2000)
            await page.screenshot(path=str(PROJ / "output" / "test_send_ok.png"), full_page=False)
            chat_data = await page.evaluate(D.EXTRACT_CHAT_JS)
            (PROJ / "output" / "test_send_chat.json").write_text(
                json.dumps(chat_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            print(f"[FAIL] {msg}")
            await page.screenshot(path=str(PROJ / "output" / "test_send_fail.png"))
    finally:
        await browser.close()
        await pw.stop()


asyncio.run(run())
