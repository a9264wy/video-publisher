#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：走**真实文件选择器**路径上传，看进度会不会动。

已确认的事实：
  * ``set_input_files`` 后该 input 的 ``files.length == 0``（文件根本没留在 input 上），
    而上传区却显示 ``0% / 取消上传``、封面一直「生成中」；
  * 平台用 FFmpeg-WASM（rhino-media-suite）在 worker 里抽帧，控制台报
    「使用video硬解获取帧: 0」。

所以怀疑 ``set_input_files`` 这条捷径拿不到真实的 File，SDK 的 worker 读不到数据。
本脚本改为：点上传区 → ``expect_file_chooser`` → ``set_files``（等价于人手选文件），
然后观察进度。

⚠️ 只投递文件，不点提交。

用法：
    python scripts/probe_chooser.py [seconds]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

FIND_UPLOAD = r"""() => {
  const out = [];
  const stack = [document];
  const seen = new Set();
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot && !seen.has(el)) { seen.add(el); stack.push(el.shadowRoot); }
      const cls = String(el.getAttribute('class') || '');
      if (!/upload|add|post-media|cover-upload/i.test(cls)) continue;
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      if (r.width < 20 || r.height < 20) continue;
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      out.push({tag: el.tagName, cls: cls.slice(0, 90), txt: (el.innerText || '').trim().slice(0, 30),
                x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)});
    }
  }
  return out.slice(0, 30);
}"""

PROGRESS = r"""() => {
  const stack = [document]; const seen = new Set(); const blob = [];
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot && !seen.has(el)) { seen.add(el); stack.push(el.shadowRoot); }
    }
    try { if (root.textContent) blob.push(root.textContent); } catch (e) {}
  }
  const t = blob.join(' ').replace(/\s+/g, ' ');
  const m = t.match(/(\d{1,3})\s*%/);
  let files = -1;
  try {
    const inp = document.querySelector("input[type=file][accept*='video']");
    if (inp) files = inp.files ? inp.files.length : -2;
  } catch (e) {}
  return {pct: m ? Number(m[1]) : null, cancel: /取消上传/.test(t),
          retry: /重新上传|上传失败/.test(t), generating: /生成中/.test(t), files: files};
}"""


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    cfg = Config.load("config.json")
    item = scan_source(cfg)[0]

    backend = create_backend("channels_web", cfg)
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)

        cands = backend._locate(page, "file_input").evaluate(FIND_UPLOAD) if False else None
        # 用主 frame 扫（含 shadow root）
        cands = backend._frames(page)[0].evaluate(FIND_UPLOAD)
        print("候选上传区元素：")
        for c in cands:
            print("  ", json.dumps(c, ensure_ascii=False))
        (ROOT / "state" / "upload_candidates.json").write_text(
            json.dumps(cands, ensure_ascii=False, indent=2), encoding="utf-8")

        # 找一个可见、尺寸合适的候选点开文件选择器
        clicked = False
        for c in cands:
            if c["w"] < 60 or c["h"] < 60:
                continue
            cx = c["x"] + c["w"] / 2
            cy = c["y"] + c["h"] / 2
            try:
                with page.expect_file_chooser(timeout=8000) as fc_info:
                    page.mouse.click(cx, cy)
                chooser = fc_info.value
                chooser.set_files(item.video_path)
                print(f"\n已通过文件选择器投递：{item.video_path}")
                print(f"  （点击位置 {cx:.0f},{cy:.0f} 类名 {c['cls']}）")
                clicked = True
                break
            except Exception as exc:
                print(f"  点 ({cx:.0f},{cy:.0f}) 未触发文件选择器：{str(exc)[:90]}")
        if not clicked:
            print("\n未能通过文件选择器投递")
            return 1

        t0 = time.time()
        for _ in range(max(1, seconds // 5)):
            time.sleep(5)
            info = None
            for f in backend._frames(page):
                try:
                    i = f.evaluate(PROGRESS)
                except Exception:
                    continue
                if i.get("cancel") or i.get("pct") is not None or i.get("generating"):
                    info = i
                    break
            print(f"[{time.time()-t0:5.1f}s] {info}")
            if info and not info["cancel"] and info["pct"] is None and not info["generating"]:
                print("   → 上传/生成看起来完成了")
                break
        page.screenshot(path=str(ROOT / "state" / "probe_chooser.png"), full_page=True)
        print(f"截图：{ROOT / 'state' / 'probe_chooser.png'}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
