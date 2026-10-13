#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查 v3：展开「发表时间」picker 面板，采集其内部结构。

已知（inspect v2 采集）：
    dl.weui-desktop-picker__date.weui-desktop-picker__date-time
      dt.weui-desktop-picker__dt
        span.weui-desktop-picker__value
          div.weui-desktop-picker__value_input.weui-desktop-form__input-area
            input.weui-desktop-form__input[placeholder='请选择发表时间'][readonly]
      dd.weui-desktop-picker__dd            ← 面板，展开前 display:none
        div.weui-desktop-picker__panel.weui-desktop-picker__panel_day…
          div.weui-desktop-picker__panel__hd  (上/下月按钮)
          …

本脚本：进页 → 点「定时」→ 点时间 input 展开面板 → dump 面板 HTML + 截图。
⚠️ 只读：不投递视频、不提交内容。

用法：
    python scripts/inspect_schedule_picker.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

TIME_INPUT = "input.weui-desktop-form__input[placeholder='请选择发表时间']"

DUMP_PANEL = r"""() => {
  const dd = document.querySelector('dd.weui-desktop-picker__dd');
  if (!dd) return {panel: null};
  const out = {panel: dd.outerHTML.slice(0, 20000), style: dd.getAttribute('style') || ''};
  const taps = [];
  const nodes = dd.querySelectorAll(
    'input,select,button,li,td,th,dt,dd,span[class*=cell],div[class*=panel],div[class*=col],div[class*=item]'
  );
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    const visible = r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    const txt = (el.innerText || el.value || '').trim().replace(/\n/g, ' ').slice(0, 30);
    if (!visible && !txt) continue;
    taps.push({
      tag: el.tagName,
      cls: String(el.getAttribute('class') || '').slice(0, 90),
      txt: txt,
      val: el.value !== undefined ? String(el.value).slice(0, 30) : '',
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      visible: visible
    });
  }
  return {...out, taps: taps.slice(0, 120)};
}"""


def main() -> int:
    cfg = Config.load("config.json")
    cfg.dry_run = False
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {}
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        print("已进入「发表动态」页")
        page.wait_for_timeout(3000)

        # 真正的表单 frame
        form = None
        for f in backend._frames(page):
            try:
                if f.locator(".post-time-wrap").count() > 0:
                    form = f
                    break
            except Exception:
                continue
        if form is None:
            print("未找到表单 frame")
            return 1
        print(f"表单 frame: {form.url}")

        # 1) 选「定时」
        form.locator('.post-time-wrap label:has(input[value="1"])').click(timeout=8000)
        page.wait_for_timeout(2000)
        checked = form.locator('.post-time-wrap input[value="1"]').is_checked()
        print(f"「定时」已选中：{checked}")
        report["scheduled_radio_checked"] = checked

        # 2) 记录默认值
        ti = form.locator(TIME_INPUT)
        print(f"时间输入框数量={ti.count()}  默认值={ti.first.input_value()!r}")
        report["default_value"] = ti.first.input_value()

        # 3) 展开面板
        ti.first.click(timeout=8000)
        page.wait_for_timeout(2500)
        info = form.evaluate(DUMP_PANEL)
        report["panel"] = info
        print(f"\n面板 style={info.get('style')!r}")
        html = info.get("panel") or ""
        print(f"面板 HTML 长度 {len(html)}")
        print("\n--- 面板 HTML（前 4000）---")
        print(html[:4000])
        print(f"\n--- 面板内可见/有文本元素（{len(info.get('taps', []))} 个，前 60）---")
        for t in (info.get("taps") or [])[:60]:
            print("   ", json.dumps(t, ensure_ascii=False))

        page.screenshot(path=str(ROOT / "state" / "inspect_picker.png"), full_page=True)
        out = ROOT / "state" / "inspect_picker.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
