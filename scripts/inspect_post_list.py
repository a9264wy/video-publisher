#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：用 SPA 客户端路由进入「视频管理」列表，核对定时/草稿状态。

直接 ``goto`` 深链会被重定向回首页，所以走 ``history.pushState`` + ``popstate``
（和进入「发表动态」页同一招）。逐个候选路由试，找到能渲染出内容列表的那个。

⚠️ 只读：不修改、不删除、不提交任何内容。

用法：
    python scripts/inspect_post_list.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

ROUTES = [
    "/platform/post/list",
    "/platform/post/published",
    "/platform/post/draft",
    "/platform/content/post/list",
    "/platform/post",
]

HAS_TABLE = r"""() => {
  const rows = document.querySelectorAll(
    '.post-list, .post-item, [class*=post-list], [class*=video-list], table tbody tr, li[class*=item]'
  );
  return {rows: rows.length, bodyLen: document.body ? document.body.innerHTML.length : 0};
}"""


def main() -> int:
    cfg = Config.load("config.json")
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {"tried": []}
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
                  timeout=60_000)
        for _ in range(15):
            page.wait_for_timeout(1000)
            if backend._is_logged_in(page):
                break
        page.wait_for_timeout(4000)

        for route in ROUTES:
            try:
                page.evaluate(
                    "(r) => { history.pushState({}, '', r);"
                    " window.dispatchEvent(new PopStateEvent('popstate')); }",
                    route,
                )
            except Exception as exc:
                print(f"{route}: pushState 失败 {str(exc)[:120]}")
                continue
            page.wait_for_timeout(6000)
            text = backend._page_text(page)
            urls = [f.url for f in backend._frames(page)]
            info = {"route": route, "url": page.url, "frames": urls,
                    "text_len": len(text), "head": text[:400]}
            try:
                info["probe"] = page.evaluate(HAS_TABLE)
            except Exception:
                info["probe"] = None
            report["tried"].append(info)
            print(f"\n{'='*68}\n{route}\n{'='*68}")
            print(f"  url={page.url}")
            print(f"  frames={urls}")
            print(f"  text_len={len(text)} probe={info['probe']}")
            if len(text) > 300:
                print(f"  ---- 文案 ----\n{text[:1200]}")
                report["best"] = info
                shot = ROOT / "state" / "post_list.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"  截图：{shot}")

        out = ROOT / "state" / "inspect_post_list.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
