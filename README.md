# 港股IPO HOT / HK IPO HOT

港股新股申购日历：招股中、即将上市、今日上市、暗盘与首日表现全收录，
给中文散户打新港股用。只显示标题、一句自己写的简介与原文链接，
不转载招股书正文，也不照搬源站的介绍文字。

Hong Kong IPO subscription calendar for Chinese retail investors:
now subscribing, listing soon, listing today, grey market and debut.
Titles, one-line summaries written in our own words, and links only.

- 主站：https://Staroltest.github.io/hk-ipo-hot
- 镜像：https://hk-ipo-hot.pages.dev
- MCP：https://hk-ipo-hot.pages.dev/mcp

## 信源

- FinKit IPO Snapshot（https://finkit.hk/en/ipo，公开页）：主源，代码、
  上市日、中英文名、招股价区间、行业、招股期、保荐人；上市日前后 7 天内
  的条目再拉详情页取准确暗盘日。
- 港交所新上市证券表（公开页）：只作上市日交叉验证，不单独成条。
- TradingView IPO Calendar（https://www.tradingview.com/ipo-calendar/）：
  只作人工交叉验证，不接入。
- ETF 不收（介绍上市、无招股、无暗盘，不是申购对象）。

HKEX 没有公开 API，解析公开 HTML。适配器在 `tools/fetch_ipo.py`，
生成器只吃 RSS/Atom 的限制由它解决；`tools/generate.py` 里只有三处
最小补丁：适配器分发、IPO 条目状态刷新、IPO 关键词中英标签映射。

## 风险提示

新股有破发可能、不是投资建议。New listings can debut below the offer
price. This is not investment advice.

## 运行

```bash
python3 tools/generate.py            # 每半小时一轮
python3 tools/generate.py --daily    # 06:00（北京时间）那轮出日报
```

更新只走定时任务，不启用 GitHub Actions，不推送 `.github/workflows/*`。
