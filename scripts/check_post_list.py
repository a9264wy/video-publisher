#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""拉取视频号助手后台的作品列表，核对定时发布的条目。

只读接口（post_list），不产生任何内容。
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
BASE = "https://channels.weixin.qq.com"
OUT = ROOT / "state" / "diag" / "post_list_now.json"


def load_cookies() -> dict[str, str]:
    d = json.loads((ROOT / "state" / "channels_storage.json").read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in d.get("cookies", [])}


def post(path: str, body_extra: dict | None = None) -> dict:
    cks = load_cookies()
    cookie = "; ".join(f"{k}={v}" for k, v in cks.items())
    page_url = f"{BASE}/platform"
    u = (f"{BASE}/{path.lstrip('/')}"
         f"?_aid={uuid.uuid4()}&_rid={uuid.uuid4()}"
         f"&_pageUrl={urllib.parse.quote(page_url, safe='')}")
    body = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": "",
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    if body_extra:
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
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
        }, method="POST")
    with urllib.request.urlopen(req, timeout=25,
                                context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    page = 1
    rows = []
    while page <= 4:
        j = post("/cgi-bin/mmfinderassistant-bin/post/post_list",
                 {"currentPage": page, "pageSize": 20})
        if j.get("errCode") not in (0, None):
            print(f"post_list errCode={j.get('errCode')} {j.get('errMsg')}")
            return 1
        data = j.get("data") or {}
        posts = data.get("list") or data.get("posts") or []
        if not posts:
            break
        for p in posts:
            if isinstance(p, dict):
                raw_desc = p.get("desc") or p.get("description") or p.get("title") or ""
                if isinstance(raw_desc, dict):
                    raw_desc = (raw_desc.get("content") or raw_desc.get("text") or json.dumps(raw_desc, ensure_ascii=False))
                rows.append({
                    "title": str(raw_desc)[:40].replace("\n", " "),
                    "status": p.get("status") or p.get("state"),
                    "createTime": p.get("createTime") or p.get("create_time"),
                    "publishTime": p.get("publishTime") or p.get("expect_publish_time"),
                })
        total = data.get("total") or data.get("totalCount")
        if total and len(rows) >= int(total):
            break
        page += 1

    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"共 {len(rows)} 条作品（已存 {OUT.name}）：\n")
    for r in rows:
        print(f"  [{r.get('status')}] {r.get('title')}  publishTime={r.get('publishTime')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
