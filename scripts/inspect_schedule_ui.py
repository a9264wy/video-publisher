#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：视频号「定时发表」的时间选择控件结构。

⚠️ 只读取 DOM + 截图，**不提交任何内容**。用于确认能否程序化设定发布时间。

用法：
    python scripts/inspect_schedule_ui.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

DUMP = """() => {
  const out = [];
  const seen = new Set();
  function collect(root, d) {
    if (!root || d > 12) return;
    let nodes = [];
    try {
      nodes = [...root.querySelectorAll(
        'input,select,button,[role=radio],[role=option],li,div[class*=date],div[class*=time],div[class*=picker]'
      )];
    } catch (e) { return; }
    for (const el of nodes) { if (!seen.has(el)) { seen.add(el); out.push(el); } }
    let hosts = [];
    try { hosts = [...root.querySelectorAll('*')]; } catch (e) { return; }
    for (const el of hosts) if (el.shadowRoot) collect(el.shadowRoot, d + 1);
  }
  collect(document, 0);
  return out.map(el => {
    const r = el.getBoundingClientRect();
    let cls = ''; try { cls = String(el.getAttribute('class') || ''); } catch (e) {}
    return {
      tag: el.tagName, cls: cls.slice(0, 80),
      type: el.getAttribute('type') || '',
      ph: el.getAttribute('placeholder') || '',
      checked: el.checked === true,
      txt: (el.innerText || el.value || '').trim().replace(/\\n/g, ' ').slice(0, 30),
      w: Math.round(r.width), h: Math.round(r.height),
      vis: r.width > 0 && r.height > 0
    };
  }).filter(x => x.vis && x.w > 0).slice(0, 60);
}"""


def main() -> int:
    cfg = Config.load("config.json")
    cfg.dry_run = False
    backend = create_backend("channels_web", cfg)
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        print("已进入发表动态页")

        # 找「定时」选项：先定位「定时发表」标签，再找它附近的可点元素
        found = False
        for sel in ("text=定时发表", "text=定时"):
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                # 尝试点它右侧/内部的「定时」单选项
                box = loc.locator("xpath=ancestor::*[self::div or self::label][1]")
                opt = box.locator("text=定时").last
                if opt.count() > 0:
                    opt.click()
                    found = True
                    print(f"已点击「定时」选项（via {sel}）")
                    break
            except Exception as exc:
                print(f"  尝试 {sel} 失败: {exc}", file=sys.stderr)
        page.wait_for_timeout(2500)

        info = page.evaluate(DUMP)
        page.screenshot(path=str(ROOT / "state" / "inspect_schedule.png"))
        out = ROOT / "state" / "inspect_schedule.json"
        out.write_text(json.dumps({"clicked": found, "elements": info},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"clicked={found}, 元素 {len(info)} 个 -> {out}")

        # 只打印与时间相关的高信号元素
        for e in info:
            blob = (e["cls"] + e["ph"] + e["txt"]).lower()
            if any(k in blob for k in ("time", "date", "picker", "时间", "日期", "定时", "时", "分")):
                print("  ", json.dumps(e, ensure_ascii=False))
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
