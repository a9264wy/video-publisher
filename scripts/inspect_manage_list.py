#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查「内容管理」列表页：我们的 10 条定时作品长什么样、删除入口在哪。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "manage_list.json"

TITLES = ["风是从哪来的", "海水为什么是咸的", "地震是怎么来的", "火山为什么爆发",
          "冰为什么浮在水上", "回声是怎么来的", "磁铁为什么吸铁", "影子是怎么来的",
          "叶子为什么是绿的", "潮汐是怎么来的"]


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    try:
        page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
                  timeout=60_000)
        page.wait_for_timeout(5_000)
        # 客户端路由进「内容管理」（主内容列表）
        page.evaluate("""() => {
            history.pushState({}, '', '/platform/post/list');
            window.dispatchEvent(new PopStateEvent('popstate'));
        }""")
        page.wait_for_timeout(6_000)
        page.screenshot(path=str(ROOT / "state" / "diag" / "manage_list.png"))

        cards = page.evaluate("""() => {
            const out = [];
            const TITLES = ['风是从哪来的','海水为什么是咸的','地震是怎么来的','火山为什么爆发','冰为什么浮在水上','回声是怎么来的','磁铁为什么吸铁','影子是怎么来的','叶子为什么是绿的','潮汐是怎么来的'];
            const grab = (root, tag) => {
                for (const el of root.querySelectorAll('.post-feed-item')) {
                    const t = (el.innerText || '').trim();
                    const r = el.getBoundingClientRect();
                    if (!(r.width > 100 && r.height > 40)) continue;
                    const leaves = [];
                    for (const x of el.querySelectorAll('*')) {
                        if (x.children.length) continue;
                        const xt = (x.innerText || '').trim();
                        const xr = x.getBoundingClientRect();
                        if (xt && xt.length <= 14 && xr.width > 0 && xr.height > 0) {
                            leaves.push({tag: x.tagName.toLowerCase(), cls: String(x.className||'').slice(0,70), text: xt});
                        }
                    }
                    out.push({
                        frame: tag,
                        cls: String(el.className || '').slice(0, 80),
                        text: t.slice(0, 260).replace(/\\n/g, ' | '),
                        leaves: leaves.slice(0, 25),
                    });
                }
            };
            grab(document, 'main');
            for (const f of document.querySelectorAll('iframe')) {
                try { grab(f.contentDocument, 'iframe:' + f.src.slice(0, 50)); } catch (e) {}
            }
            for (const h of document.querySelectorAll('*')) {
                if (h.shadowRoot) { try { grab(h.shadowRoot, 'shadow:' + h.tagName.toLowerCase()); } catch (e) {} }
            }
            return out.slice(0, 40);
        }""")
        OUT.write_text(json.dumps(cards, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"命中 {len(cards)} 个卡片 -> {OUT}")
        seen = set()
        for c in cards:
            if c["frame"] != "iframe:https://channels.weixin.qq.com/empty.html":
                continue
            key = c["text"][:10]
            if key in seen:
                continue
            seen.add(key)
            print(f"\n== {c['text'][:60]!r}")
            for x in c["leaves"]:
                print(f"   <{x['tag']} {x['cls'][:55]}> {x['text']!r}")
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
