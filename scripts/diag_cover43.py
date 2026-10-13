#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复现完整发布流程到 4:3 封面前一步，然后诊断为什么 4:3「编辑」点不开。

流程：进发布页 → 上传视频 → 3:4 封面上传（同 publish 流程）→
诊断：elementFromPoint(4:3按钮中心) 是什么、有哪些浮层、
mouse.move 后浮层怎么变、click 后弹窗为什么没出。

结论直接打印，不提交、不发布。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

# 某坐标点上真正接收事件的元素 + 可见浮层清单
PROBE_JS = r"""(pt) => {
  const out = {at: [], overlays: [], btn: null};
  const el = document.elementFromPoint(pt[0], pt[1]);
  if (el) {
    out.at.push({
      tag: el.tagName.toLowerCase(),
      cls: String(el.className || '').slice(0, 80),
      text: (el.innerText || '').trim().slice(0, 40),
      rect: (r => [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)])(el.getBoundingClientRect()),
    });
  }
  const walk = (root, tag) => {
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (const e of all) {
      if (e.shadowRoot) walk(e.shadowRoot, tag);
      const r = e.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const st = getComputedStyle(e);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
      // 只报「浮层感」的元素：fixed/absolute 定位且面积不小
      if (st.position !== 'fixed' && st.position !== 'absolute') continue;
      if (r.width < 200 || r.height < 100) continue;
      // 是否盖住目标点
      const covers = pt[0] >= r.x && pt[0] <= r.x + r.width && pt[1] >= r.y && pt[1] <= r.y + r.height;
      if (!covers) continue;
      out.overlays.push({
        frame: tag, cls: String(e.className || '').slice(0, 70),
        zIndex: st.zIndex, pointerEvents: st.pointerEvents,
        rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        text: (e.innerText || '').trim().slice(0, 50),
      });
    }
  };
  walk(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { walk(f.contentDocument, 'iframe'); } catch (e) {}
  }
  // 4:3 编辑按钮本体状态
  const find = (root, tag) => {
    for (const e of root.querySelectorAll('*')) {
      if (e.shadowRoot) find(e.shadowRoot, tag);
      const own = Array.from(e.childNodes).filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim()).join('');
      if (own !== '编辑') continue;
      const card = e.closest('[class*=cover], [class*=card], label, .form-item');
      const ct = card ? (card.innerText || '') : '';
      if (!ct.includes('分享卡片')) continue;
      const r = e.getBoundingClientRect();
      const st = getComputedStyle(e);
      out.btn = {frame: tag, cls: String(e.className || '').slice(0, 60),
                 rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                 opacity: st.opacity, pointerEvents: st.pointerEvents, display: st.display};
    }
  };
  find(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { find(f.contentDocument, 'iframe'); } catch (e) {}
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
        # 清位置（同流程）
        be._clear_location(page)

        # 3:4 封面上传（复用生产代码路径）
        folder = Path(item.cover_path).parent
        vertical = be._find_cover_file(folder, ("3x4", "3：4", "3X4"))
        be._set_cover_card(page, "vertical-cover-wrap", vertical)
        print("3:4 封面流程结束")

        # —— 诊断 4:3 ——
        targets = be._cover_edit_targets(page)
        t43 = [t for t in targets if "分享卡片" in (t.get("cardText") or "")]
        if not t43:
            print("!! 没找到 4:3 编辑按钮")
            return 1
        rect = t43[0]["rect"]
        cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        print(f"4:3 按钮 rect={rect} 点击点=({cx},{cy})")

        print("\n== 点击前 ==")
        r = page.evaluate(PROBE_JS, [cx, cy])
        print("  elementFromPoint:", r["at"])
        print("  btn:", r["btn"])
        for o in r["overlays"]:
            print(f"  overlay: {o['cls'][:50]} z={o['zIndex']} pe={o['pointerEvents']} rect={o['rect']}")

        page.mouse.move(cx, cy)
        page.wait_for_timeout(800)
        print("\n== hover 800ms 后 ==")
        r = page.evaluate(PROBE_JS, [cx, cy])
        print("  elementFromPoint:", r["at"])
        for o in r["overlays"]:
            print(f"  overlay: {o['cls'][:50]} z={o['zIndex']} pe={o['pointerEvents']} rect={o['rect']} text={o['text'][:24]!r}")

        page.mouse.click(cx, cy)
        page.wait_for_timeout(1800)
        print("\n== click 后 1800ms ==")
        dlg = be._visible_cover_dialog_text(page)
        print("  弹窗文本:", repr((dlg or "")[:60]))
        r = page.evaluate(PROBE_JS, [cx, cy])
        print("  elementFromPoint:", r["at"])
        for o in r["overlays"]:
            print(f"  overlay: {o['cls'][:50]} z={o['zIndex']} pe={o['pointerEvents']} rect={o['rect']}")
        page.screenshot(path=str(ROOT / "state" / "diag" / "cover43_diag.png"), full_page=True)
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
