#!/usr/bin/env python3
"""
抖音私信自动化回复系统 — 按配置对指定好友自动回复

配置: replies_config.json（好友名字 / 回复内容 / 回复时间窗口 / 回复周期）
状态: output/auto_reply_state.json（每人上次回复时间/已回复条数）
日志: output/auto_reply_log.json（发送记录）

用法:
    python auto_replier.py --once --dry-run   # 单轮演练：只检测和演示，不真正发送
    python auto_replier.py --once             # 单轮真实回复
    python auto_replier.py                    # 守护循环（按 check_interval_min 周期运行）
"""
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJ = Path(__file__).resolve().parent
OUT = PROJ / "output"
OUT.mkdir(exist_ok=True)
CONFIG = PROJ / "replies_config.json"
STATE_F = OUT / "auto_reply_state.json"
LOG_F = OUT / "auto_reply_log.json"
sys.path.insert(0, str(PROJ))
import dm_scraper as D  # 复用登录/面板/行定位/聊天抽取

DEFAULT_CONFIG = {
    "check_interval_min": 10,
    "rules": [
        {
            "name": "示例好友",
            "reply": "你好，稍后回复你~",
            "active": False,
            "trigger": "new_message",        # new_message | always
            "active_hours": "09:00-22:00",   # 回复时间窗口
            "min_gap_min": 60,               # 回复周期：同一好友最小回复间隔
            "max_per_day": 10,
        }
    ],
}

# 发送消息：定位聊天抽屉的输入框，键入文字并回车
SEND_MESSAGE_JS = """
() => {
  // 返回抽屉内输入框信息（调试用）
  const eds = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]'));
  for (const el of eds) {
    const r = el.getBoundingClientRect();
    if (r.width && r.x > window.innerWidth * 0.5 && r.y > window.innerHeight * 0.5) {
      return { found: true, tag: el.tagName, editable: el.isContentEditable, x: r.x, y: r.y, w: r.width, h: r.height };
    }
  }
  return { found: false, n: eds.length };
}
"""


def load_config() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 已生成配置模板: {CONFIG}，请编辑后运行（把 active 改为 true）")
        sys.exit(0)
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_F.exists():
        return json.loads(STATE_F.read_text(encoding="utf-8"))
    return {}


def save_state(st: dict):
    STATE_F.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def load_log() -> list:
    if LOG_F.exists():
        return json.loads(LOG_F.read_text(encoding="utf-8"))
    return []


def append_log(entry: dict):
    lg = load_log()
    lg.append(entry)
    if len(lg) > 2000:
        lg = lg[-2000:]
    LOG_F.write_text(json.dumps(lg, ensure_ascii=False, indent=2), encoding="utf-8")


def in_active_hours(window: str) -> bool:
    """active_hours 'HH:MM-HH:MM'，支持跨零点（如 22:00-06:00）；空/无效 = 全天"""
    if not window or "-" not in window:
        return True
    try:
        a, b = window.split("-")
        now = datetime.now().time()
        t1 = datetime.strptime(a.strip(), "%H:%M").time()
        t2 = datetime.strptime(b.strip(), "%H:%M").time()
        if t1 <= t2:
            return t1 <= now <= t2
        return now >= t1 or now <= t2  # 跨零点
    except Exception:
        return True


def can_reply(rule: dict, st: dict, now: float) -> tuple[bool, str]:
    """检查时间窗口/回复周期/每日上限"""
    if not in_active_hours(rule.get("active_hours", "")):
        return False, f"不在回复时间窗口 {rule.get('active_hours')}"
    rec = st.get(rule["name"], {})
    gap = float(rule.get("min_gap_min", 60)) * 60
    last = rec.get("last_reply_ts", 0)
    if last and now - last < gap:
        remain = int((gap - (now - last)) / 60)
        return False, f"回复周期未到（还差 {remain} 分钟）"
    today = datetime.now().strftime("%Y-%m-%d")
    cnt = rec.get("count_date", "")
    cnt_v = rec.get("count", 0) if cnt == today else 0
    if cnt_v >= int(rule.get("max_per_day", 10)):
        return False, "已达每日上限"
    return True, "ok"


