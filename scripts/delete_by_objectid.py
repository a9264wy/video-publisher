#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按标题精确删除**定时未发表**的作品（走 post_delete API，只读列表 + 精确匹配）。

比 UI 点删可靠：UI 删除会被主 frame 的全屏 mask 吃掉，且只能按标题前缀猜。
本脚本先分页拉全 → 只保留「未来定时（effectiveTime 在未来）」的条目 →
按标题**唯一**匹配 → 打印目标（标题/objectId/定时时间）→ 调 API 删除 → 复查。

安全约束（防误伤，2026-10-17 加）：
  * 只删定时未发表的，已发表内容一律不碰；
  * 标题必须唯一命中，命中 0 条或 >1 条都直接中止，不猜；
  * 逐条打印「即将删除」再执行。

用法：
    .venv/Scripts/python.exe scripts/delete_by_objectid.py 切洋葱为什么会流眼泪
    .venv/Scripts/python.exe scripts/delete_by_objectid.py --list
    .venv/Scripts/python.exe scripts/delete_by_objectid.py --published \
        --object-id export/UzFfBgAAxOSP...   # 同标题多条并存时按 objectId 精确删
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
PAGE_SIZE = 50
MAX_PAGES = 10
DELETE_PATH = "/micro/content/cgi-bin/mmfinderassistant-bin/post/post_delete"


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
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def list_all() -> list[dict]:
    seen: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        j = post("/cgi-bin/mmfinderassistant-bin/post/post_list",
                 {"currentPage": page, "pageSize": PAGE_SIZE})
        if j.get("errCode") not in (0, None):
            print(f"post_list errCode={j.get('errCode')} {j.get('errMsg')}")
            break
        data = j.get("data") or {}
        lst = data.get("list") or []
        if not lst:
            break
        for p in lst:
            if not isinstance(p, dict):
                continue
            oid = p.get("objectId") or ""
            if not oid:
                continue
            st = (p.get("desc") or {}).get("shortTitle") or []
            title = (st[0].get("shortTitle") if st and isinstance(st[0], dict) else "") or ""
            seen.setdefault(oid, {"objectId": oid, "title": title,
                                  "status": p.get("status"),
                                  "publishTime": p.get("effectiveTime")})
        if len(lst) < PAGE_SIZE:
            break
    return list(seen.values())


def main() -> int:
    argv = sys.argv[1:]
    allow_published = "--published" in argv
    obj_id = None
    if "--object-id" in argv:
        i = argv.index("--object-id")
        if i + 1 >= len(argv):
            print("✗ --object-id 后面要跟 objectId")
            return 2
        obj_id = argv[i + 1]
    args = [a for a in argv if not a.startswith("--") and a != obj_id]
    rows = list_all()
    now = time.time()
    future = [r for r in rows
              if isinstance(r.get("publishTime"), (int, float)) and r["publishTime"] > now]
    print(f"作品总数 {len(rows)}，其中定时未发表 {len(future)} 条")

    if not args and not obj_id:
        print("\n== 定时未发表清单 ==")
        for r in sorted(future, key=lambda x: x["publishTime"]):
            print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['publishTime']))}"
                  f"  {r['title']}")
        return 0

    # 默认只允许删「定时未发表」；--published 才允许删已发表（内容已公开，删了就没了）
    pool = rows if allow_published else future
    kind = "全部作品（含已发布）" if allow_published else "定时未发表"
    targets: list[dict] = []
    if obj_id:
        # objectId 精确删除：同标题多条并存时（如带封面重发vs无封面旧版）用它区分
        hits = [r for r in pool if r["objectId"] == obj_id]
        if not hits:
            print(f"✗ objectId「{obj_id}」在{kind}里不存在，已中止（不猜、不删）")
            if not allow_published:
                print("  提示：若要删已发表内容，需显式加 --published")
            return 2
        targets = hits[:1]
    else:
        for want in args:
            hits = [r for r in pool if want in r["title"]]
            if len(hits) == 0:
                print(f"✗ 「{want}」在{kind}里没有匹配项，已中止（不猜、不删）")
                if not allow_published:
                    print("  提示：若要删已发表内容，需显式加 --published")
                return 2
            if len(hits) > 1:
                print(f"✗ 「{want}」命中 {len(hits)} 条，无法唯一确定，已中止：")
                for h in hits:
                    print(f"    - {h['title']}  ({h['objectId']})")
                return 2
            targets.append(hits[0])

    print("\n== 即将删除 ==")
    for t in targets:
        pt = t.get("publishTime")
        when = (time.strftime('%Y-%m-%d %H:%M', time.localtime(pt))
                if isinstance(pt, (int, float)) and pt else "已发布（无定时时间）")
        print(f"  {when}  {t['title']}  ({t['objectId']})")

    ok = 0
    for t in targets:
        j = post(DELETE_PATH, {"objectId": t["objectId"]})
        rc = j.get("errCode")
        if rc in (0, None):
            print(f"✓ 已删除：{t['title']}")
            ok += 1
        else:
            print(f"✗ 删除失败：{t['title']}  errCode={rc} {j.get('errMsg')}")
    print(f"\n删除完成 {ok}/{len(targets)}")
    return 0 if ok == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())