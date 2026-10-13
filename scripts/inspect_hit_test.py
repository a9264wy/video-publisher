#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：搞清「主 frame 的 shadow 副本」与「micro iframe 副本」谁在最上层。

背景：视频号助手用 **WUJIE（无界）微前端**。主 document 里有个
``<wujie-app class="wujie_iframe">`` 的 shadow root，里面渲染了一整套表单；
同时 page.frames 里还有一个 ``/micro/content/post/create`` 的 iframe，里面也有一份。

两份 DOM 的控件坐标**完全重合**，所以：
  * 点对了一份、看的是另一份，就会出现「日志说点了、界面没反应」；
  * 用 ``document.elementFromPoint`` 做主 frame 内的**命中测试**即可判定谁在上面。

⚠️ 只读：不投递视频、不点提交。

用法：
    python scripts/inspect_hit_test.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

HIT = r"""(pt) => {
  const el = document.elementFromPoint(pt.x, pt.y);
  if (!el) return {hit: null};
  // 找到它在主 document 里的宿主链（穿透 shadow 边界）
  let chain = [];
  let cur = el;
  while (cur) {
    chain.push(cur.tagName + (cur.className ? '.' + String(cur.className).slice(0, 40) : ''));
    const root = cur.getRootNode && cur.getRootNode();
    cur = (root && root.host) ? root.host : cur.parentElement;
  }
  return {hit: el.tagName, cls: String(el.getAttribute('class') || '').slice(0, 80),
          chain: chain.slice(0, 10)};
}"""

FRAME_INFO = r"""() => {
  const out = {iframes: [], shadowHosts: []};
  for (const f of document.querySelectorAll('iframe')) {
    const r = f.getBoundingClientRect();
    out.iframes.push({cls: String(f.getAttribute('class') || '').slice(0, 60),
                      src: (f.getAttribute('src') || '').slice(0, 80),
                      x: Math.round(r.x), y: Math.round(r.y),
                      w: Math.round(r.width), h: Math.round(r.height),
                      z: getComputedStyle(f).zIndex,
                      display: getComputedStyle(f).display,
                      vis: getComputedStyle(f).visibility});
  }
  const stack = [document];
  const seen = new Set();
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot && !seen.has(el)) {
        seen.add(el);
        const r = el.getBoundingClientRect();
        out.shadowHosts.push({tag: el.tagName,
                              cls: String(el.getAttribute('class') || '').slice(0, 60),
                              x: Math.round(r.x), y: Math.round(r.y),
                              w: Math.round(r.width), h: Math.round(r.height),
                              z: getComputedStyle(el).zIndex});
        stack.push(el.shadowRoot);
      }
    }
  }
  return out;
}"""


def main() -> int:
    cfg = Config.load("config.json")
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {}
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)
        print("已进入「发表动态」页")

        main = page.main_frame
        info = main.evaluate(FRAME_INFO)
        report["main_frame_layout"] = info
        print("\n### 主 frame 内的 iframe")
        for f in info["iframes"]:
            print("  ", f)
        print("\n### 主 frame 内的 shadow host")
        for h in info["shadowHosts"]:
            print("  ", h)

        # 发表按钮（主 frame 的 shadow 副本）
        pub_sel = "button:text-is('发表')"
        btn = main.locator(pub_sel).first
        n_pub = main.locator(pub_sel).count()
        print(f"\n### 主 frame 里 {pub_sel} count={n_pub}")
        bbox = btn.bounding_box()
        print(f"  bounding_box = {bbox}")
        report["publish_bbox"] = bbox

        if bbox:
            pt = {"x": bbox["x"] + bbox["width"] / 2, "y": bbox["y"] + bbox["height"] / 2}
            hit = main.evaluate(HIT, pt)
            print(f"  主 frame 命中测试 @({pt['x']:.0f},{pt['y']:.0f}) → {hit}")
            report["hit_at_publish"] = hit

        # 对比 iframe 内的同名按钮
        for i, f in enumerate(backend._frames(page)):
            try:
                n = f.locator("button:text-is('发表')").count()
                bb = f.locator("button:text-is('发表')").first.bounding_box() if n else None
            except Exception as exc:
                n, bb = f"err {str(exc)[:60]}", None
            print(f"\n### frame[{i}] {getattr(f, 'url', '?')}  发表按钮 count={n} bbox={bb}")
            report.setdefault("frames_publish", []).append(
                {"index": i, "url": getattr(f, "url", ""), "count": n, "bbox": bb})

        page.screenshot(path=str(ROOT / "state" / "hit_test.png"), full_page=True)
        out = ROOT / "state" / "inspect_hit_test.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