def needs_reply(rule: dict, msgs: list, st: dict) -> tuple[bool, str]:
    """判断是否需要回复"""
    trig = rule.get("trigger", "new_message")
    real = [m for m in msgs if m["side"] in ("self", "other")]
    if trig == "always":
        return True, "always 模式"
    if not real:
        return False, "会话为空"
    last = real[-1]
    if last["side"] != "other":
        return False, "最后一条是自己发的，无需回复"
    # 防重复：同一条消息已回过就不回
    rec = st.get(rule["name"], {})
    sig = f"{last['text']}|{len(real)}"
    if rec.get("last_replied_sig") == sig:
        return False, "该消息已回复过"
    return True, f"对方新消息: {last['text'][:20]!r}"


async def send_reply(page, reply: str) -> tuple[bool, str]:
    """在已打开的聊天窗中发送一条消息"""
    info = await page.evaluate(SEND_MESSAGE_JS)
    if not info.get("found"):
        return False, "未找到输入框"
    box = page.locator('textarea, [contenteditable="true"]').last
    try:
        await box.click(timeout=4000)
        await page.wait_for_timeout(400)
        await page.keyboard.type(reply, delay=30)
        await page.wait_for_timeout(300)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(2500)
        # 验证：最后一条 self 消息应包含回复内容
        data = await page.evaluate(D.EXTRACT_CHAT_JS)
        msgs = [m for m in data.get("msgs", []) if m["side"] == "self"]
        if msgs and reply[:10] in msgs[-1]["text"]:
            return True, "已发送（已验证）"
        return True, "已发送（回车已按，未在聊天窗验证到，请人工确认）"
    except Exception as e:
        return False, f"发送失败: {e}"


async def ensure_list_view(page) -> bool:
    """确保当前处于「会话列表」视图：若停在某个聊天窗里，点返回箭头回到列表。

    多任务串行处理时，必须每个任务之间都干净地回到列表，否则下一个任务
    会找不到会话行 / 打开到残留视图（表现为“会话列表中未找到该好友”或“会话为空”）。

    注意：用 IN_CHAT_JS 判定是否处于聊天窗——它只认「发送消息」输入框，
    不会把列表视图的“搜索”框误判成聊天，从而避免误点返回箭头关掉面板。
    """
    for _ in range(4):
        if await page.evaluate(D.IN_CHAT_JS):
            # 确实在聊天窗里 → 找返回箭头退回列表
            back = await page.evaluate(D.BACK_ARROW_JS)
            if back:
                await page.mouse.click(back["x"], back["y"])
                await page.wait_for_timeout(1000)
                continue
            # 在聊天里却找不到返回箭头 → 无法退出，交由上层重开面板
            return False
        # 不在聊天里 → 用会话行存在与否确认是否已在列表视图
        rows = await page.evaluate(D.LIST_ROWS_JS)
        if rows:
            return True
        await page.wait_for_timeout(800)
    return bool(await page.evaluate(D.LIST_ROWS_JS))


