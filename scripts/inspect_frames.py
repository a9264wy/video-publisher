#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查 v5：把 3 个 frame 的「真实身份」定死。

背景：
  * v1（递归 shadowRoot 遍历）说 frame[0] 和 frame[2] 各含 9 个「定时」元素；
  * v2（document.querySelector）说只有 frame[2] 有 .post-time-wrap；
  * v4 用 locator 选 frame，选中了 frame[0]，但 frame[0].evaluate(document.querySelector)
    找不到 .post-time-wrap → 说明 frame[0] 的副本在 **shadow root** 里。

必须搞清楚**哪个副本才是用户看到、且点击真正生效的那一份**，
否则 set_input_files / 点「发表」可能落在死副本上。

判据（按可靠性排序）：
  1. 视频 file input 在哪个 frame 的 **light DOM** 里（当初实测 count=1）；
  2. 各 frame 的 document.body.innerHTML 长度；
  3. 各 frame 里 .post-time-wrap 是 light DOM 还是 shadow DOM；
  4. frame 的 parent_frame / 层级。

⚠️ 只读，不投递、不提交。

用法：
    python scripts/inspect_frames.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

PROBE = r"""() => {
  const LIGHT = {
    video_inputs: document.querySelectorAll("input[type=file][accept*='video']").length,
    time_wrap: document.querySelectorAll('.post-time-wrap').length,
    desc_editor: document.querySelectorAll('div.input-editor').length,
    publish_btn: document.querySelectorAll('button.weui-desktop-btn_primary').length,
  };
  // 统计 shadow root 数量与其中的关键节点
  let hosts = 0, shadow_video = 0, shadow_wrap = 0, shadow_desc = 0;
  const stack = [document];
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot) {
        hosts++;
        try {
          shadow_video += el.shadowRoot.querySelectorAll("input[type=file][accept*='video']").length;
          shadow_wrap  += el.shadowRoot.querySelectorAll('.post-time-wrap').length;
          shadow_desc  += el.shadowRoot.querySelectorAll('div.input-editor').length;
        } catch (e) {}
        stack.push(el.shadowRoot);
      }
    }
  }
  return {
    url: location.href,
    title: document.title,
    body_len: document.body ? document.body.innerHTML.length : 0,
    light: LIGHT,
    shadow_hosts: hosts,
    shadow: {video: shadow_video, wrap: shadow_wrap, desc: shadow_desc},
    has_iframes: document.querySelectorAll('iframe').length,
    iframe_srcs: [...document.querySelectorAll('iframe')].map(f => f.getAttribute('src') || '').slice(0, 8)
  };
}"""


def main() -> int:
    cfg = Config.load("config.json")
    cfg.dry_run = False
    backend = create_backend("channels_web", cfg)
    report: list[dict] = []
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)

        frames = backend._frames(page)
        print(f"共 {len(frames)} 个 frame（_frames() 的顺序：main 优先）\n")
        for i, f in enumerate(frames):
            try:
                info = f.evaluate(PROBE)
            except Exception as exc:
                info = {"error": str(exc)[:200]}
            # locator 视角（会穿透 shadow root）
            loc = {}
            for key, sel in (("video_input", "input[type=file][accept*='video']"),
                             ("time_wrap", ".post-time-wrap"),
                             ("desc_editor", "div.input-editor"),
                             ("primary_btn", "button.weui-desktop-btn_primary")):
                try:
                    loc[key] = f.locator(sel).count()
                except Exception as exc:
                    loc[key] = f"err:{str(exc)[:40]}"
            info["locator_counts"] = loc
            info["index"] = i
            info["is_main"] = (f is page.main_frame)
            try:
                info["parent_is_main"] = (f.parent_frame is page.main_frame)
            except Exception:
                info["parent_is_main"] = None
            report.append(info)

            print(f"──[{i}] main={info['is_main']} parent_is_main={info['parent_is_main']}")
            print(f"     evaluate url = {info.get('url')}")
            print(f"     body_len={info.get('body_len')} iframes={info.get('has_iframes')} "
                  f"srcs={info.get('iframe_srcs')}")
            print(f"     light DOM : {info.get('light')}")
            print(f"     shadow    : {info.get('shadow')}（host 数 {info.get('shadow_hosts')}）")
            print(f"     locator   : {info.get('locator_counts')}")
            print()

        Path(ROOT / "state" / "inspect_frames.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出：{ROOT / 'state' / 'inspect_frames.json'}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
