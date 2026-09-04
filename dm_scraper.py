#!/usr/bin/env python3
"""
抖音私信抓取 — 复用扫码登录后的 Cookie 会话

用法:
    python dm_scraper.py                    # 抓取消息面板的会话列表
    python dm_scraper.py --visible          # 有头模式（调试用）
    python dm_scraper.py --deep             # 深入每个会话抓取聊天记录
    python dm_scraper.py --dump             # 额外保存面板 HTML（用于调试选择器）
    python dm_scraper.py --cookies xxx.json # 指定 cookie 文件
    python dm_scraper.py --limit 10         # --deep 时最多进入前 N 个会话

输出:
    output/dm_list.json       会话列表（JSON）
    output/dm_list.md         会话列表（Markdown）
    output/dm_messages.json   聊天记录（--deep 时）
    output/dm_panel.html      面板 HTML（--dump 时）
    output/dm_screenshot.png  截图
"""
import asyncio
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent
OUT = PROJ / "output"
OUT.mkdir(exist_ok=True)

DOUYIN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
// 压制 trust 弹窗遮罩（会拦截鼠标事件，且被移除后会被 React 重新渲染，需 CSS 级压制）
(function maskKill() {
  const inject = () => {
    const s = document.createElement('style');
    s.id = '__mask_kill';
    s.textContent = '.trust-login-dialog-mask,#trust-logout-dialog,.trust-login-dialog{display:none!important}';
    if (document.head && !document.getElementById('__mask_kill')) document.head.appendChild(s);
  };
  const t = setInterval(inject, 500);
  setTimeout(() => clearInterval(t), 15000);
  inject();
})();
"""

TIME_RE = re.compile(r"^(昨天|前天|今天|刚刚|\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日|周[一二三四五六日]|\d{4}-\d{1,2}-\d{1,2})$")


def find_latest_session() -> str:
    """找 output/ 下最新的 session_*.json"""
    files = sorted(glob.glob(str(OUT / "session_*.json")), key=os.path.getmtime)
    if not files:
        return ""
    return files[-1]


async def start_browser(headless: bool):
    from patchright.async_api import async_playwright

    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
    )
    pw = await async_playwright().start()
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",                  # 沙箱内 swiftshader 会崩溃（04:27 踩坑）
        "--disable-software-rasterizer",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    browser = await pw.chromium.launch(headless=headless, args=args)
    ctx = await browser.new_context(
        viewport={"width": 1600, "height": 1000},
        user_agent=DOUYIN_UA,
        locale="zh-CN",
        ignore_https_errors=True,
    )
    await ctx.add_init_script(STEALTH_JS)
    return pw, browser, ctx


async def load_cookies(ctx, cookie_file: str) -> int:
    with open(cookie_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    cookies = data.get("cookies", data if isinstance(data, list) else [])
    if cookies:
        await ctx.add_cookies(cookies)
    return len(cookies)


def has_login_cookie(ctx_cookies) -> bool:
    names = {c.get("name", "") for c in ctx_cookies}
    return bool(names & {"sessionid", "sessionid_ss", "sid_tt", "uid_tt"})


# ──────────────────── 页面操作 ────────────────────

OPEN_PANEL_JS = """
() => {
  // 找右上角导航里文字为“消息”的可点击元素，点击它或其可点击祖先
  const els = Array.from(document.querySelectorAll('div,span,p,li'));
  const cands = els.filter(el => {
    if (el.children.length > 2) return false;
    const t = (el.textContent || '').trim();
    if (t !== '消息') return false;
    const r = el.getBoundingClientRect();
    return r.top >= 0 && r.top < 140 && r.width > 0;
  });
  if (!cands.length) return false;
  let target = cands[cands.length - 1];
  // 向上找可点击祖先（最多 5 层）
  let node = target;
  for (let i = 0; i < 5 && node; i++) {
    node.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
    node = node.parentElement;
  }
  return true;
}
"""

PANEL_PROBE_JS = """
() => {
  // 探测消息面板是否已打开（兼容最新右上角图标版布局）
  const all = document.querySelectorAll('div,section,aside');
  for (const el of all) {
    const t = (el.getAttribute('aria-label') || '');
    if (t.includes('消息')) return { open: true, hint: 'aria-label:' + t };
  }
  // 抽屉内有「发送消息」输入框（占位符文本）即视为已打开
  const hasSendBox = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input'))
    .some(el => {
      const r = el.getBoundingClientRect();
      return r.width && r.x > window.innerWidth * 0.5;
    });
  if (hasSendBox) return { open: true, hint: 'input-box(right)' };
  const leafHasSend = Array.from(document.querySelectorAll('*')).some(
    el => el.children.length === 0 && (el.textContent || '').trim() === '发送消息'
  );
  if (leafHasSend) return { open: true, hint: 'placeholder:发送消息' };
  const body = (document.body && document.body.innerText) || '';
  const m = body.match(/消息\\s*[\\(（]\\s*\\d+/);
  if (m) return { open: true, hint: 'header:' + m[0] };
  return { open: false, hint: body.slice(0, 200) };
}
"""

# 定位顶部导航右上角的「消息」图标（当前为 li 图标按钮，不是 <p> 文本）
OPEN_MSG_ICON_JS = """
() => {
  // 取文字恰好为「消息」、位于右上角、可见的元素，向上找可点击祖先（li/a）
  let el = Array.from(document.querySelectorAll('p,span,div,li,a')).find(e => {
    const t = (e.textContent || '').trim();
    const r = e.getBoundingClientRect();
    return t === '消息' && r.width > 0 && r.height > 0 && r.x > window.innerWidth * 0.7 && r.top < 120;
  });
  if (!el) return null;
  let node = el;
  for (let i = 0; i < 8 && node; i++) {
    const s = window.getComputedStyle(node);
    if (node.tagName === 'LI' || node.tagName === 'A' || node.getAttribute('role') === 'button' || s.cursor === 'pointer') {
      const r = node.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2, tag: node.tagName };
    }
    node = node.parentElement;
  }
  const r = el.getBoundingClientRect();
  return { x: r.x + r.width / 2, y: r.y + r.height / 2, tag: el.tagName };
}
"""

# 会话列表抽取：定位消息抽屉里的滚动列表，逐行解析
EXTRACT_LIST_JS = """
() => {
  const timeRe = /^(昨天|前天|今天|刚刚|\\d{1,2}:\\d{2}|\\d{1,2}月\\d{1,2}日|周[一二三四五六日]|\\d{4}-\\d{1,2}-\\d{1,2})$/;

  // 1) 找到所有“含头像 img 且文本 ≥1 行”的候选条目
  const items = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('div,li,a');
  for (const el of nodes) {
    const img = el.querySelector('img');
    if (!img) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 120) continue;
    const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
    if (lines.length < 1) continue;
    const r = el.getBoundingClientRect();
    // 消息抽屉在页面右侧
    if (r.left < window.innerWidth * 0.55) continue;
    if (r.height < 40 || r.height > 140) continue;
    if (r.width < 150 || r.width > window.innerWidth * 0.5) continue;
    // 必须有子元素（纯容器排除）
    if (el.children.length < 2) continue;
    const key = lines[0] + '|' + r.top.toFixed(0);
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({
      name: lines[0],
      preview: lines.length > 1 ? lines.slice(1, -0).filter(l => !timeRe.test(l)).join(' ') : '',
      time: lines.filter(l => timeRe.test(l)).pop() || '',
      top: r.top,
      text: txt,
    });
  }
  // 按 top 排序，去重（同名保留信息最全的）
  items.sort((a, b) => a.top - b.top);
  const byName = new Map();
  for (const it of items) {
    if (!byName.has(it.name) || it.text.length > byName.get(it.name).text.length) {
      byName.set(it.name, it);
    }
  }
  return Array.from(byName.values());
}
"""

SCROLL_LIST_JS = """
async (rounds) => {
  // 在右侧抽屉里找可滚动容器并向下滚动，加载更多会话
  const drawers = document.querySelectorAll('div,section,aside');
  let scroller = null;
  for (const el of drawers) {
    const r = el.getBoundingClientRect();
    if (r.left < window.innerWidth * 0.55) continue;
    if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200) {
      if (!scroller || el.clientHeight > scroller.clientHeight) scroller = el;
    }
  }
  if (!scroller) return 0;
  scroller.scrollTop = scroller.scrollHeight;
  await new Promise(r => setTimeout(r, 800));
  return scroller.scrollTop;
}
"""

SCROLL_TOP_JS = """
async () => {
  // 把消息抽屉的滚动容器滚回顶部
  const drawers = document.querySelectorAll('div,section,aside');
  let scroller = null;
  for (const el of drawers) {
    const r = el.getBoundingClientRect();
    if (r.left < window.innerWidth * 0.55) continue;
    if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200) {
      if (!scroller || el.clientHeight > scroller.clientHeight) scroller = el;
    }
  }
  if (!scroller) return 0;
  scroller.scrollTop = 0;
  await new Promise(r => setTimeout(r, 600));
  return scroller.scrollTop;
}
"""

# 定位聊天窗口：以“发送消息”输入框为锚点，向上找抽屉，再取不包含输入框的文本最多的子列表
FIND_CHAT_JS = """
() => {
  const timeRe = /^(昨天|前天|今天|刚刚|\\d{1,2}:\\d{2}|周[一二三四五六日]|\\d{4}-\\d{1,2}-\\d{1,2}|\\d{1,2}月\\d{1,2}日)$/;
  const dtRe = /^(昨天|前天|今天|刚刚)?\\s*\\d{1,2}:\\d{2}$/;
  // 1) 找输入框
  let input = null;
  for (const el of document.querySelectorAll('textarea, [contenteditable="true"]')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.x > window.innerWidth * 0.5) { input = el; break; }
  }
  if (!input) {
    for (const el of document.querySelectorAll('*')) {
      if (el.children.length === 0 && (el.textContent || '').trim() === '发送消息') { input = el; break; }
    }
  }
  if (!input) return { found: false, reason: 'no_input', msgs: [] };
  // 2) 向上找聊天抽屉容器
  let drawer = null;
  let node = input;
  for (let i = 0; i < 10 && node; i++) {
    const r = node.getBoundingClientRect();
    if (r.width >= 260 && r.width <= 560 && r.height > 400) { drawer = node; break; }
    node = node.parentElement;
  }
  if (!drawer) return { found: false, reason: 'no_drawer', msgs: [] };
  // 3) 抽屉内找消息列表：不包含输入框、文本叶子最多的 div
  const countLeaves = (el) => {
    let n = 0;
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) { if ((w.currentNode.textContent || '').trim()) n++; }
    return n;
  };
  let list = null, best = 0;
  drawer.querySelectorAll('div').forEach(el => {
    if (el.contains(input) || input.contains(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height < 200) return;
    const n = countLeaves(el);
    if (n > best) { best = n; list = el; }
  });
  return { found: true, drawer: !!drawer, list: !!list, drawer_el: drawer, list_el: list, timeRe, dtRe };
}
"""

# 聊天记录抽取（--deep）：锚点定位聊天窗口，只收消息列表内文本
EXTRACT_CHAT_JS = """
() => {
  const timeRe = /^(昨天|前天|今天|刚刚|\\d{1,2}:\\d{2}|周[一二三四五六日]|\\d{4}-\\d{1,2}-\\d{1,2}|\\d{1,2}月\\d{1,2}日)$/;
  const dtRe = /^(昨天|前天|今天|刚刚)?\\s*\\d{1,2}:\\d{2}$/;
  let input = null;
  for (const el of document.querySelectorAll('textarea, [contenteditable="true"]')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.x > window.innerWidth * 0.5) { input = el; break; }
  }
  if (!input) {
    for (const el of document.querySelectorAll('*')) {
      if (el.children.length === 0 && (el.textContent || '').trim() === '发送消息') { input = el; break; }
    }
  }
  if (!input) return { found: false, reason: 'no_input', msgs: [] };
  let drawer = null;
  let node = input;
  for (let i = 0; i < 10 && node; i++) {
    const r = node.getBoundingClientRect();
    if (r.width >= 260 && r.width <= 560 && r.height > 400) { drawer = node; break; }
    node = node.parentElement;
  }
  if (!drawer) return { found: false, reason: 'no_drawer', msgs: [] };
  const countLeaves = (el) => {
    let n = 0;
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) { if ((w.currentNode.textContent || '').trim()) n++; }
    return n;
  };
  let list = null, best = 0;
  drawer.querySelectorAll('div').forEach(el => {
    if (el.contains(input) || input.contains(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height < 200) return;
    const n = countLeaves(el);
    if (n > best) { best = n; list = el; }
  });
  const scope = list || drawer;
  const cr = scope.getBoundingClientRect();
  const center = cr.x + cr.width / 2;
  const msgs = [];
  const seen = new Set();
  const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const t = (walker.currentNode.textContent || '').trim();
    if (!t || t.length > 500) continue;
    const el = walker.currentNode.parentElement;
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const key = t + '@' + Math.round(r.top);
    if (seen.has(key)) continue;
    seen.add(key);
    let side = 'other';
    const c = r.x + r.width / 2;
    if (timeRe.test(t) || dtRe.test(t)) side = 'time';
    else if (c > center + 40) side = 'self';
    else if (c < center - 40) side = 'other';
    else side = 'sys';
    msgs.push({ side, text: t, top: Math.round(r.top) });
  }
  msgs.sort((a, b) => a.top - b.top);
  return {
    found: true,
    msgs,
    scroll_top: scope.scrollTop || 0,
    scroll_height: scope.scrollHeight || 0,
  };
}
"""

# 取当前聊天窗标题（抽屉头部联系人或群名），用于在多任务链式处理时
# 校验“打开的会话确实是目标会话”，避免点到残留/错位视图
CHAT_TITLE_JS = """
() => {
  let input = null;
  for (const el of document.querySelectorAll('textarea, [contenteditable="true"]')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.x > window.innerWidth * 0.5) { input = el; break; }
  }
  if (!input) return null;
  let node = input;
  for (let i = 0; i < 10 && node; i++) {
    const r = node.getBoundingClientRect();
    if (r.width >= 260 && r.width <= 560 && r.height > 400) {
      let best = null, bestLen = 0;
      node.querySelectorAll('div,span,a,p').forEach(el => {
        const t = (el.innerText || '').trim();
        if (!t || t.length > 20) return;
        const rr = el.getBoundingClientRect();
        if (rr.y < r.y + 60 && rr.x > r.x - 10 && rr.width < r.width * 0.6) {
          if (t.length > bestLen) { bestLen = t.length; best = t; }
        }
      });
      return best;
    }
    node = node.parentElement;
  }
  return null;
}
"""

# 判定“当前是否处于聊天窗（而非会话列表）”。
# 关键：列表视图也有一个右半屏的“搜索”输入框，会被 EXTRACT_CHAT_JS 误判为聊天。
# 这里只认“右半屏的 textarea / contenteditable 可编辑框”——聊天窗的发送框是
# contenteditable（无 placeholder 属性），而列表的搜索框是普通 <input>，
# 二者天然区分，不会误判。该判定与 send_reply 的输入框定位保持一致。
IN_CHAT_JS = """
() => {
  return Array.from(document.querySelectorAll('textarea, [contenteditable="true"]')).some(el => {
    const r = el.getBoundingClientRect();
    return r.width && r.x > window.innerWidth * 0.5;
  });
}
"""

# 返回会话列表：点聊天窗头部左上角的返回箭头（Escape 无效）
# 注意：要从输入框向上找“最顶部的同宽窄容器”（真抽屉），第一层窄容器是消息列表，不含头部
BACK_ARROW_JS = """
() => {
  let input = null;
  for (const el of document.querySelectorAll('textarea, [contenteditable="true"]')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.x > window.innerWidth * 0.5) { input = el; break; }
  }
  if (!input) return null;
  let node = input.parentElement, cand = null;
  for (let i = 0; i < 14 && node; i++) {
    const r = node.getBoundingClientRect();
    if (r.width >= 260 && r.width <= 560 && r.height > 300) cand = node;
    else if (cand) break;
    node = node.parentElement;
  }
  if (!cand) return null;
  const r = cand.getBoundingClientRect();
  let best = null;
  cand.querySelectorAll('*').forEach(el => {
    const rr = el.getBoundingClientRect();
    if (rr.height === 0 || rr.width === 0) return;
    if (rr.y < r.y + 2 || rr.y > r.y + 70) return;
    if (rr.x < r.x || rr.x > r.x + 60) return;
    if (el.children.length > 4) return;
    if (!best || rr.width < best.w) best = { x: rr.x + rr.width / 2, y: rr.y + rr.height / 2, w: rr.width };
  });
  return best;
}
"""

SCROLL_CHAT_TOP_JS = """
async () => {
  // 以输入框为锚找聊天消息列表并滚到顶部，触发历史加载
  let input = null;
  for (const el of document.querySelectorAll('textarea, [contenteditable="true"]')) {
    const r = el.getBoundingClientRect();
    if (r.width && r.x > window.innerWidth * 0.5) { input = el; break; }
  }
  if (!input) {
    for (const el of document.querySelectorAll('*')) {
      if (el.children.length === 0 && (el.textContent || '').trim() === '发送消息') { input = el; break; }
    }
  }
  if (!input) return -1;
  let cand = null;
  let node = input.parentElement;
  for (let i = 0; i < 14 && node; i++) {
    const r = node.getBoundingClientRect();
    if (r.width >= 260 && r.width <= 560 && r.height > 300) cand = node;
    else if (cand) break;
    node = node.parentElement;
  }
  if (!cand) return -1;
  const countLeaves = (el) => {
    let n = 0;
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) { if ((w.currentNode.textContent || '').trim()) n++; }
    return n;
  };
  let list = null, best = 0;
  cand.querySelectorAll('div').forEach(el => {
    if (el.contains(input) || input.contains(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height < 200) return;
    const n = countLeaves(el);
    if (n > best) { best = n; list = el; }
  });
  const scope = list || cand;
  scope.scrollTop = 0;
  await new Promise(r => setTimeout(r, 900));
  return scope.scrollTop;
}
"""

# 校验坐标处是否真的是目标会话行（防止 React 重渲染导致坐标漂移）
VERIFY_ROW_JS = """
(args) => {
  const el = document.elementFromPoint(args.x, args.y);
  if (!el) return { ok: false, hit: 'none' };
  const row = el.closest('div,li,a');
  let text = '';
  let node = el;
  for (let i = 0; i < 6 && node; i++) {
    text = (node.innerText || '').trim().slice(0, 40);
    if (text) break;
    node = node.parentElement;
  }
  return { ok: text.includes(args.name), hit: text };
}
"""


REMOVE_MASK_JS = """
() => {
  // 移除拦截点击的 trust 弹窗遮罩 + 确保 CSS 压制样式存在
  let n = 0;
  document.querySelectorAll('.trust-login-dialog-mask, #trust-logout-dialog, [class*="trust-login"]').forEach(e => { e.remove(); n++; });
  if (document.head && !document.getElementById('__mask_kill')) {
    const s = document.createElement('style');
    s.id = '__mask_kill';
    s.textContent = '.trust-login-dialog-mask,#trust-logout-dialog,.trust-login-dialog{display:none!important}';
    document.head.appendChild(s);
  }
  return n;
}
"""

# 返回消息抽屉中的会话行（精确名称 + 坐标，供真实鼠标点击）
LIST_ROWS_JS = """
() => {
  const timeRe = /^(昨天|前天|今天|刚刚|\\d{1,2}:\\d{2}|\\d{1,2}月\\d{1,2}日|周[一二三四五六日]|\\d{4}-\\d{1,2}-\\d{1,2})$/;
  const rows = [];
  const seen = new Set();
  const nodes = document.querySelectorAll('div,li,a');
  for (const el of nodes) {
    const img = el.querySelector('img');
    if (!img) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 120) continue;
    const lines = txt.split('\\n').map(s => s.trim()).filter(Boolean);
    if (!lines.length) continue;
    const r = el.getBoundingClientRect();
    if (r.left < window.innerWidth * 0.55) continue;
    if (r.height < 40 || r.height > 140) continue;
    if (r.width < 150 || r.width > window.innerWidth * 0.5) continue;
    if (el.children.length < 2) continue;
    const key = lines[0] + '|' + r.top.toFixed(0);
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push({ name: lines[0], x: r.x + r.width / 2, y: r.y + r.height / 2, text: txt });
  }
  const byName = new Map();
  for (const it of rows) {
    if (!byName.has(it.name) || it.text.length > byName.get(it.name).text.length) byName.set(it.name, it);
  }
  return Array.from(byName.values());
}
"""


async def open_message_panel(page) -> bool:
    """尝试打开消息面板（兼容最新右上角图标版布局）"""
    probe = await page.evaluate(PANEL_PROBE_JS)
    if probe.get("open"):
        print("[OK] 消息面板已打开", flush=True)
        return True
    # 优先用 Playwright locator 点击顶部导航的「消息」图标（自动换算坐标，最稳）
    for attempt in range(3):
        clicked = False
        # 方法A：Playwright locator（li/a 含文字“消息”，取右上角可见那个）
        for sel in ('li:has-text("消息")', 'a:has-text("消息")', 'div:has-text("消息")'):
            try:
                loc = page.locator(sel).filter(has_text="消息").first
                if await loc.count() > 0:
                    box = await loc.bounding_box()
                    if box and box["x"] > 0 and box["y"] < 120:
                        await loc.click(timeout=4000)
                        clicked = True
                        break
            except Exception:
                continue
        # 方法B：JS 定位可点击祖先后真实鼠标点击（兜底）
        if not clicked:
            try:
                box = await page.evaluate(OPEN_MSG_ICON_JS)
                if box:
                    await page.mouse.move(box["x"], box["y"])
                    await page.wait_for_timeout(500)
                    await page.mouse.click(box["x"], box["y"])
                    clicked = True
            except Exception as e:
                print(f"[WARN] 消息图标兜底点击失败(第{attempt+1}次): {e}", flush=True)
        if clicked:
            for _ in range(15):
                await page.wait_for_timeout(800)
                if (await page.evaluate(PANEL_PROBE_JS)).get("open"):
                    print("[OK] 消息面板已打开", flush=True)
                    return True
        await page.wait_for_timeout(1500)
    # 最后手段：旧 JS 派发点击
    await page.evaluate(OPEN_PANEL_JS)
    await page.wait_for_timeout(2500)
    return (await page.evaluate(PANEL_PROBE_JS)).get("open", False)


async def find_conversation_row(page, name: str, max_steps: int = 20):
    """在消息抽屉中定位会话行；不在可见区则逐步向下滚动寻找"""
    await page.evaluate(SCROLL_TOP_JS)
    await page.wait_for_timeout(500)
    for step in range(max_steps):
        rows = await page.evaluate(LIST_ROWS_JS)
        row_map = {r["name"]: r for r in rows if 80 < r["y"] < 900}
        if name in row_map:
            return row_map[name]
        # 向下滚一步
        moved = await page.evaluate(
            """
            async () => {
              const drawers = document.querySelectorAll('div,section,aside');
              let scroller = null;
              for (const el of drawers) {
                const r = el.getBoundingClientRect();
                if (r.left < window.innerWidth * 0.55) continue;
                if (el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 200) {
                  if (!scroller || el.clientHeight > scroller.clientHeight) scroller = el;
                }
              }
              if (!scroller) return false;
              const before = scroller.scrollTop;
              scroller.scrollTop = before + 350;
              await new Promise(r => setTimeout(r, 450));
              return scroller.scrollTop !== before;
            }
            """
        )
        if not moved:
            break
    return None


async def scrape_conversation_list(page, max_scroll: int = 5):
    """抓会话列表，滚动加载更多"""
    blocklist = {"充钻石", "客户端", "壁纸", "通知", "投稿", "消息", "搜索", "直播", "短剧"}
    all_items = {}
    for i in range(max_scroll):
        items = await page.evaluate(EXTRACT_LIST_JS)
        for it in items:
            if it["name"] not in blocklist:
                all_items[it["name"]] = it
        print(f"  [scroll {i+1}] 当前累计 {len(all_items)} 个会话")
        if i < max_scroll - 1:
            try:
                await page.evaluate(SCROLL_LIST_JS, i + 1)
                await page.wait_for_timeout(1200)
            except Exception:
                break
    return list(all_items.values())


async def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--cookies", default="")
    ap.add_argument("--visible", action="store_true")
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--scroll", type=int, default=5)
    ap.add_argument("--history-rounds", type=int, default=4, help="每个会话向上滚动加载历史的轮数")
    a = ap.parse_args()

    cookie_file = a.cookies or find_latest_session()
    if not cookie_file:
        print("[ERROR] 未找到 cookie 文件。请先通过 server_v3.py 完成扫码登录。")
        sys.exit(1)
    print(f"[OK] 使用 cookie: {cookie_file}")

    pw, browser, ctx = await start_browser(headless=not a.visible)
    n = await load_cookies(ctx, cookie_file)
    print(f"[OK] 已加载 {n} 条 Cookie")

    page = await ctx.new_page()
    try:
        print("[..] 打开 douyin.com/jingxuan ...")
        await page.goto("https://www.douyin.com/jingxuan", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(5000)

        cookies = await ctx.cookies()
        if not has_login_cookie(cookies):
            print("[ERROR] Cookie 未生效（未检测到 sessionid），可能已过期，请重新扫码登录。")
            await page.screenshot(path=str(OUT / "dm_screenshot.png"))
            sys.exit(2)
        print("[OK] 登录态有效")

        if not await open_message_panel(page):
            print("[ERROR] 消息面板未能打开，保存现场供调试：")
            await page.screenshot(path=str(OUT / "dm_screenshot.png"), full_page=False)
            if a.dump:
                await page.screenshot(path=str(OUT / "dm_screenshot.png"))
                html = await page.content()
                (OUT / "dm_panel.html").write_text(html, encoding="utf-8")
                print(f"  已保存 {OUT/'dm_panel.html'}")
            sys.exit(3)

        # 清掉 trust 弹窗遮罩（会拦截鼠标事件）
        n = await page.evaluate(REMOVE_MASK_JS)
        if n:
            print(f"[OK] 已移除 {n} 个 trust 遮罩元素")

        await page.wait_for_timeout(2000)
        print("[..] 抓取会话列表 ...")
        convs = await scrape_conversation_list(page, max_scroll=a.scroll)

        result = {
            "timestamp": time.time(),
            "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "https://www.douyin.com/jingxuan 消息面板",
            "count": len(convs),
            "conversations": convs,
        }
        (OUT / "dm_list.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        md = [f"# 抖音私信会话列表（{result['time_str']}）", "", f"共 {len(convs)} 个会话", ""]
        md.append("| # | 名称 | 最近消息 | 时间 |")
        md.append("|---|------|----------|------|")
        for i, c in enumerate(convs, 1):
            preview = c["preview"].replace("|", "\\|")[:50]
            md.append(f"| {i} | {c['name']} | {preview} | {c['time']} |")
        (OUT / "dm_list.md").write_text("\n".join(md), encoding="utf-8")
        print(f"[OK] 会话列表已保存: {OUT/'dm_list.json'} / dm_list.md（{len(convs)} 个会话）")

        # 深度抓取：进入每个会话读聊天记录
        if a.deep and convs:
            print("[..] 深度抓取聊天记录 ...")
            messages_all = {}
            for i, c in enumerate(convs[: a.limit]):
                name = c["name"]
                row = await find_conversation_row(page, name)
                if not row:
                    print(f"    [WARN] 面板中未找到会话行: {name}")
                    continue
                print(f"  [{i+1}/{min(a.limit, len(convs))}] 进入: {name}")
                try:
                    data = {"found": False, "msgs": []}
                    for attempt in range(3):
                        await page.evaluate(REMOVE_MASK_JS)
                        if attempt > 0:
                            row = await find_conversation_row(page, name)
                            if not row:
                                break
                        # 点击前校验坐标处确实是目标行
                        chk = await page.evaluate(VERIFY_ROW_JS, {"x": row["x"], "y": row["y"], "name": name})
                        if not chk.get("ok"):
                            print(f"    [WARN] 坐标校验失败(hit={chk.get('hit','')[:20]!r})，重新定位")
                            row = await find_conversation_row(page, name)
                            if not row:
                                break
                        await page.mouse.click(row["x"], row["y"] + (attempt - 1) * 4)
                        # 轮询等待聊天窗加载（最多10秒）
                        for _ in range(10):
                            await page.wait_for_timeout(1000)
                            data = await page.evaluate(EXTRACT_CHAT_JS)
                            if data.get("found"):
                                break
                        if data.get("found"):
                            # 加载到聊天窗后，向上滚动加载历史消息（多轮）
                            for rnd in range(a.history_rounds):
                                try:
                                    await page.evaluate(SCROLL_CHAT_TOP_JS)
                                except Exception:
                                    break
                                await page.wait_for_timeout(700)
                            data = await page.evaluate(EXTRACT_CHAT_JS)
                            break
                    msgs = data.get("msgs", [])
                    if not data.get("found"):
                        await page.screenshot(path=str(OUT / f"dbg_fail_{name}.png"))
                    messages_all[name] = {
                        "found_container": data.get("found", False),
                        "count": len(msgs),
                        "messages": msgs,
                    }
                    print(f"    [OK] 抓到 {len(msgs)} 条（容器: {data.get('found')}）")
                    # 返回会话列表：点聊天窗左上角返回箭头（Escape 无效）
                    back = None
                    try:
                        back = await page.evaluate(BACK_ARROW_JS)
                    except Exception:
                        pass
                    if back:
                        await page.mouse.click(back["x"], back["y"])
                        await page.wait_for_timeout(1800)
                    else:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(1200)
                        if not await open_message_panel(page):
                            await page.wait_for_timeout(2000)
                    await page.evaluate(REMOVE_MASK_JS)
                except Exception as e:
                    print(f"    [ERROR] {name}: {e}")

            (OUT / "dm_messages.json").write_text(
                json.dumps(messages_all, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[OK] 聊天记录已保存: {OUT/'dm_messages.json'}")

        await page.screenshot(path=str(OUT / "dm_screenshot.png"))
        if a.dump:
            html = await page.content()
            (OUT / "dm_panel.html").write_text(html, encoding="utf-8")
            print(f"[OK] HTML 已保存: {OUT/'dm_panel.html'}")

    finally:
        await browser.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