async def open_conversation(page, name: str, max_retry: int = 4) -> bool:
    """定位并打开目标会话。每轮都重新定位会话行拿最新坐标（防 React 重渲染漂移），
    点击后确认聊天窗打开（IN_CHAT_JS），打开后做宽松标题校验。成功返回 True。"""
    for attempt in range(max_retry):
        await page.evaluate(D.REMOVE_MASK_JS)
        # max_steps=40：会话列表实际约 70 个（容器 5000px），默认 20 步滚不到深处的好友
        row = await D.find_conversation_row(page, name, max_steps=40)
        if not row:
            await page.wait_for_timeout(1500)
            continue
        # 坐标校验：该点确实落在“右侧会话行”区域内（含头像、视口内），而非错位/空白。
        # 不强制文本含昵称——点击点常落在消息预览上，强校验会把正确会话误判为点错。
        hit_ok = await page.evaluate(
            """(args) => {
                const el = document.elementFromPoint(args.x, args.y);
                if (!el) return false;
                const r = el.getBoundingClientRect();
                if (r.x < window.innerWidth * 0.55) return false;
                if (r.y < 70 || r.y > 920) return false;
                let n = el;
                for (let i = 0; i < 8 && n; i++) {
                    if (n.querySelector && n.querySelector('img')) return true;
                    n = n.parentElement;
                }
                return false;
            }""", {"x": row["x"], "y": row["y"]}
        )
        if not hit_ok:
            await ensure_list_view(page)
            continue
        # 点击并等待聊天加载（以 IN_CHAT_JS 命中右半屏可编辑发送框为准）
        await page.mouse.click(row["x"], row["y"])
        chat_open = False
        for _ in range(10):
            await page.wait_for_timeout(1000)
            if await page.evaluate(D.IN_CHAT_JS):
                chat_open = True
                break
        if not chat_open:
            # 没打开聊天（坐标漂移/未加载）→ 回到列表，下一轮重新查找
            await ensure_list_view(page)
            continue
        #  note: 不再做标题校验——CHAT_TITLE_JS 抓到的大多是消息/系统提示文本而非昵称，
        #  会误把正确会话判成“打开错会话”导致无限重试。点击目标已是按昵称定位的行，
        #  且经过坐标区域校验，可信。
        return True
    return False


async def extract_messages_wait(page, timeout_s: float = 8.0) -> dict:
    """打开聊天后，消息气泡可能尚未渲染完成（发送框已出现但消息还在加载/转圈）。

    这里轮询 EXTRACT_CHAT_JS 直到「抽屉出现且尽量拿到真实消息」。这是多任务串行
    处理时“会话为空”错误的根因修复：原来打开聊天后立刻抽取，常抽到空列表，
    被 needs_reply 误判为“会话为空”而跳过目标好友。
    """
    data = {"found": False, "msgs": []}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = await page.evaluate(D.EXTRACT_CHAT_JS)
        if data.get("found") and data.get("msgs"):
            return data
        await page.wait_for_timeout(700)
    return data


async def process_rule(page, rule: dict, st: dict, dry: bool) -> dict:
    """处理单条规则（每个任务自成一个独立闭环，与上一个任务完全隔离）。"""
    name, reply = rule["name"], rule["reply"]
    now = time.time()
    ok, why = can_reply(rule, st, now)
    if not ok:
        return {"name": name, "action": "skip", "reason": why}

    # 1) 确保处于会话列表视图（与上一任务隔离）
    if not await ensure_list_view(page):
        await D.open_message_panel(page)
        await page.evaluate(D.REMOVE_MASK_JS)
        if not await ensure_list_view(page):
            return {"name": name, "action": "skip", "reason": "无法回到会话列表"}

    # 2) 打开目标会话（含标题校验）
    if not await open_conversation(page, name):
        await ensure_list_view(page)  # 仍回到列表，别影响后续任务
        return {"name": name, "action": "skip", "reason": "会话列表中未找到该好友"}

    # 3) 判断是否需要回复
    trig = rule.get("trigger", "new_message")
    if trig == "always":
        # always 模式无需看消息内容，直接进发送流程
        need, why2 = True, "always 模式"
    else:
        # 非 always：需先抽取聊天消息，且消息可能延迟加载，等待其出现再判定
        data = await extract_messages_wait(page, timeout_s=8.0)
        if not data.get("found"):
            await ensure_list_view(page)
            return {"name": name, "action": "skip",
                    "reason": "会话已打开但未找到消息区"}
        need, why2 = needs_reply(rule, data.get("msgs", []), st)
        if not need:
            await ensure_list_view(page)
            return {"name": name, "action": "skip", "reason": why2}

    # 4) 发送 / 演练
    if dry:
        info = await page.evaluate(SEND_MESSAGE_JS)
        await ensure_list_view(page)
        if info.get("found"):
            return {"name": name, "action": "dry", "reason": f"[演练] 将回复: {reply!r}（触发: {why2}）"}
        return {"name": name, "action": "skip", "reason": "演练模式：未找到输入框"}

    sent, msg = await send_reply(page, reply)
    await ensure_list_view(page)  # 回到列表，供下一个任务
    if not sent:
        return {"name": name, "action": "fail", "reason": msg}

    # 5) 更新状态与日志
    rec = st.get(name, {})
    today = datetime.now().strftime("%Y-%m-%d")
    if rec.get("count_date") != today:
        rec["count"], rec["count_date"] = 0, today
    rec["count"] = rec.get("count", 0) + 1
    rec["last_reply_ts"] = time.time()
    st[name] = rec
    append_log({
        "ts": time.time(),
        "time_str": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "to": name,
        "reply": reply,
    })
    return {"name": name, "action": "replied", "reason": f"已回复: {reply!r}"}


