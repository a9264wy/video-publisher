#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探查发布页里「活动 / 参与」相关 UI 与接口。

回答两个问题：
  1. 发表动态表单里有没有「参加活动」入口（文案、选择器、点击后发生什么）
  2. 有没有 activity 相关的 XHR（活动列表/搜索）

只读：进入发布页、枚举元素、截屏；不投递文件、不提交。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "activity_ui.json"

ACTIVITY_JS = r"""() => {
  const out = [];
  const walk = (root, frameTag) => {
    for (const el of root.querySelectorAll('*')) {
      const t = (el.textContent || '').trim();
      if (!t || t.length > 40) continue;
      if (/活动|参与|挑战|话题/.test(t)) {
        const r = el.getBoundingClientRect();
        out.push({
          frame: frameTag,
          tag: el.tagName.toLowerCase(),
          cls: (el.className && el.className.baseVal !== undefined) ? String(el.className.baseVal) : String(el.className || ''),
          text: t.slice(0, 40),
          visible: r.width > 0 && r.height > 0,
          rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
        });
      }
    }
  };
  walk(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { walk(f.contentDocument, 'iframe:' + (f.src || '').slice(0, 60)); } catch (e) {}
  }
  // shadow roots
  for (const host of document.querySelectorAll('*')) {
    if (host.shadowRoot) {
      try { walk(host.shadowRoot, 'shadow:' + host.tagName.toLowerCase()); } catch (e) {}
    }
  }
  return out;
}"""


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    try:
        be._enter_create_page(page)
        page.wait_for_timeout(3000)
        found = page.evaluate(ACTIVITY_JS)
        page.screenshot(path=str(ROOT / "state" / "diag" / "activity_ui.png"), full_page=False)
        OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"命中 {len(found)} 个含「活动/参与/挑战/话题」的元素 -> {OUT}")
        for it in found[:60]:
            v = "可见" if it["visible"] else "隐藏"
            print(f"  [{v}] {it['frame']} <{it['tag']} {it['cls'][:40]}> {it['text']}")
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
