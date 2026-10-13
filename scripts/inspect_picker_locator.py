#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查 v6：用 **locator**（能穿透 shadow root）展开「发表时间」面板并采集结构。

v4 的失败原因已定位：v4 用 ``frame.evaluate(document.querySelector(...))``，
但主 frame 的表单在 **shadow root** 内 → 查不到 → 误判「面板打不开」。
截图（inspect_schedule_after.png）已证明点「定时」在真实 UI 上生效。

本脚本只用 locator API：
    label:has(input[value="1"])   → 选「定时」
    input[placeholder='请选择发表时间'] → 开面板
    dl/ dd / .weui-desktop-picker__panel* → dump 结构

⚠️ 只读：不投递视频、不提交内容。

用法：
    python scripts/inspect_picker_locator.py
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

# 在「元素上下文」里评估：locator.evaluate 会在该元素内部执行，天然穿透 shadow root
DUMP = r"""el => {
  const root = el;
  const vis = n => {
    const r = n.getBoundingClientRect();
    const st = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const items = [];
  const nodes = root.querySelectorAll(
    'input,select,button,li,td,th,dt,dd,span,div[class*=panel],div[class*=col],div[class*=item],div[class*=list]'
  );
  for (const n of nodes) {
    if (!vis(n)) continue;
    const r = n.getBoundingClientRect();
    const txt = (n.innerText || n.value || '').trim().replace(/\n/g, ' ').slice(0, 24);
    if (!txt && n.tagName !== 'INPUT') continue;
    items.push({
      tag: n.tagName,
      cls: String(n.getAttribute('class') || '').slice(0, 80),
      txt: txt,
      val: n.value !== undefined ? String(n.value).slice(0, 24) : '',
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    });
  }
  return {html: root.outerHTML.slice(0, 30000), items: items.slice(0, 120)};
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
        page.wait_for_timeout(3000)
        frame = page.main_frame

        # 1) 选「定时」
        frame.locator('.post-time-wrap label:has(input[value="1"])').click(timeout=10000)
        page.wait_for_timeout(2000)
        checked = frame.locator('.post-time-wrap input[value="1"]').is_checked()
        print(f"「定时」选中 = {checked}")
        report["checked"] = checked

        ti = frame.locator(TIME_INPUT).first
        print(f"默认时间 = {ti.input_value()!r}")
        report["default"] = ti.input_value()

        # 2) 开面板
        ti.click(timeout=10000)
        page.wait_for_timeout(2500)

        dd = frame.locator(".post-time-wrap dd.weui-desktop-picker__dd").first
        print(f"\ndd 数量 = {frame.locator('.post-time-wrap dd.weui-desktop-picker__dd').count()}")
        print(f"dd 可见 = {dd.is_visible()}")
        print(f"dd 内 HTML 长度 = {len(dd.inner_html())}")
        report["dd_visible"] = dd.is_visible()

        info = dd.evaluate(DUMP)
        report["panel_html"] = info["html"]
        report["items"] = info["items"]
        Path(ROOT / "state" / "picker_panel.html").write_text(info["html"], encoding="utf-8")

        print(f"\n面板可见元素 {len(info['items'])} 个：")
        for it in info["items"]:
            print("   ", json.dumps(it, ensure_ascii=False))

        page.screenshot(path=str(ROOT / "state" / "inspect_picker_locator.png"), full_page=True)
        out = ROOT / "state" / "inspect_picker_locator.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
