#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：把「视频管理 / 草稿箱 / 已发表」等列表逐个走一遍，看内容落在哪个桶里。

背景：定时提交返回 success 后，视频管理里竟然是「视频 (0) 你还没有发表过视频」。
必须搞清楚：定时内容到底进了草稿箱、还是进了别的「定时/待发表」列表，
还是根本没提交上去。

⚠️ 只读：只导航 + 读文案 + 截图。

用法：
    python scripts/inspect_buckets.py
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
    "/platform/post/draft",
    "/platform/post/published",
]

FRAME_TEXT = r"""() => {
  let t = '';
  try { t = (document.body ? document.body.innerText : '').trim(); } catch (e) {}
  return {url: location.href, len: t.length, text: t.slice(0, 1500)};
}"""

# 找常见的「定时」入口/Tab
TABS = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll('div,span,a,li,button')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 16) continue;
    if (!/定时|待发表|已发表|草稿|审核|全部/.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    out.push({tag: el.tagName, txt: t, w: Math.round(r.width), h: Math.round(r.height),
              cls: String(el.getAttribute('class') || '').slice(0, 60)});
  }
  return out.slice(0, 30);
}"""


def main() -> int:
    cfg = Config.load("config.json")
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {}
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
            page.evaluate(
                "(r) => { history.pushState({}, '', r);"
                " window.dispatchEvent(new PopStateEvent('popstate')); }",
                route,
            )
            page.wait_for_timeout(8000)
            print(f"\n{'='*72}\n{route}\n{'='*72}")
            bucket = {"route": route, "frames": []}
            for i, f in enumerate(backend._frames(page)):
                try:
                    info = f.evaluate(FRAME_TEXT)
                except Exception as exc:
                    info = {"url": "?", "error": str(exc)[:150]}
                info["index"] = i
                if info.get("len"):
                    bucket["frames"].append(info)
                    print(f"  frame[{i}] {info.get('url')} len={info['len']}")
                    print("  " + info["text"][:900].replace("\n", "\n  "))
                # 找 tab
                try:
                    tabs = f.evaluate(TABS)
                except Exception:
                    tabs = []
                if tabs:
                    bucket.setdefault("tabs", []).extend(tabs)
                    print(f"  frame[{i}] 可疑 tab/入口：")
                    for t in tabs:
                        print(f"     {t}")
            report[route] = bucket

            shot = ROOT / "state" / f"bucket_{route.strip('/').replace('/', '_')}.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"  截图：{shot}")

        out = ROOT / "state" / "inspect_buckets.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
