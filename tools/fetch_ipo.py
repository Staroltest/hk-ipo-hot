#!/usr/bin/env python3
"""IPO 适配器：把 FinKit / HKEX 的公开 HTML 页转成生成器条目。

生成器只吃 RSS/Atom，但港股 IPO 数据只有公开 HTML 页，没有 RSS。
本适配器由 tools/generate.py 的拉源循环按 sources.json 的
"adapter": "ipo" 标记调用，返回 parse_feed() 同形的条目 dict：

    {title, link, source, source_id, published (datetime 或 None),
     summary, category}

其中 summary 是内部事实串（只进本地 data/store.json，不公开发布；
公开展示的简介一律是 data/zh_draft.json 里自己写的 zh_oneliner /
en_oneliner）。category 由上市日/招股期/暗盘日按生命周期算出，
generate.py 收到后直接采用，之后每轮刷新（IPO 状态会变）。

信源分工：
- FinKit IPO Snapshot（主）：每张卡片含代码、上市日、中英文名、
  招股价区间、行业、招股期文字、保荐人。上市日前后 7 天内的条目
  再拉详情页取准确暗盘日（"Grey market: YYYY-MM-DD"）。
- HKEX 新上市证券表（辅，只验证不单独成条）：近两周的
  New Listing 行，核对 FinKit 的上市日是否一致；结果只打印日志。
- HKIPOx（https://hkipox.com/）：2026-11-18 起 verify 观察期，
  2026-11-19 转正为补全源——两天实测稳定（HTTP 200、三张静态表结构
  未变），8 条首日表现数值与财华社 / Finimize / Finet 周报逐一核对
  一致。首页表提供认购倍数、暗盘涨幅、首日涨幅，按代码挂到 FinKit
  条目上（只补渲染层数字，不改 zh_draft.json 草稿）；招股中的认购
  倍数是招股期内的滚动估计，展示时加“约”字。
- vbkr 華盛証券新股列表（https://www.vbkr.com/ipo/hk/v2/ipo-hk-index）：
  2026-11-21 起 verify 观察期，2026-11-22 转正为验证源——两天实测
  稳定（HTTP 200、三张服务端渲染表结构未变），待上市表提供待上市
  代码与上市日，用于核对 FinKit 的待上市上市日（HKEX 表只覆盖已上
  市，核不到待上市）；只验证不单独成条。介绍上市（如 01821 中旅港
  澳）FinKit 无条目，只进日志不建条目；已遞表 pipeline 表暂不解析。
- TradingView IPO Calendar 只作人工交叉验证，不接入。

版权：只取招股事实（公司名、代码、价格区间、日期、保荐人）与链接，
不取卡片里的介绍段落，不转载招股书正文。
"""

import html
import re
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

FINKIT_LIST = "https://finkit.hk/en/ipo"
FINKIT_DETAIL = "https://finkit.hk/en/ipo/{}"
HKEX_NEWLY = ("https://www.hkex.com.hk/services/trading/securities/"
              "trading-news/newly-listed-securities?sc_lang=en")
VBKR_LIST = "https://www.vbkr.com/ipo/hk/v2/ipo-hk-index"

_HKEX_CACHE = {}
_VBKR_CACHE = {}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
MONTHS.update({m[:3]: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])})

STAGE_ZH = {"subscribing": "招股中", "upcoming": "即将上市",
            "listed-today": "今日上市", "grey-market": "暗盘",
            "debut": "首日表现", "filings": "递表聆讯"}
STAGE_EN = {"subscribing": "Offer open now", "upcoming": "Listing soon",
            "listed-today": "Listing today", "grey-market": "Grey market today",
            "debut": "Debut", "filings": "Filing"}


