#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定位视频号登录页的二维码元素，并输出精确坐标。

用途：当通用选择器匹配不到二维码时，用几何启发式（接近正方形、尺寸够大、
位于页面右侧面板）找出候选区域，供 login 流程做精确裁剪截图。
输出 JSON 到 stdout 与 state/qr_probe.json。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

JS = """() => {
  const out = [];
  document.querySelectorAll('img,canvas,div,svg').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 100 || r.height < 100) return;
    const ratio = r.width / r.height;
    if (ratio < 0.8 || ratio > 1.25) return;
    let cls = '';
    try { cls = (el.getAttribute('class') || ''); } catch (e) { cls = ''; }
    out.push({
      tag: el.tagName, cls: String(cls).slice(0, 80), id: el.id || '',
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      src: String(el.getAttribute('src') || '').slice(0, 60)
    });
  });
  return out;
}"""


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto("https://channels.weixin.qq.com/login.html",
                  wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)
        cands = page.evaluate(JS)
        browser.close()

    result = {
        "viewport": {"w": 1440, "h": 900},
        "candidates": cands,
    }
    out = ROOT / "state" / "qr_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n已写入 {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
