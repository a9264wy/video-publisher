#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：复刻完整发布流程，在点「发表」前后**高频抓取**页面状态。

动机：前面两次实盘都返回 success，但视频管理里始终是「视频 (0)」。
说明点击「发表」之后**什么也没发生**，但日志看不出来 —— 因为：
  * 我们只看关键词（审核不通过…），而页面文案几乎采不到；
  * 没有记录点击瞬间的截图/文案。

本脚本在 click 之后 0.8s / 2s / 5s / 10s 各抓一次截图 + 各 frame 文案 + 可见按钮，
把「平台到底有没有反应」这件事钉死。

⚠️ 这是**真实提交**（会真的建一条定时内容）。仅在用户明确要求时运行。

用法：
    python scripts/diag_submit.py [item_id]
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

VISIBLE_BUTTONS = r"""() => {
  const out = [];
  const seen = new Set();
  const stack = [document];
  while (stack.length) {
    const r = stack.pop();
    let all = [];
    try { all = r.querySelectorAll('button'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot) stack.push(el.shadowRoot);
      const rect = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      if (rect.width > 0 && rect.height > 0 && st.display !== 'none'
          && st.visibility !== 'hidden') {
        const t = (el.innerText || '').trim();
        const key = t + '|' + String(el.className).slice(0, 30);
        if (t && !seen.has(key)) { seen.add(key); out.push(t.slice(0, 24)); }
      }
    }
  }
  return out.slice(0, 40);
}"""

FRAME_TEXT = r"""() => {
  let t = '';
  try { t = (document.body ? document.body.innerText : '').trim(); } catch (e) {}
  return {url: location.href, len: t.length, text: t.slice(0, 2500)};
}"""


def snapshot(page, backend, tag: str, outdir: Path) -> dict:
    snap = {"tag": tag, "at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "url": page.url, "frames": [], "buttons": []}
    for i, f in enumerate(backend._frames(page)):
        try:
            info = f.evaluate(FRAME_TEXT)
        except Exception as exc:
            info = {"url": "?", "len": 0, "error": str(exc)[:120]}
        if info.get("len"):
            snap["frames"].append({"index": i, **info})
    try:
        snap["buttons"] = page.main_frame.evaluate(VISIBLE_BUTTONS)
    except Exception as exc:
        snap["buttons"] = [f"err {str(exc)[:80]}"]
    try:
        page.screenshot(path=str(outdir / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    return snap


def main() -> int:
    item_id = sys.argv[1] if len(sys.argv) > 1 else "01"
    cfg = Config.load("config.json")
    outdir = ROOT / "state" / "diag"
    outdir.mkdir(parents=True, exist_ok=True)

    items = [it for it in scan_source(cfg)
             if it.item_id == item_id or it.item_id.startswith(item_id)]
    if not items:
        print(f"未找到条目 {item_id}")
        return 2
    item = items[0]
    print(f"条目：{item.item_id} / {item.title}")
    print(f"视频：{item.video_path}（{item.video_size / 1024 / 1024:.2f}MB）")
    print(f"封面：{item.cover_path}")

    target = cfg.publish.resolve_scheduled_at()
    print(f"定时目标：{target}")

    backend = create_backend("channels_web", cfg)
    timeline: list[dict] = []
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        print("已进入「发表动态」页")

        backend._upload_video(page, item)
        print("视频已投递（_upload_video 返回）")
        timeline.append(snapshot(page, backend, "01_after_upload", outdir))

        backend._fill_description(page, item)
        backend._apply_schedule(page, item, {})
        if item.cover_path:
            backend._upload_cover(page, item)
        print("表单填写完成")

        timeline.append(snapshot(page, backend, "02_before_click", outdir))

        btn = backend._wait_button_enabled(page, "publish_btn", timeout_s=180)
        cls = btn.get_attribute("class") if btn else "<none>"
        print(f"发表按钮 class = {cls!r}")
        print(f"发表按钮 bbox  = {btn.bounding_box() if btn else None}")

        t0 = time.time()
        btn.click()
        print(f"[{time.time()-t0:5.2f}s] 已点击「发表」")
        for delay, name in ((0.8, "03_click_0.8s"), (2.0, "04_click_2s"),
                            (5.0, "05_click_5s"), (10.0, "06_click_10s")):
            time.sleep(max(0.0, delay - (time.time() - t0)))
            snap = snapshot(page, backend, name, outdir)
            timeline.append(snap)
            print(f"[{time.time()-t0:5.2f}s] {name}: url={snap['url']}")
            print(f"          buttons={snap['buttons']}")
            for fr in snap["frames"]:
                head = fr["text"][:300].replace("\n", " | ")
                print(f"          frame{fr['index']}: {head}")

        (outdir / "timeline.json").write_text(
            json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n时间线已写出：{outdir / 'timeline.json'}")
        print(f"截图目录：{outdir}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
