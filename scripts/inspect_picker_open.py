#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查 v4：搞清「发表时间」picker 到底怎么展开。

v3 结果：选了「定时」后时间框出现（默认 2026-10-13 20:00），
但点 input 之后 ``dd.weui-desktop-picker__dd`` 依然查不到（panel=None）。

本脚本依次尝试多个点击目标（input / dt / 图标 / 外层 span），
每次点击后报告 dd 的数量与 display 状态，找到真正的「展开开关」。

⚠️ 只读：不投递视频、不提交内容。

用法：
    python scripts/inspect_picker_open.py
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

STATE = r"""() => {
  const dds = [...document.querySelectorAll('dd.weui-desktop-picker__dd')];
  const wrap = document.querySelector('.post-time-wrap');
  const panels = [...document.querySelectorAll('.weui-desktop-picker__panel')];
  return {
    dd_count: dds.length,
    dd_styles: dds.map(d => ({style: d.getAttribute('style') || '', cls: d.className,
                              kids: d.children.length,
                              h: Math.round(d.getBoundingClientRect().height)})),
    panel_count: panels.length,
    wrap_has_dd: !!(wrap && wrap.querySelector('dd.weui-desktop-picker__dd')),
    wrap_html_len: wrap ? wrap.outerHTML.length : 0,
    dd_in_wrap: wrap ? wrap.querySelectorAll('dd').length : 0,
    // 面板里若出现日历，会有 1..31 的数字单元格
    day_cells: [...document.querySelectorAll('.weui-desktop-picker__panel_day td, .weui-desktop-picker__panel_day span')]
      .map(e => (e.innerText || '').trim()).filter(t => /^\d{1,2}$/.test(t)).slice(0, 40)
  };
}"""

TARGETS = [
    ("input", TIME_INPUT),
    ("dt", ".post-time-wrap dt.weui-desktop-picker__dt"),
    ("icon", ".post-time-wrap .weui-desktop-picker__icon-wrap"),
    ("value_span", ".post-time-wrap .weui-desktop-picker__value"),
    ("input_wrp", ".post-time-wrap .weui-desktop-form__input-wrp"),
]


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

        form.locator('.post-time-wrap label:has(input[value="1"])').click(timeout=8000)
        page.wait_for_timeout(2000)
        print(f"「定时」已选中：{form.locator('.post-time-wrap input[value=\"1\"]').is_checked()}")
        print(f"默认时间：{form.locator(TIME_INPUT).first.input_value()!r}")

        print("\n初始状态：")
        print("  ", json.dumps(form.evaluate(STATE), ensure_ascii=False))

        steps = []
        for name, sel in TARGETS:
            loc = form.locator(sel).first
            try:
                n = loc.count()
            except Exception as exc:
                steps.append({"target": name, "error": str(exc)[:120]})
                continue
            if n == 0:
                steps.append({"target": name, "skipped": "not found"})
                print(f"\n[{name}] 未找到，跳过")
                continue
            try:
                loc.click(timeout=6000)
            except Exception as exc:
                steps.append({"target": name, "click_error": str(exc)[:150]})
                print(f"\n[{name}] click 失败：{str(exc)[:150]}")
                continue
            page.wait_for_timeout(2000)
            st = form.evaluate(STATE)
            steps.append({"target": name, "state": st})
            print(f"\n[{name}] 点击后：")
            print("  ", json.dumps(st, ensure_ascii=False))
            if st.get("dd_count") and any((d.get("style") or "").find("none") < 0
                                          for d in st.get("dd_styles", [])):
                print(f"  → 面板已展开（{name}）")
                break

        report["steps"] = steps
        report["default_value"] = form.locator(TIME_INPUT).first.input_value()

        # 展开后 dump 面板
        dumped = form.evaluate(r"""() => {
          const dd = document.querySelector('.post-time-wrap dd.weui-desktop-picker__dd')
                     || document.querySelector('dd.weui-desktop-picker__dd');
          if (!dd) return null;
          return dd.outerHTML.slice(0, 30000);
        }""")
        report["panel_html"] = dumped or ""
        print(f"\n面板 HTML 长度：{len(report['panel_html'])}")
        if dumped:
            Path(ROOT / "state" / "picker_panel.html").write_text(dumped, encoding="utf-8")
            print(dumped[:3000])

        page.screenshot(path=str(ROOT / "state" / "inspect_picker_open.png"), full_page=True)
        out = ROOT / "state" / "inspect_picker_open.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
