#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查 v2：点击「定时」单选项后，看看时间选择控件长什么样。

已知真实 DOM（scripts/inspect_schedule_radio.py 采集）：

    <div class="post-time-wrap">
      <div class="form-item">
        <div class="label">定时发表</div>
        <div class="form-item-body">
          <div class="weui-desktop-radio-group">
            <label class="weui-desktop-form__check-label">
              <input type="radio" class="weui-desktop-form__radio" value="0"> …不定时
            </label>
            <label class="weui-desktop-form__check-label">
              <input type="radio" class="weui-desktop-form__radio" value="1"> …定时
            </label>
          </div>
        </div>
      </div>
    </div>

⚠️ 只读：进页 → 点「定时」→ dump 结构 → 关闭。**不投递视频、不提交内容**。

用法：
    python scripts/inspect_schedule_radio.py --click
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

# 每个 frame 的现状：谁是「活的」表单
PROBE_FRAME = r"""() => {
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const files = [...document.querySelectorAll("input[type=file][accept*='video']")];
  const radios = [...document.querySelectorAll('.post-time-wrap input.weui-desktop-form__radio')];
  const wrap = document.querySelector('.post-time-wrap');
  return {
    file_inputs: files.length,
    file_visible: files.filter(vis).length,
    radios: radios.map(r => ({value: r.value, checked: r.checked, visible: vis(r)})),
    wrap_present: !!wrap,
    body_len: (document.body ? document.body.innerText.length : 0)
  };
}"""

# 点「定时」之后，dump 整个 post-time-wrap 以及新出现的可见时间控件
PROBE_AFTER = r"""() => {
  const vis = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
  };
  const wrap = document.querySelector('.post-time-wrap');
  const widgets = [];
  const nodes = document.querySelectorAll(
    'input,select,button,.weui-desktop-picker,.weui-desktop-form__control,[class*=date],[class*=time],[class*=picker]'
  );
  for (const el of nodes) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    widgets.push({
      tag: el.tagName,
      cls: String(el.getAttribute('class') || '').slice(0, 100),
      type: el.getAttribute('type') || '',
      value: el.value !== undefined ? String(el.value).slice(0, 40) : '',
      ph: el.getAttribute('placeholder') || '',
      txt: (el.innerText || '').trim().replace(/\n/g, ' ').slice(0, 40),
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    });
  }
  return {
    wrap_html: wrap ? wrap.outerHTML.slice(0, 8000) : null,
    widgets: widgets.slice(0, 80)
  };
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

        frames = backend._frames(page)
        print(f"\n### 各 frame 现状（{len(frames)} 个）")
        states = []
        for i, f in enumerate(frames):
            try:
                st = f.evaluate(PROBE_FRAME)
            except Exception as exc:
                st = {"error": str(exc)[:160]}
            st["index"] = i
            st["url"] = f.url
            st["is_main"] = (f is page.main_frame)
            states.append(st)
            print(f"  [{i}] main={st['is_main']} {st['url']}")
            print(f"       file_inputs={st.get('file_inputs')} "
                  f"file_visible={st.get('file_visible')} wrap={st.get('wrap_present')} "
                  f"radios={st.get('radios')}")
        report["frames_before"] = states

        # ---- 点「定时」：逐 frame 尝试，以「radio 真的选中」为成功判据 ----
        clicked = None
        for f in frames:
            try:
                radios = f.locator(".post-time-wrap input.weui-desktop-form__radio")
                if radios.count() < 2:
                    continue
            except Exception:
                continue
            try:
                f.locator(".post-time-wrap input.weui-desktop-form__radio").nth(1).check(force=True)
            except Exception as exc:
                print(f"  check() 失败：{str(exc)[:120]}")
                try:
                    f.locator('.post-time-wrap label:has(input[value="1"])').click(timeout=5000)
                except Exception as exc2:
                    print(f"  click(label) 失败：{str(exc2)[:120]}")
                    continue
            page.wait_for_timeout(2000)
            try:
                checked = f.locator('.post-time-wrap input[value="1"]').is_checked()
            except Exception:
                checked = False
            if checked:
                clicked = f.url
                break
        print(f"\n点击「定时」结果：{'成功 via ' + str(clicked) if clicked else '失败'}")
        report["clicked_frame"] = clicked
        page.wait_for_timeout(2500)

        print(f"\n### 点击后各 frame 状态")
        after_states = []
        for i, f in enumerate(frames):
            try:
                st = f.evaluate(PROBE_FRAME)
            except Exception as exc:
                st = {"error": str(exc)[:160]}
            st["index"] = i
            st["url"] = f.url
            after_states.append(st)
            print(f"  [{i}] {st['url']} -> radios={st.get('radios')}")
        report["frames_after"] = after_states

        for i, f in enumerate(frames):
            try:
                info = f.evaluate(PROBE_AFTER)
            except Exception as exc:
                info = {"error": str(exc)[:200]}
            if info.get("wrap_html"):
                report[f"after_{i}"] = info
                print(f"\n### frame[{i}] post-time-wrap 结构")
                print(info["wrap_html"][:1800])
                print(f"\n### frame[{i}] 可见时间控件 {len(info.get('widgets', []))} 个")
                for w in info.get("widgets", [])[:25]:
                    print("   ", json.dumps(w, ensure_ascii=False))
                break

        page.screenshot(path=str(ROOT / "state" / "inspect_schedule_after.png"),
                        full_page=True)
        out = ROOT / "state" / "inspect_schedule_after.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
