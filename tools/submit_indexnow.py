#!/usr/bin/env python3
"""IndexNow 提交（hk-ipo-hot 试点，2026-11-22 老板批准，muse-5 协调）。

从工作目录 sitemap.xml 取全部 canonical URL（sitemap 只列 pages.dev 规范 host，
不另推 github.io），与上次已提交集合（state 文件）比对，只提交新增 URL；
首次运行 state 为空，即提交全部。200/202 = 对方已收到列表，不保证收录。

提交方式：GET 单条提交（IndexNow 协议支持 url + key 参数）。
2026-11-22 实测：POST 路由被堵——脚本与 curl 各重试多次，两端 POST 均 30 秒
读超时；同日改走 GET 后，bing.com/indexnow 与 api.indexnow.org/indexnow
对全部 83 条 URL 均返回 202（约 2 秒/条，并发 8），首批提交完成。
所以本脚本用 GET，不再用 POST。

用法（在 ~/workspace/hk-ipo-hot 下跑）：
    python3 tools/submit_indexnow.py            # 增量提交
    python3 tools/submit_indexnow.py --all       # 全量重提
    python3 tools/submit_indexnow.py --dry-run   # 只打印将提交的 URL，不发请求

前置：key 文件 <key>.txt 必须已上线（generate.py 每轮写到站根，随部署发布），
提交前脚本先校验 https://hk-ipo-hot.pages.dev/<key>.txt 返回 200。
state 默认：/home/hatch/workspace/hk-ipo-hot-work/indexnow/submitted.json（本机专用，不进仓库）。
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STATE = "/home/hatch/workspace/hk-ipo-hot-work/indexnow/submitted.json"
ENDPOINTS = [
    "https://api.indexnow.org/indexnow",
    "https://www.bing.com/indexnow",
]


def _load_config():
    with open(os.path.join(WORK_DIR, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def _sitemap_urls(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    urls = re.findall(r"<loc>([^<]+)</loc>", text)
    # 只推规范 host（pages.dev）；sitemap 本来就只列它，这里再守一道
    urls = [u for u in urls if u.startswith("https://hk-ipo-hot.pages.dev/")]
    return sorted(set(urls))


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def _save_state(path, urls):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(urls), f, indent=1, ensure_ascii=False)


def _get_submit(url, key, host):
    """GET 单条提交到两端；任一端返回 200/202 即算收到。返回 (url, codes, ok)。"""
    codes = {}
    for ep in ENDPOINTS:
        params = {"url": url, "key": key}
        if "api.indexnow.org" in ep:
            params["host"] = host
        full = ep + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            full,
            headers={"User-Agent": "hk-ipo-hotBot/1.0 (+https://hk-ipo-hot.pages.dev)"},
            method="GET")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                codes[ep] = resp.status
        except urllib.error.HTTPError as e:
            codes[ep] = e.code
        except Exception as e:
            print(f"  {ep} {url} 请求失败：{e}")
            codes[ep] = None
    ok = any(c in (200, 202) for c in codes.values())
    return url, codes, ok


def main():
    args = sys.argv[1:]
    force_all = "--all" in args
    dry_run = "--dry-run" in args
    state_path = DEFAULT_STATE
    sitemap_path = os.path.join(WORK_DIR, "sitemap.xml")
    for i, a in enumerate(args):
        if a == "--state" and i + 1 < len(args):
            state_path = args[i + 1]
        if a == "--sitemap" and i + 1 < len(args):
            sitemap_path = args[i + 1]

    cfg = _load_config()
    site = cfg.get("site", {})
    key = (site.get("indexnow_key") or "").strip()
    if not key:
        print("config.json site.indexnow_key 未填，无法提交")
        return 1
    host = site.get("url", "https://hk-ipo-hot.pages.dev").replace("https://", "").rstrip("/")
    key_location = f"https://{host}/{key}.txt"

    # 前置：key 文件必须已上线（urllib 默认 UA 会被 Cloudflare 挡 403，带上本站爬虫 UA）
    # 某些受限环境（如后台准备阶段的出口代理）会挡 urllib 访问 pages.dev，
    # 此时改用 curl 再校验一次；两路都失败才认定 key 文件未上线。
    _ua = "hk-ipo-hotBot/1.0 (+https://hk-ipo-hot.pages.dev)"
    _key_ok = False
    try:
        _ua_req = urllib.request.Request(key_location, headers={"User-Agent": _ua})
        with urllib.request.urlopen(_ua_req, timeout=15) as resp:
            _key_ok = (resp.status == 200)
    except Exception as e:
        print(f"key 文件 urllib 校验失败（{e}），改用 curl 再试")
        try:
            import subprocess
            _r = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "15", "-A", _ua, key_location],
                capture_output=True, text=True, timeout=20)
            _key_ok = (_r.stdout.strip() == "200")
        except Exception as e2:
            print(f"key 文件 curl 校验也失败（{e2}）")
    if not _key_ok:
        print(f"key 文件未上线：{key_location}，先部署再提交")
        return 1
    print(f"key 文件在线：{key_location} 200")

    urls = _sitemap_urls(sitemap_path)
    print(f"sitemap 共 {len(urls)} 个 URL")
    submitted = _load_state(state_path)
    new_urls = urls if force_all else [u for u in urls if u not in submitted]
    if not new_urls:
        print("没有新增 URL，本轮不提交")
        return 0
    print(f"新增 {len(new_urls)} 个 URL，准备提交")

    if dry_run:
        for u in new_urls:
            print("  " + u)
        print("dry-run，未发送")
        return 0

    # GET 单条提交（POST 路由被堵，见文件头说明）；并发 8，每条约 2 秒
    done, failed = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_get_submit, u, key, host): u for u in new_urls}
        for fut in as_completed(futs):
            url, codes, ok = fut.result()
            (done if ok else failed).append(url)
            if not ok:
                print(f"失败 {url}: {codes}")
    print(f"成功 {len(done)} / {len(new_urls)}，失败 {len(failed)}")
    if done:
        _save_state(state_path, submitted | set(done))
        print(f"已提交 {len(done)} 个 URL，state 已更新（{state_path}）")
        print("注意：200/202 只表示对方已收到，不保证收录；看接下来 1-2 天的流量日报。")
    if failed:
        print("部分 URL 未提交成功，下一轮会重试未提交的")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