def _http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _parse_offer_period(text, year_hint=None):
    """'Offer open 16-19 November 2026' / 'Offer open 30 October - 4 November 2026'
    / 'offer period 23 November-1 December 2026'
    / 'Offer open (27 November to 2 December)'（FinKit 2026-11-27 起的新写法：
    括号 + to 连接 + 无年份）-> (start_date, end_date)，解析失败返回 (None, None)。
    无年份时取当年（跨年如 12 月到 1 月则结束年 +1）；快照只保留近 30 天
    的条目，招股窗口总在今天附近，当年假设是安全的。"""
    m = re.search(
        r"(?:Offer open|offer period)\s*\(?\s*(\d{1,2})\s+([A-Za-z]+)\s+"
        r"(?:to|[-–])\s+(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?\s*\)?",
        text, re.I)
    if m:
        d1, mon1, d2, mon2 = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        m1, m2 = MONTHS.get(mon1), MONTHS.get(mon2)
        if m1 and m2:
            y1 = int(m.group(5)) if m.group(5) else (
                year_hint or datetime.now(CST).year)
            y2 = int(m.group(5)) if m.group(5) else (y1 + 1 if m2 < m1 else y1)
            try:
                return date(y1, m1, d1), date(y2, m2, d2)
            except ValueError:
                return None, None
    m = re.search(
        r"(?:Offer open|offer period)\s*\(?\s*(\d{1,2})\s+(?:to|[-–])\s*"
        r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?\s*\)?", text, re.I)
    if m:
        d1, d2, mon = int(m.group(1)), int(m.group(2)), m.group(3)
        month = MONTHS.get(mon)
        if month:
            y = int(m.group(4)) if m.group(4) else (
                year_hint or datetime.now(CST).year)
            try:
                return date(y, month, d1), date(y, month, d2)
            except ValueError:
                return None, None
    m = re.search(r"Offer open\s+(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        d1, d2, mon, y = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
        month = MONTHS.get(mon)
        if month:
            try:
                return date(y, month, d1), date(y, month, d2)
            except ValueError:
                return None, None
    m = re.search(r"Offer open\s+(\d{1,2})\s+([A-Za-z]+)\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if m:
        d1, mon1, d2, mon2, y = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4), int(m.group(5))
        m1, m2 = MONTHS.get(mon1), MONTHS.get(mon2)
        if m1 and m2:
            try:
                return date(y, m1, d1), date(y, m2, d2)
            except ValueError:
                return None, None
    m = re.search(r"offer period\s+(\d{1,2})\s+([A-Za-z]+)\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text, re.I)
    if m:
        d1, mon1, d2, mon2, y = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4), int(m.group(5))
        m1, m2 = MONTHS.get(mon1), MONTHS.get(mon2)
        if m1 and m2:
            try:
                return date(y, m1, d1), date(y, m2, d2)
            except ValueError:
                return None, None
    m = re.search(r"offer period\s+(\d{1,2})\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text, re.I)
    if m:
        d1, d2, mon, y = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4))
        month = MONTHS.get(mon)
        if month:
            try:
                return date(y, month, d1), date(y, month, d2)
            except ValueError:
                return None, None
    return None, None


def _parse_sponsors(text):
    m = re.search(r"(?:Joint sponsors|Sole sponsor)[^:]*:\s*([^.;]{3,180})", text)
    if not m:
        return ""
    return _clean(m.group(1))


def parse_finkit_list(body):
    """解析 FinKit 快照页的 IPO 卡片，返回原始事实 dict 列表。"""
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    cards = re.split(r'<div class="flex flex-col rounded-xl border', text)[1:]
    out = []
    for c in cards:
        m = re.search(r'href="/en/ipo/(\d{5})">(\d{5})</a>', c)
        if not m:
            continue
        code = m.group(1)
        dm = re.search(r'href="/en/ipo/\d{5}">\d{5}</a><span[^>]*>(\d{4}-\d{2}-\d{2})</span>', c)
        listing = None
        if dm:
            try:
                listing = date.fromisoformat(dm.group(1))
            except ValueError:
                listing = None
        nm = re.search(r"<h2[^>]*>(.*?)</h2>", c, re.S)
        zh = re.search(r'<p class="mt-0\.5[^>]*>(.*?)</p>', c, re.S)
        price = re.search(r"Offer Price</span><span[^>]*>(.*?)</span>", c, re.S)
        sector = re.search(r"Sector</span><span[^>]*>(.*?)</span>", c, re.S)
        desc = re.search(r'<p class="mt-2 text-xs[^>]*>(.*?)</p>', c, re.S)
        desc_t = _clean(desc.group(1)) if desc else ""
        offer_start, offer_end = _parse_offer_period(
            desc_t, year_hint=listing.year if listing else None)
        out.append({
            "code": code,
            "name_en": _clean(nm.group(1)) if nm else "",
            "name_zh": _clean(zh.group(1)) if zh else "",
            "listing": listing,
            "price": _clean(price.group(1)) if price else "",
            "sector": _clean(sector.group(1)) if sector else "",
            "offer_start": offer_start,
            "offer_end": offer_end,
            "sponsors": _parse_sponsors(desc_t),
            "grey": None,
        })
    return out


