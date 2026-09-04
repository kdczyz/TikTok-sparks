#!/usr/bin/env python3
"""诊断：消息面板为什么打不开"""
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
    await page.wait_for_timeout(6000)
    print("URL =", page.url)
    print("TITLE =", await page.title())
    probe = await page.evaluate(D.PANEL_PROBE_JS)
    print("PROBE =", json.dumps(probe, ensure_ascii=False))
    # 页面上是否有“消息”相关文字
    hits = await page.evaluate(
        "() => Array.from(document.querySelectorAll('p,span,div')).filter(e=>{const t=(e.textContent||'').trim();return (t==='消息')&&e.children.length<=1}).map(e=>{const r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),cls:e.className?.toString?.().slice(0,60)};})"
    )
    print("MSG_ELS =", json.dumps(hits, ensure_ascii=False))
    # 找到可见的“消息”文字元素，打印祖先链，并尝试点击
    chain = await page.evaluate(
        """() => {
          const el = Array.from(document.querySelectorAll('p,span,div,a')).find(e=>{
            const t=(e.textContent||'').trim();
            const r=e.getBoundingClientRect();
            return t==='消息' && r.width>0 && r.x > window.innerWidth*0.7 && r.top < 100;
          });
          if (!el) return [];
          const out = [];
          let n = el;
          for (let i=0; i<8 && n; i++) {
            const r=n.getBoundingClientRect();
            out.push({tag:n.tagName, cls:(n.className||'').toString().slice(0,50), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height), role:n.getAttribute('role'), href:n.getAttribute('href')});
            n = n.parentElement;
          }
          return out;
        }"""
    )
    print("CHAIN =", json.dumps(chain, ensure_ascii=False))
    html = await page.content()
    (PROJ/"output"/"dbg_panel.html").write_text(html, encoding="utf-8")
    await page.screenshot(path=str(PROJ/"output"/"dbg_panel_before_click.png"), full_page=False)
    # 尝试点击最小的“消息”容器（最具体）
    target = await page.evaluate(
        """() => {
          const all = Array.from(document.querySelectorAll('p,span,div,li,a')).filter(e=>{
            const r=e.getBoundingClientRect();
            return (e.textContent||'').trim()==='消息' && r.width>0 && r.x > window.innerWidth*0.8 && r.top < 80;
          });
          // 选面积最小的叶子节点（最具体）
          let best=null;
          for (const el of all) {
            const r=el.getBoundingClientRect();
            const area=r.width*r.height;
            if (!best || area < best.area) best={x: r.x+r.width/2, y: r.y+r.height/2, area, tag: el.tagName};
          }
          return best;
        }"""
    )
    print("CLICK_TARGET =", target)
    # 打印顶部导航所有 li / a 项
    top_items = await page.evaluate(
        """() => {
          const container = document.querySelector('div.Tx2HHA7o, div.ohQ_Zt2d, div.bxA6zgj0') || document.body;
          const items = Array.from(container.querySelectorAll('li, a, div')).filter(e=>{
            const t=(e.textContent||'').trim();
            return ['消息','通知','投稿','赚钱','客户端'].includes(t);
          }).map(e=>{
            const r=e.getBoundingClientRect();
            return {tag:e.tagName, text:(e.textContent||'').trim(), x:Math.round(r.x), y:Math.round(r.y), w:Math.round(r.width), h:Math.round(r.height), cls:(e.className||'').toString().slice(0,60)};
          });
          return items;
        }"""
    )
    print("TOP_ITEMS =", json.dumps(top_items, ensure_ascii=False))
    if target:
        # 通过 JS 找到文本“消息”的最近可点击祖先并点击
        clickable = await page.evaluate(
            """() => {
              let el = Array.from(document.querySelectorAll('p,span,div')).find(e=>{
                const t=(e.textContent||'').trim();
                const r=e.getBoundingClientRect();
                return t==='消息' && r.width>0 && r.x > window.innerWidth*0.8 && r.top < 80;
              });
              if (!el) return null;
              // 向上找 li / a / 有 role=button / cursor=pointer 的元素
              let node = el;
              for (let i=0; i<8 && node; i++) {
                const s = window.getComputedStyle(node);
                if (node.tagName === 'LI' || node.tagName === 'A' || node.getAttribute('role') === 'button' || s.cursor === 'pointer') {
                  const r = node.getBoundingClientRect();
                  return {tag:node.tagName, x: r.x+r.width/2, y: r.y+r.height/2, cls:(node.className||'').toString().slice(0,60)};
                }
                node = node.parentElement;
              }
              return null;
            }"""
        )
        print("CLICKABLE_ANCESTOR =", clickable)
        if clickable:
            target = clickable
        await page.mouse.move(target["x"], target["y"])
        await page.wait_for_timeout(600)
        await page.mouse.click(target["x"], target["y"])
        await page.wait_for_timeout(4000)
        print("URL_AFTER_CLICK =", page.url)
        print("PROBE_AFTER_CLICK =", json.dumps(await page.evaluate(D.PANEL_PROBE_JS), ensure_ascii=False))
        await page.screenshot(path=str(PROJ/"output"/"dbg_panel_after_click.png"), full_page=False)
    await browser.close(); await pw.stop()

asyncio.run(run())
