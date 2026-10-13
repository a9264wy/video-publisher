#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查 v7：验证「定时发表 + 发表时间」全流程可程序化，并校准提交按钮选择器。

核心动作（**不点「发表」**）：
    1. 选「定时」radio      → .post-time-wrap label:has(input[value="1"])
    2. 开日期面板            → input[placeholder='请选择发表时间']
    3. 选日期 14             → .weui-desktop-picker__panel_day a:text-is('14')
    4. 开时间列表            → dl.weui-desktop-picker__time input[placeholder='请选择时间']
    5. 选小时 08 / 分钟 00   → ol.weui-desktop-picker__time__hour li:text-is('08') …
    6. 关闭面板 & 回读值      → input[placeholder='请选择发表时间'].input_value()

同时统计候选提交按钮：button.weui-desktop-btn_primary 有 23 个，
必须找出「文案恰为『发表』」的那一个（否则点到隐藏按钮）。

⚠️ 不投递视频、不提交内容。

用法：
    python scripts/verify_schedule_flow.py [YYYY-MM-DD] [HH:MM]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

MAIN_TIME = "input.weui-desktop-form__input[placeholder='请选择发表时间']"
TIME_LIST_INPUT = "dl.weui-desktop-picker__time input[placeholder='请选择时间']"
HOUR_LI = "ol.weui-desktop-picker__time__hour li"
MIN_LI = "ol.weui-desktop-picker__time__minute li"
DAY_LINK = ".weui-desktop-picker__panel_day a"

BUTTON_CANDIDATES = [
    "button.weui-desktop-btn_primary",
    "button:text-is('发表')",
    "button:has-text('发表')",
    ".post-footer button.weui-desktop-btn_primary",
    "button:text-is('保存草稿')",
    "button:text-is('手机预览')",
]


def main() -> int:
    target_date = sys.argv[1] if len(sys.argv) > 1 else "2026-10-14"
    target_time = sys.argv[2] if len(sys.argv) > 2 else "08:00"
    day = str(int(target_date.split("-")[2]))
    hour, minute = target_time.split(":")

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

        # ---- 0) 按钮选择器校准 ----
        print("### 提交按钮候选计数")
        btns = {}
        for sel in BUTTON_CANDIDATES:
            n = frame.locator(sel).count()
            vis = 0
            for i in range(min(n, 30)):
                try:
                    if frame.locator(sel).nth(i).is_visible():
                        vis += 1
                except Exception:
                    pass
            btns[sel] = {"count": n, "visible": vis}
            print(f"  {sel:52s} count={n:3d} visible={vis}")
        report["buttons"] = btns

        # 主提交按钮（文案恰为「发表」）的可见性
        pub = frame.locator("button:text-is('发表')")
        print(f"\n  button:text-is('发表') 可见数 = "
              f"{sum(1 for i in range(pub.count()) if pub.nth(i).is_visible())}")

        # ---- 1) 选「定时」 ----
        frame.locator('.post-time-wrap label:has(input[value="1"])').click(timeout=10000)
        page.wait_for_timeout(2000)
        checked = frame.locator('.post-time-wrap input[value="1"]').is_checked()
        print(f"\n### 1) 定时 radio checked = {checked}")
        report["scheduled_checked"] = checked

        ti = frame.locator(MAIN_TIME).first
        print(f"### 2) 默认时间 = {ti.input_value()!r}")

        # ---- 2) 开日期面板 ----
        ti.click(timeout=10000)
        page.wait_for_timeout(2000)
        dd = frame.locator(".post-time-wrap dd.weui-desktop-picker__dd").first
        print(f"### 3) 日期面板可见 = {dd.is_visible()}")

        # ---- 3) 选日期 ----
        day_loc = dd.locator(f"a:text-is('{day}')")
        nday = day_loc.count()
        enabled = []
        for i in range(nday):
            cls = day_loc.nth(i).get_attribute("class") or ""
            enabled.append("disabled" not in cls)
        print(f"### 4) 日期 {day} 出现 {nday} 次，可用 {enabled}")
        if nday and enabled[0]:
            day_loc.first.click(timeout=8000)
            page.wait_for_timeout(1500)
            print(f"    已点日期 {day}")
            report["day_clicked"] = True
        else:
            print(f"    日期 {day} 不可用！")
            report["day_clicked"] = False

        # ---- 4) 开时间列表 ----
        tlist = frame.locator(TIME_LIST_INPUT).first
        print(f"### 5) 时间列表输入框 count={frame.locator(TIME_LIST_INPUT).count()}")
        tlist.click(timeout=8000)
        page.wait_for_timeout(1500)

        hour_panel = frame.locator(HOUR_LI)
        min_panel = frame.locator(MIN_LI)
        print(f"    小时项 {hour_panel.count()} 个，分钟项 {min_panel.count()} 个")

        # 可用性（选完日期后，小时应全部可用）
        h_exact = frame.locator(f"{HOUR_LI}:text-is('{hour}')")
        m_exact = frame.locator(f"{MIN_LI}:text-is('{minute}')")
        print(f"    精确匹配小时 '{hour}' = {h_exact.count()}，分钟 '{minute}' = {m_exact.count()}")
        if h_exact.count():
            hcls = h_exact.first.get_attribute("class") or ""
            print(f"    小时 '{hour}' class = {hcls!r}")
        if m_exact.count():
            mcls = m_exact.first.get_attribute("class") or ""
            print(f"    分钟 '{minute}' class = {mcls!r}")

        # ---- 5) 点小时 / 分钟 ----
        if h_exact.count() and "disabled" not in (h_exact.first.get_attribute("class") or ""):
            h_exact.first.click(timeout=8000)
            page.wait_for_timeout(1200)
            print(f"    已点小时 {hour}")
            report["hour_clicked"] = True
        else:
            report["hour_clicked"] = False
            print(f"    小时 {hour} 不可点，尝试滚动/重试")
        if m_exact.count():
            m_exact.first.click(timeout=8000)
            page.wait_for_timeout(1200)
            print(f"    已点分钟 {minute}")
            report["minute_clicked"] = True
        else:
            report["minute_clicked"] = False

        # ---- 6) 关面板并回读 ----
        frame.locator(".post-desc-box").first.click(timeout=8000)
        page.wait_for_timeout(2000)
        final = frame.locator(MAIN_TIME).first.input_value()
        print(f"\n### 6) 最终时间框值 = {final!r}")
        report["final_value"] = final
        report["expected"] = f"{target_date} {target_time}"
        report["ok"] = (final == f"{target_date} {target_time}")

        page.screenshot(path=str(ROOT / "state" / "verify_schedule_flow.png"), full_page=True)
        out = ROOT / "state" / "verify_schedule_flow.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n期望 = {report['expected']}  实际 = {final}  匹配 = {report['ok']}")
        print(f"已写出：{out}")
        return 0 if report["ok"] else 1
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
