#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实验脚本：视频投递后，进度到底会不会动？headless 与有头模式各测一次。

诊断背景：实盘时点「发表」后平台弹「请上传视频」，且上传区一直停在
``0% / 取消上传``、缩略图是灰块 —— 说明**文件虽然塞进 input 了，但上传没跑起来**。

本脚本只做一件事：``set_input_files`` → 之后每 5s 打一次进度，最多 3 分钟，
看 ``0%`` 会不会变。用于区分「只是慢」还是「根本没开始」。

⚠️ 只投递文件、不点任何提交按钮 → 不会在账号上产生内容。

用法：
    python scripts/probe_upload.py headless
    python scripts/probe_upload.py headful
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

PROGRESS = re.compile(r"(\d{1,3})\s*%")
UPLOAD_JS = r"""() => {
  const out = {text: '', hasCancel: false, hasRetry: false, thumb: false, pct: null};
  const stack = [document];
  const seen = new Set();
  let blob = [];
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot && !seen.has(el)) { seen.add(el); stack.push(el.shadowRoot); }
    }
    let t = '';
    try { t = (root.textContent || ''); } catch (e) {}
    if (t) blob.push(t);
  }
  out.text = blob.join(' ').replace(/\s+/g, ' ');
  out.hasCancel = /取消上传/.test(out.text);
  out.hasRetry = /重新上传|上传失败/.test(out.text);
  out.hasForm = /视频描述/.test(out.text);
  out.thumb = !!document.querySelector('img,video,canvas');
  const m = out.text.match(/(\d{1,3})\s*%/);
  out.pct = m ? Number(m[1]) : null;
  out.len = out.text.length;
  out.tail = out.text.slice(0, 400);
  return out;
}"""


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "headless").lower()
    headless = mode != "headful"
    cfg = Config.load("config.json")
    items = scan_source(cfg)
    item = items[0]

    backend = create_backend("channels_web", cfg)
    try:
        print(f"模式：{'headless' if headless else '有头(headful)'}")
        print(f"条目：{item.item_id}  视频：{Path(item.video_path).name} "
              f"({item.video_size / 1024 / 1024:.2f}MB)")
        ctx = backend._launch(headless=headless)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)

        loc = backend._locate(page, "file_input")
        print(f"file_input 定位结果：{loc is not None}")
        t0 = time.time()
        loc.set_input_files(item.video_path)
        print(f"[{time.time()-t0:5.1f}s] 已 set_input_files")

        last = None
        for i in range(24):   # 最多 2 分钟
            time.sleep(5)
            el = time.time() - t0
            # 取「内容 frame」的信息：文案里含 取消上传/视频描述 的那个
            best = None
            for f in backend._frames(page):
                try:
                    info = f.evaluate(UPLOAD_JS)
                except Exception:
                    continue
                if info.get("hasForm") or info.get("hasCancel"):
                    best = info
                    break
            if best is None:
                print(f"[{el:5.1f}s] 未取到上传区信息")
                continue
            pct = best.get("pct")
            print(f"[{el:5.1f}s] pct={pct} cancel={best['hasCancel']} "
                  f"retry={best['hasRetry']} 媒体节点={best['thumb']} textLen={best.get('len')}")
            if pct is not None and pct != last:
                last = pct
            if not best["hasCancel"] and pct is None:
                print("   → 上传已完成（「取消上传」消失且无百分比）")
                break
        page.screenshot(path=str(ROOT / "state" / f"probe_upload_{mode}.png"), full_page=True)
        print(f"截图：{ROOT / 'state' / f'probe_upload_{mode}.png'}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
