# 抖音登录二维码自动抓取系统

自动打开抖音网页版，点击登录按钮，捕获扫码二维码，并通过 Web 页面实时展示。

## 功能

- 🤖 **全自动**: Playwright 无头浏览器自动操作，反检测脚本
- 📸 **二维码提取**: 从登录面板提取 base64 二维码图片
- 🖥️ **Web 展示**: 内置 HTTP 服务器，浏览器打开即可扫码
- 🔄 **自动刷新**: 循环抓取模式，二维码过期自动更新
- 💾 **多格式保存**: 二维码 PNG + 登录面板截图 + 扫码区域截图

## 快速开始

### 1. 单次抓取

```bash
python main.py fetch
# 二维码保存到 ./output/qrcode_latest.png
```

### 2. Web 服务器（推荐）

```bash
python main.py serve
# 浏览器打开 http://localhost:8765 即可查看和扫码
```

### 3. 循环抓取

```bash
python main.py loop --interval 30
# 每 30 秒自动刷新一次二维码
```

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 二维码展示页面 |
| `/api/status` | GET | 当前二维码状态（JSON） |
| `/api/qrcode` | GET | 二维码图片（PNG） |
| `/api/refresh` | POST | 手动触发刷新 |

### 状态示例

```json
{
  "success": true,
  "img_data": "data:image/png;base64,...",
  "size": "512x512",
  "timestamp": 1694000000.0,
  "time_str": "12:00:00"
}
```

## 项目结构

```
douyin_qr_login/
├── crawler.py   # 核心抓取模块（DouyinQRLogin 类）
├── server.py    # Web 展示服务器
├── main.py      # CLI 入口
├── README.md    # 本文档
└── output/      # 输出目录（自动创建）
    ├── qrcode_*.png         # 二维码（带时间戳）
    ├── qrcode_latest.png    # 最新二维码（固定名）
    ├── login_panel_*.png    # 登录面板截图
    └── scan_area_*.png      # 扫码区域截图
```

## 依赖

```bash
pip install playwright
python -m playwright install chromium
```

## 编程接口

```python
import asyncio
from crawler import DouyinQRLogin

async def main():
    async with DouyinQRLogin() as crawler:
        result = await crawler.fetch_qrcode()
        if result.success:
            print(f"二维码尺寸: {result.natural_size}")
            result.save("./output")

asyncio.run(main())
```

## 注意事项

- ⏰ 二维码有效期约 **3-5 分钟**，过期后需重新抓取
- 🔒 抖音有反爬机制，脚本包含反检测处理
- 🌐 需要网络连接访问 `douyin.com`
- 📱 扫码需要手机上安装抖音 App