async def run_round(page, config: dict, st: dict, dry: bool) -> list:
    """一轮处理所有激活规则"""
    results = []
    for rule in config.get("rules", []):
        if not rule.get("active"):
            results.append({"name": rule.get("name"), "action": "skip", "reason": "规则未激活"})
            continue
        try:
            r = await process_rule(page, rule, st, dry)
        except Exception as e:
            r = {"name": rule.get("name"), "action": "fail", "reason": f"异常: {e}"}
        print(f"  [{r['action']:7}] {r['name']}: {r['reason']}")
        results.append(r)
        save_state(st)  # 每条规则后落盘
    return results


async def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--dry-run", action="store_true", help="演练：不真正发送")
    ap.add_argument("--cookies", default="")
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--results-file", default="", help="把本轮结果 JSON 写入该文件（控制台引擎用）")
    a = ap.parse_args()

    config = load_config()
    st = load_state()
    active = [r for r in config.get("rules", []) if r.get("active")]
    print(f"[OK] 配置加载: {len(config.get('rules', []))} 条规则，激活 {len(active)} 条"
          f"{'（演练模式）' if a.dry_run else ''}")
    if not active:
        print("[提示] 没有激活的规则，请在 replies_config.json 中把 active 改为 true")
        if a.once:
            return

    pw, browser, ctx = await D.start_browser(headless=not a.visible)
    ck = a.cookies or D.find_latest_session()
    if not ck:
        print("[ERROR] 未找到 cookie，请先扫码登录")
        sys.exit(1)
    await D.load_cookies(ctx, ck)
    page = await ctx.new_page()
    try:
        await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)
        cookies = await ctx.cookies()
        if not D.has_login_cookie(cookies):
            print("[ERROR] 登录态失效，请重新扫码")
            sys.exit(2)
        print("[OK] 登录态有效")

        first = True
        while True:
            print(f"\n==== 回复轮次 {datetime.now().strftime('%H:%M:%S')} ====")
            ok = await D.open_message_panel(page) if first else await D.open_message_panel(page)
            if not ok:
                print("[ERROR] 消息面板未能打开，本轮跳过")
                results = [{"name": "-", "action": "fail", "reason": "消息面板未能打开"}]
                if a.results_file:
                    Path(a.results_file).write_text(json.dumps(
                        {"ts": time.time(), "results": results}, ensure_ascii=False), encoding="utf-8")
            else:
                await page.evaluate(D.REMOVE_MASK_JS)
                results = await run_round(page, config, st, a.dry_run)
                if a.results_file:
                    Path(a.results_file).write_text(json.dumps(
                        {"ts": time.time(), "results": results}, ensure_ascii=False), encoding="utf-8")
            save_state(st)
            if a.once:
                break
            wait_s = int(config.get("check_interval_min", 10)) * 60
            print(f"[..] {config.get('check_interval_min')} 分钟后进行下一轮检查 ...")
            await page.wait_for_timeout(min(wait_s, 600) * 1000)
            # 长等待分片，期间刷新面板状态
            remain = wait_s - min(wait_s, 600)
            while remain > 0:
                await page.wait_for_timeout(min(remain, 600) * 1000)
                remain -= min(remain, 600)
            first = False
    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
