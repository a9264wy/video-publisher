#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""分页拉全「内容管理」作品，核验定时未发表清单（只读）。

为什么需要它：管理页 UI 与拦截接口默认只看得到**第一页**（20 条），
新批次发布后老条目会被挤到第二页，看起来像「消失了」。这个脚本直接
HTTP 翻页拉全，用 status==1 判定定时未发表，并输出定时时间。

用法：
    .venv/Scripts/python.exe scripts/verify_all_scheduled.py
"""

from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://channels.weixin.qq.com"
OUT = ROOT / "state" / "diag" / "all_scheduled.json"
PAGE_SIZE = 50
MAX_PAGES = 10


def load_cookies() -> dict[str, str]:
    d = json.loads((ROOT / "state" / "channels_storage.json").read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in d.get("cookies", [])}


def post(path: str, body_extra: dict) -> dict:
    cks = load_cookies()
    cookie = "; ".join(f"{k}={v}" for k, v in cks.items())
    page_url = f"{BASE}/platform"
    u = (f"{BASE}/{path.lstrip('/')}?_aid={uuid.uuid4()}&_rid={uuid.uuid4()}"
         f"&_pageUrl={urllib.parse.quote(page_url, safe='')}")
    body = {
        "timestamp": str(int(time.time() * 1000)),
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    body.update(body_extra)
    req = urllib.request.Request(
        u, data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Referer": page_url,
            "Origin": BASE,
            "x-wechat-uin": cks.get("wxuin", ""),
            "Cookie": cookie,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        }, method="POST")
    with urllib.request.urlopen(req, timeout=25, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    seen: dict[str, dict] = {}
    total_count = None
    for page in range(1, MAX_PAGES + 1):
        j = post("/cgi-bin/mmfinderassistant-bin/post/post_list",
                 {"currentPage": page, "pageSize": PAGE_SIZE})
        if j.get("errCode") not in (0, None):
            print(f"[{page}] errCode={j.get('errCode')} {j.get('errMsg')}")
            break
        data = j.get("data") or {}
        lst = data.get("list") or []
        total_count = data.get("totalCount") or total_count
        if not lst:
            break
        for p in lst:
            if not isinstance(p, dict):
                continue
            oid = p.get("objectId") or f"__noid_{len(seen)}"
            st = (p.get("desc") or {}).get("shortTitle") or []
            title = (st[0].get("shortTitle") if st and isinstance(st[0], dict) else "") or ""
            seen.setdefault(oid, {
                "objectId": oid,
                "title": title,
                "status": p.get("status"),
                "createTime": p.get("createTime"),
                # effectiveTime 才是「定时发表时间」；publishTime 不存在
                "publishTime": p.get("effectiveTime"),
            })
        print(f"[{page}] 本页 {len(lst)} 条，累计去重 {len(seen)} 条"
              + (f"，接口 totalCount={total_count}" if total_count else ""))
        if len(lst) < PAGE_SIZE:
            break

    rows = sorted(seen.values(), key=lambda r: (r.get("publishTime") or 0, r["title"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    sched = [r for r in rows if r.get("status") == 1]
    print(f"\n== 作品总数 {len(rows)}（接口 totalCount={total_count}），"
          f"其中定时未发表(status=1) {len(sched)} 条 ==")
    for r in sched:
        pt = r.get("publishTime")
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(pt)) if isinstance(pt, (int, float)) and pt else "?"
        print(f"  {when}  {r['title']}")
    print(f"\n（明细已存 {OUT}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
