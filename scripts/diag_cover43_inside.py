#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""深挖 4:3 封面点不开：在 iframe 内部做 elementFromPoint。

主文档的 elementFromPoint 只到 wujie-app（iframe 容器）为止，
真正接收事件的是 iframe 内部文档。这个脚本在 iframe 内部：
  1. 找 4:3「编辑」按钮的真实 rect（iframe 坐标）
  2. elementFromPoint 取该点最深元素 + 逐级祖先链（含 pointer-events）
  3. hover 前后各来一次，看浮层（img-popover）有没有出现
  4. 顺便看有没有 toast/遮罩残留

流程同 diag_cover43：上传视频 → 3:4 封面上传 → 诊断 4:3。不提交。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

INSIDE_JS = r"""(pt) => {
  const out = {scroll: [window.scrollX, window.scrollY], chain: [], popovers: [], toasts: []};
  const el = document.elementFromPoint(pt[0], pt[1]);
  let cur = el, depth = 0;
  while (cur && depth < 8) {
    const r = cur.getBoundingClientRect();
    const st = getComputedStyle(cur);
    out.chain.push({
      tag: cur.tagName.toLowerCase(),
      cls: String(cur.className || '').slice(0, 70),
      pe: st.pointerEvents, opacity: st.opacity, display: st.display,
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      text: (cur.innerText || '').trim().slice(0, 30),
    });
    cur = cur.parentElement;
    depth++;
  }
  for (const e of document.querySelectorAll('[class*=popover], [class*=toast], [class*=mask], [class*=dialog]')) {
    const r = e.getBoundingClientRect();
    const st = getComputedStyle(e);
    if (!(r.width > 0 && r.height > 0)) continue;
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const rec = {cls: String(e.className || '').slice(0, 60), pe: st.pointerEvents,
                 rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                 text: (e.innerText || '').trim().slice(0, 40)};
    if (String(e.className).includes('popover')) out.popovers.push(rec);
    else if (String(e.className).includes('toast')) out.toasts.push(rec);
    else out.toasts.push(rec);
  }
  return out;
}"""


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    item = scan_source(cfg)[0]
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    try:
        be._enter_create_page(page)
        be._attach_upload_probe(page)
        be._upload_video(page, item)
        be._dismiss_blocking_dialogs(page)
        page.wait_for_timeout(2000)
        be._clear_location(page)

        folder = Path(item.cover_path).parent
        vertical = be._find_cover_file(folder, ("3x4", "3：4", "3X4"))
        be._set_cover_card(page, "vertical-cover-wrap", vertical)
        print("3:4 封面流程结束")

        # 找 post/create iframe（活副本）
        create_frame = None
        for f in page.frames:
            if "micro/content/post/create" in (f.url or ""):
                create_frame = f
                break
        if create_frame is None:
            print("!! 没找到 post/create iframe")
            return 1
        print("iframe url:", create_frame.url[:70])

        targets = be._cover_edit_targets(page)
        t43 = [t for t in targets if "分享卡片" in (t.get("cardText") or "")]
        rect = t43[0]["rect"]
        cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        # iframe 自身偏移（一般是 0）
        fr = page.evaluate("""() => {
            for (const f of document.querySelectorAll('iframe')) {
                if ((f.src || '').includes('post/create')) {
                    const r = f.getBoundingClientRect();
                    return [r.x, r.y];
                }
            }
            return [0, 0];
        }""")
        ix, iy = cx - fr[0], cy - fr[1]
        print(f"4:3 按钮 main 坐标=({cx},{cy}) iframe 偏移={fr} → iframe 坐标=({ix},{iy})")

        for phase in ("点击前", "hover 800ms 后", "click 后 1800ms"):
            if phase == "hover 800ms 后":
                page.mouse.move(cx, cy)
                page.wait_for_timeout(800)
            elif phase == "click 后 1800ms":
                page.mouse.click(cx, cy)
                page.wait_for_timeout(1800)
            print(f"\n== {phase} ==")
            r = create_frame.evaluate(INSIDE_JS, [ix, iy])
            print("  iframe scroll:", r["scroll"])
            for c in r["chain"][:5]:
                print(f"    <{c['tag']} {c['cls'][:46]}> pe={c['pe']} op={c['opacity']} rect={c['rect']} {c['text'][:16]!r}")
            for p in r["popovers"][:4]:
                print(f"    popover: {p['cls'][:40]} pe={p['pe']} rect={p['rect']}")
            for t in r["toasts"][:4]:
                print(f"    toast/mask: {t['cls'][:40]} pe={t['pe']} rect={t['rect']} {t['text'][:20]!r}")
            if phase == "click 后 1800ms":
                print("  弹窗文本:", repr((be._visible_cover_dialog_text(page) or "")[:50]))
        page.screenshot(path=str(ROOT / "state" / "diag" / "cover43_inside.png"), full_page=True)
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