def fetch_grey_dates(items, today):
    """只对上市日在 [today, today+7] 的条目拉详情页取准确暗盘日。"""
    near = [it for it in items
            if it["listing"] and today <= it["listing"] <= today + timedelta(days=7)]
    for it in near[:12]:
        try:
            page = _http_get(FINKIT_DETAIL.format(it["code"]), timeout=20).decode("utf-8", "ignore")
            text = _clean(page)
            m = re.search(r"Grey market:\s*(\d{4}-\d{2}-\d{2})", text)
            if not m:
                m2 = re.search(r"Grey market trading:\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
                if m2 and MONTHS.get(m2.group(2)):
                    it["grey"] = date(int(m2.group(3)), MONTHS[m2.group(2)], int(m2.group(1)))
                    continue
            if m:
                it["grey"] = date.fromisoformat(m.group(1))
        except Exception:
            pass
    return items


def parse_hkex_newly(body):
    """解析 HKEX 新上市证券表：返回 {code: listing_date}（近两周 New Listing 行）。"""
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    out = {}
    for tb in re.findall(r"<table.*?</table>", text, re.S):
        if "Date of Listing / Traded" not in tb:
            continue
        for row in re.findall(r"<tr.*?</tr>", tb, re.S):
            if "New Listing" not in row:
                continue
            cells = [_clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if len(cells) < 3:
                continue
            dm = re.match(r"(\d{2})/(\d{2})/(\d{4})", cells[0])
            code = re.sub(r"\D", "", cells[2])
            if dm and len(code) == 5:
                try:
                    out[code] = date(int(dm.group(3)), int(dm.group(2)), int(dm.group(1)))
                except ValueError:
                    pass
    return out


def verify_hkex(finkit_items):
    """用 HKEX 表核对 FinKit 的上市日；HKEX 取不到时静默跳过。返回 (rows, matched, checked)。"""
    try:
        if "map" not in _HKEX_CACHE:
            _HKEX_CACHE["map"] = parse_hkex_newly(_http_get(HKEX_NEWLY))
        hkex = _HKEX_CACHE["map"]
    except Exception:
        return 0, 0, 0
    checked = matched = 0
    for it in finkit_items:
        if not it["listing"]:
            continue
        d = hkex.get(it["code"])
        if d is None:
            continue
        checked += 1
        if d == it["listing"]:
            matched += 1
    return len(hkex), matched, checked


def parse_vbkr_upcoming(body):
    """解析 vbkr 待上市表：返回 {code: listing_date}。

    待上市表以“暗盤時間”列为标志（可認購表无此列、已遞表无此表头）；
    2026-11-28 修：待上市表为空时（如全部新股仍在认购期）可認購表
    同样含“股票代碼”“上市”两列且同样可核对上市日，改为按表头认表，
    不再要求“暗盤時間”列（已遞表只有“公司名稱”，仍被表头排除）。
    按表头定位“股票代碼”“上市”两列，上市日为 YYYY-MM-DD，“--”跳过。
    """
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    out = {}
    for tb in re.findall(r"<table.*?</table>", text, re.S):
        rows = re.findall(r"<tr.*?</tr>", tb, re.S)
        if not rows:
            continue
        header = [_clean(c) for c in
                  re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", rows[0], re.S)]
        try:
            ci = header.index("股票代碼")
            li = header.index("上市")
        except ValueError:
            continue
        for row in rows[1:]:
            cells = [_clean(c) for c in
                     re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
            if len(cells) <= max(ci, li):
                continue
            code = re.sub(r"\D", "", cells[ci])
            dm = re.match(r"(\d{4})-(\d{2})-(\d{2})", cells[li].strip())
            if dm and len(code) == 5:
                try:
                    out[code] = date(int(dm.group(1)), int(dm.group(2)),
                                   int(dm.group(3)))
                except ValueError:
                    pass
    return out


def verify_vbkr(finkit_items):
    """用 vbkr 待上市表核对 FinKit 的待上市上市日；取不到时静默跳过。
    返回 (rows, matched, checked, extra_codes)：extra 为 vbkr 有、
    FinKit 快照里没有的代码（如介绍上市），只进日志不建条目。"""
    try:
        if "map" not in _VBKR_CACHE:
            _VBKR_CACHE["map"] = parse_vbkr_upcoming(_http_get(VBKR_LIST))
        vbkr = _VBKR_CACHE["map"]
    except Exception:
        return 0, 0, 0, []
    checked = matched = 0
    finkit_codes = set()
    for it in finkit_items:
        finkit_codes.add(it["code"])
        if not it["listing"]:
            continue
        d = vbkr.get(it["code"])
        if d is None:
            continue
        checked += 1
        if d == it["listing"]:
            matched += 1
    extra = sorted(c for c in vbkr if c not in finkit_codes)
    return len(vbkr), matched, checked, extra


def categorize(ipo, today):
    listing = ipo["listing"]
    grey = ipo.get("grey")
    if grey == today:
        return "grey-market"
    if listing == today:
        return "listed-today"
    if listing and today - timedelta(days=7) < listing < today:
        return "debut"
    if ipo.get("offer_start") and ipo.get("offer_end") \
            and ipo["offer_start"] <= today <= ipo["offer_end"]:
        return "subscribing"
    if listing and listing > today:
        return "upcoming"
    if listing and listing < today:
        return "debut"
    return "filings"


def _summary(ipo, category, hkex_ok):
    parts = [STAGE_ZH[category], STAGE_EN[category]]
    if ipo["name_zh"]:
        parts.append(ipo["name_zh"])
    if ipo["price"]:
        parts.append("Offer price " + ipo["price"])
    if ipo["sector"]:
        parts.append("Sector " + ipo["sector"])
    if ipo.get("offer_start") and ipo.get("offer_end"):
        parts.append("Offer open %s to %s" % (ipo["offer_start"].isoformat(),
                                              ipo["offer_end"].isoformat()))
    if ipo["listing"]:
        parts.append("Listing date " + ipo["listing"].isoformat())
    if ipo.get("grey"):
        parts.append("Grey market " + ipo["grey"].isoformat())
    if ipo["sponsors"]:
        parts.append("Sponsors: " + ipo["sponsors"])
    if hkex_ok:
        parts.append("Listing date confirmed against HKEX newly listed table")
    return " · ".join(parts)


HKIPOX_HOME = "https://hkipox.com/"


def parse_hkipox_home(body):
    """解析 HKIPOx 首页三张静态表（今日申购 / 待上市 / 已上市）。

    每行 <tr data-href="/stock/NNNNN">，单元格按 data-label 取值。
    返回 {code: {name, listing(date|None), multiple, grey, first_day}}，
    其中 multiple 如 '351.08x'，grey / first_day 如 '+32.32%'，缺失为 None
    （'—' 也归一化为 None）。只取招股事实与涨跌幅数字，不转载正文。
    """
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    out = {}
    for m in re.finditer(r'<tr data-href="/stock/(\d+)">(.*?)</tr>', text, re.S):
        code, row = m.group(1), m.group(2)

        def cell(label):
            mm = re.search(r'data-label="%s">(.*?)</td>' % re.escape(label), row, re.S)
            return _clean(mm.group(1)) if mm else ""

        listing = None
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", cell("上市日"))
        if dm:
            try:
                listing = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            except ValueError:
                listing = None

        def pct(v):
            v = (v or "").strip()
            return v if v and v != "—" else None

        mult_raw = cell("认购倍数") or None
        if mult_raw in ("0.0x", "0x", "0"):
            # 刚开招股、倍数尚未滚动时 HKIPOx 显示 0.0x：当作缺失，
            # 否则渲染层会写出“认购约 0.0 倍。”
            mult_raw = None
        out[code] = {
            "name": cell("名称"),
            "listing": listing,
            "multiple": None if mult_raw in (None, "—") else mult_raw,
            "grey": pct(cell("暗盘涨幅")),
            "first_day": pct(cell("首日涨幅")),
        }
    return out


_HKIPOX_CACHE = {}


def _load_hkipox(today):
    """拉取并解析 HKIPOx 首页三张表，按代码缓存；失败返回 {}（不中断整轮）。
    招股中的认购倍数是招股期内的滚动估计，展示时加“约”字。"""
    global _HKIPOX_CACHE
    if _HKIPOX_CACHE:
        return _HKIPOX_CACHE
    try:
        perf = parse_hkipox_home(_http_get(HKIPOX_HOME))
        listed = [c for c, v in perf.items() if v["first_day"]]
        live = [c for c, v in perf.items()
                if not v["first_day"] and v["listing"] and v["listing"] >= today]
        print("hkipox: %d rows (%d listed with first-day %%, "
              "%d subscribing/pending)" % (len(perf), len(listed), len(live)))
        _HKIPOX_CACHE = perf
        return perf
    except Exception as e:
        print("hkipox failed: %s" % str(e)[:80])
        return {}


def fetch_ipo_items(src, now):
    """generate.py 的入口：按 sources.json 的源返回条目列表。"""
    today = now.astimezone(CST).date()
    if src.get("id") == "hkipox":
        # HKIPOx：2026-11-19 起为补全源——只解析、不成条；按代码缓存，
        # 由 FinKit 分支挂到条目上（首日涨跌 / 暗盘涨跌 / 认购倍数）。
        _load_hkipox(today)
        return []
    if src.get("role") == "verify":
        # 验证源：只解析、不成条；行数进日志，供核对。
        try:
            if src.get("id") == "vbkr":
                rows = parse_vbkr_upcoming(_http_get(src["url"]))
                print("vbkr verify: %d upcoming rows parsed" % len(rows))
            else:
                rows = parse_hkex_newly(_http_get(src["url"]))
                print("hkex verify: %d newly listed rows parsed" % len(rows))
        except Exception as e:
            print("%s verify failed: %s" % (src.get("id"), str(e)[:80]))
        return []
    body = _http_get(src["url"])
    raw_all = parse_finkit_list(body)
    # ETF 不是新股申购对象（介绍上市、无招股、无暗盘），不在打新日历里收。
    raw = [it for it in raw_all
           if "ETF" not in (it["name_en"] or "").upper()
           and "ETF" not in (it["sector"] or "").upper()]
    n_etf = len(raw_all) - len(raw)
    raw = fetch_grey_dates(raw, today)
    rows, matched, checked = verify_hkex(raw)
    hkex_map = _HKEX_CACHE.get("map", {})
    vrows, vmatched, vchecked, vextra = verify_vbkr(raw)
    items = []
    for ipo in raw:
        cat = categorize(ipo, today)
        hkex_ok = bool(ipo["listing"]) and hkex_map.get(ipo["code"]) == ipo["listing"]
        pub = None
        if ipo["listing"]:
            pub = datetime(ipo["listing"].year, ipo["listing"].month, ipo["listing"].day,
                           tzinfo=CST)
        title = "%s (%s)" % (ipo["name_en"], ipo["code"]) if ipo["name_en"] else ipo["code"]
        items.append({
            "title": title,
            "link": FINKIT_DETAIL.format(ipo["code"]),
            "source": src["name"],
            "source_id": src["id"],
            "published": pub,
            "summary": _summary(ipo, cat, hkex_ok),
            "category": cat,
        })
    print("finkit: %d cards parsed (%d ETFs skipped), hkex table %d rows, "
          "%d/%d listing dates confirmed; vbkr upcoming %d rows, "
          "%d/%d listing dates confirmed%s"
          % (len(raw_all), n_etf, rows, matched, checked,
             vrows, vmatched, vchecked,
             (", vbkr-only: %s" % ",".join(vextra)) if vextra else ""))
    # HKIPOx 补全：首日涨跌 / 暗盘涨跌 / 认购倍数按代码挂到条目上，
    # 只进内部字段（渲染层再决定是否展示），不改公开简介草稿。
    perf = _load_hkipox(today)
    for ipo, item in zip(raw, items):
        p = perf.get(ipo["code"])
        if p:
            item["hkipox"] = {"multiple": p["multiple"], "grey": p["grey"],
                              "first_day": p["first_day"]}
    return items
