#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读验证：直接跑**生产代码**的定时设置路径（不提交）。

与 ``verify_schedule_flow.py``（脚本内自带一份复刻逻辑）不同，
本脚本调用后端真实方法：

    backend._set_timing_radio(page, True)
    backend._set_schedule_datetime(page, target)
    backend._read_schedule_value(page)     # 回读校验

这样修 bug 时验的就是将要上线的那份代码，而不是「另写一遍以为一样」的代码。

⚠️ 全程不点「发表」、不投递视频文件 → 账号上不会产生任何内容。
   注意：定时时间会被设进页面表单，但**不提交**，关掉浏览器即丢弃。

用法：
    python scripts/verify_apply_schedule.py [YYYY-MM-DD] [HH:MM]
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub import timing  # noqa: E402
from videopub.backends import create_backend  # noqa: E402


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-10-14"
    hhmm = sys.argv[2] if len(sys.argv) > 2 else "08:00"
    target = dt.datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")

    cfg = Config.load("config.json")
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {"target": target.strftime("%Y-%m-%d %H:%M")}
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)
        print(f"已进入「发表动态」页；目标定时 = {report['target']}")

        # 1) 选「定时」——生产方法
        backend._set_timing_radio(page, True)
        print("已调用 _set_timing_radio(scheduled=True)")

        # 2) 翻月 + 选日期 + 选时间 ——生产方法（内部含 _goto_month/_pick_time_item）
        backend._set_schedule_datetime(page, target)
        print("已调用 _set_schedule_datetime()")

        # 3) 回读校验
        got = backend._read_schedule_value(page)
        ok = (got == report["target"])
        report["actual"] = got
        report["ok"] = ok
        print(f"\n回读值 = {got!r}  期望 = {report['target']!r}  匹配 = {ok}")

        # 顺带确认「发表时间」控件确实可见（截图前先滚到可视区）
        try:
            page.locator("input.weui-desktop-form__input[placeholder='请选择发表时间']").first.scroll_into_view_if_needed()
        except Exception:
            pass
        page.wait_for_timeout(800)
        page.screenshot(path=str(ROOT / "state" / "verify_apply_schedule.png"), full_page=True)

        out = ROOT / "state" / "verify_apply_schedule.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出：{out}")
        print(f"截图：{ROOT / 'state' / 'verify_apply_schedule.png'}")
        if not ok:
            print("\n⚠️ 回读不一致 —— 生产路径的定时设置仍不可靠！")
        return 0 if ok else 1
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
