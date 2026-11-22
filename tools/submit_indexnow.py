#!/usr/bin/env python3
"""IndexNow 提交（hk-ipo-hot 试点，2026-11-22 老板批准，muse-5 协调）。

从工作目录 sitemap.xml 取全部 canonical URL（sitemap 只列 pages.dev 规范 host，
不另推 github.io），与上次已提交集合（state 文件）比对，只提交新增 URL；
首次运行 state 为空，即提交全部。200 = 对方已收到列表，不保证收录。

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
import urllib.request
import urllib.error

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


def _post(url, payload_bytes):
    req = urllib.request.Request(
        url, data=payload_bytes,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # 网络错误等
        print(f"  {url} 请求失败：{e}")
        return None


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

    # 前置：key 文件必须已上线
    try:
        with urllib.request.urlopen(key_location, timeout=15) as resp:
            if resp.status != 200:
                print(f"key 文件未上线：{key_location} 返回 {resp.status}，先部署再提交")
                return 1
    except Exception as e:
        print(f"key 文件校验失败：{key_location} 无法访问（{e}），先部署再提交")
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

    payload = {
        "host": host,
        "key": key,
        "keyLocation": key_location,
        "urlList": new_urls,
    }
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if dry_run:
        for u in new_urls:
            print("  " + u)
        print("dry-run，未发送")
        return 0

    codes = {}
    for ep in ENDPOINTS:
        code = _post(ep, payload_bytes)
        codes[ep] = code
        print(f"{ep} -> HTTP {code}")
    if any(c == 200 for c in codes.values()):
        _save_state(state_path, set(urls))
        print(f"已提交 {len(new_urls)} 个 URL，state 已更新（{state_path}）")
        print("注意：200 只表示对方已收到，不保证收录；看接下来 1-2 天的流量日报。")
        return 0
    print("两端都未返回 200，state 未更新，下一轮会重试")
    return 1


if __name__ == "__main__":
    sys.exit(main())
