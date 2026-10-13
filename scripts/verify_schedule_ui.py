#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读核验：列出视频号助手管理页所有「定时未发表」卡片及其发表时间。

不点击、不删除、不提交任何内容，纯读取。
输出与 state/diag/verify_schedule_ui.json 一致，便于留档。

用法：
    .venv/Scripts/python.exe scripts/verify_schedule_ui.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "verify_schedule_ui.json"


def open_list_page(be) -> None:
    page = be._page
    page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
              timeout=60_000)
    page.wait_for_timeout(5_000)
    page.evaluate("""() => {
        history.pushState({}, '', '/platform/post/list');
        window.dispatchEvent(new PopStateEvent('popstate'));
    }""")
    page.wait_for_timeout(6_000)


def collect_cards(be) -> list[dict]:
    """跨 frame 收集 .post-feed-item 卡片文本（iframe 活副本优先，去重）。"""
    seen: set[str] = set()
    out: list[dict] = []
    frames = [f for f in be._frames(be._page) if f is not be._page.main_frame] + [be._page.main_frame]
    for f in frames:
        try:
            cards = f.locator(".post-feed-item")
            n = min(cards.count(), 60)
        except Exception:
            continue
        for i in range(n):
            cand = cards.nth(i)
            try:
                txt = (cand.inner_text() or "").strip()
            except Exception:
                continue
            if not txt or txt in seen:
                continue
            seen.add(txt)
            first = txt.splitlines()[0].strip() if txt.splitlines() else ""
            scheduled = "将于" in txt
            # 提取「将于XXXX年XX月XX日 XX:XX发表」片段
            when = ""
            for line in txt.splitlines():
                line = line.strip()
                if line.startswith("将于"):
                    when = line
                    break
            out.append({"title": first, "scheduled": scheduled, "when": when,
                        "raw": txt[:200]})
    return out


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch()
    try:
        open_list_page(be)
        cards = collect_cards(be)
        sched = [c for c in cards if c["scheduled"]]
        OUT.write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"== 管理页卡片共 {len(cards)} 条，其中定时未发表 {len(sched)} 条 ==")
        for c in sorted(sched, key=lambda x: (x["when"], x["title"])):
            print(f"  {c['when']:<28s} {c['title']}")
        print(f"\n（明细已存 {OUT.relative_to(ROOT)}）")
        return 0
    finally:
        try:
            be.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
