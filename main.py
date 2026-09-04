#!/usr/bin/env python3
"""
抖音登录二维码自动抓取 - CLI 入口

用法:
    # 单次抓取二维码（保存到 ./output/）
    python main.py fetch

    # 启动 Web 服务器（浏览器访问 http://localhost:8765 扫码）
    python main.py serve

    # 循环抓取（每 30 秒刷新一次）
    python main.py loop --interval 30
"""
import argparse
import asyncio
import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from crawler import DouyinQRLogin


async def cmd_fetch(args):
    """单次抓取"""
    print("🚀 正在启动浏览器...")
    async with DouyinQRLogin(output_dir=args.output, headless=not args.visible) as crawler:
        print("📄 正在打开抖音精选页...")
        result = await crawler.fetch_qrcode(url=args.url, save=True)
        if result.success:
            print(f"\n✅ 二维码抓取成功!")
            print(f"   📐 尺寸: {result.natural_size[0]}×{result.natural_size[1]}")
            paths = result.save(args.output)
            print(f"   💾 二维码: {paths.get('qrcode', 'N/A')}")
            print(f"   📌 最新:   {paths.get('latest', 'N/A')}")
            if result.panel_screenshot:
                print(f"   🖼️  面板截图: {paths.get('panel', 'N/A')}")
            if result.scan_area_screenshot:
                print(f"   📱 扫码区域: {paths.get('scan_area', 'N/A')}")
        else:
            print(f"\n❌ 抓取失败: {result.error}")
            sys.exit(1)


async def cmd_loop(args):
    """循环抓取"""
    print(f"🔄 循环抓取模式 (间隔 {args.interval}s)")
    async with DouyinQRLogin(output_dir=args.output, headless=not args.visible) as crawler:
        count = 0
        while True:
            count += 1
            print(f"\n── 第 {count} 次抓取 ──")
            result = await crawler.fetch_qrcode(url=args.url, save=True)
            if result.success:
                print(f"✅ 二维码已更新 ({result.natural_size[0]}×{result.natural_size[1]})")
                paths = result.save(args.output)
                latest = paths.get("latest", "")
                if latest:
                    print(f"   保存: {latest}")
            else:
                print(f"❌ 失败: {result.error}")

            if args.json:
                print(json.dumps({
                    "success": result.success,
                    "size": list(result.natural_size),
                    "error": result.error or None,
                    "timestamp": result.timestamp,
                }, ensure_ascii=False))

            if args.count and count >= args.count:
                print(f"\n已完成 {count} 次抓取")
                break

            print(f"⏳ {args.interval}s 后下次抓取...")
            await asyncio.sleep(args.interval)


def cmd_server(args):
    """启动 Web 服务器"""
    from server import run_server
    run_server(host=args.host, port=args.port)


def main():
    parser = argparse.ArgumentParser(
        description="抖音登录二维码自动抓取系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="子命令")

    # fetch
    p_fetch = sub.add_parser("fetch", help="单次抓取二维码")
    p_fetch.add_argument("--url", default="https://www.douyin.com/jingxuan")
    p_fetch.add_argument("--output", "-o", default="./output")
    p_fetch.add_argument("--visible", action="store_true", help="显示浏览器窗口（调试用）")

    # loop
    p_loop = sub.add_parser("loop", help="循环抓取二维码")
    p_loop.add_argument("--url", default="https://www.douyin.com/jingxuan")
    p_loop.add_argument("--output", "-o", default="./output")
    p_loop.add_argument("--interval", "-i", type=float, default=30.0, help="抓取间隔(秒)")
    p_loop.add_argument("--count", "-n", type=int, default=0, help="抓取次数(0=无限)")
    p_loop.add_argument("--json", action="store_true", help="输出 JSON 格式")
    p_loop.add_argument("--visible", action="store_true", help="显示浏览器窗口")

    # serve
    p_serve = sub.add_parser("serve", help="启动 Web 展示服务器")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", "-p", type=int, default=8765)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "fetch":
        asyncio.run(cmd_fetch(args))
    elif args.command == "loop":
        asyncio.run(cmd_loop(args))
    elif args.command == "serve":
        cmd_server(args)


if __name__ == "__main__":
    main()
