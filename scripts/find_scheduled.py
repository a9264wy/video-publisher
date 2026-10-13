#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""列出「内容管理」里定时未发表的作品（objectId + 标题 + 定时时间）。

做法：playwright 打开 /platform/post/list，**拦截页面自己发出的
post_list 响应** —— 那份数据里带 objectId，正是 post_delete 需要的。
比 UI 点删可靠得多（UI 删除被主 frame 全屏 mask 吃掉，已弃用）。

只读：不删任何东西。删除请配合 scripts/delete_scheduled_api.py。

用法：
    .venv/Scripts/python.exe scripts/find_scheduled.py            # 全部
    .venv/Scripts/python.exe scripts/find_scheduled.py 潮汐       # 标题过滤
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "scheduled_posts.json"


def main() -> int:
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    captured: list[dict] = []

    def on_response(resp):
        if "post/post_list" not in resp.url:
            return
        try:
            j = resp.json()
        except Exception:
            return
        lst = (j.get("data") or {}).get("list") or []
        for p in lst:
            if isinstance(p, dict):
                captured.append(p)

    page.on("response", on_response)
    try:
        page.goto("https://channels.weixin.qq.com/platform",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        page.evaluate("""() => {
            history.pushState({}, '', '/platform/post/list');
            window.dispatchEvent(new PopStateEvent('popstate'));
        }""")
        # 等列表接口回来
        for _ in range(20):
            page.wait_for_timeout(1_000)
            if captured:
                break
        page.wait_for_timeout(2_000)

        rows = []
        for p in captured:
            d = p.get("desc") or {}
            st = d.get("shortTitle") or []
            title = (st[0].get("shortTitle") if st and isinstance(st[0], dict) else "") or ""
            rows.append({
                "objectId": p.get("objectId") or "",
                "title": title,
                "status": p.get("status"),
                "createTime": p.get("createTime"),
            })
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"列表接口返回 {len(rows)} 条（已存 {OUT.name}）：")
        for r in rows:
            mark = "★" if (not prefix or r["title"].startswith(prefix)) else " "
            print(f"  {mark} [{r['status']}] {r['title'][:24]:24s} id={r['objectId'][-12:]}")
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
