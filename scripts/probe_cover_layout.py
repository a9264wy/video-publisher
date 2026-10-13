#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探查封面预览区布局：每个可见「编辑」候选的精确属性 + 点击后的弹窗归属。

要回答的问题：
  1. x=680 / x=770 这两个「编辑」到底是什么元素（tag/class/opacity/归属卡片）？
  2. 4:3「分享卡片」的编辑按钮到底在哪、为什么 force 点击无反应？
  3. hover 会不会改变候选集合（编辑按钮疑似 hover 才显示）？

流程：进发布页 → 投递视频 → 等上传完成 → dump 布局 →
对每个候选：hover 记录 mask-layer → force click → dump 所有弹窗 → Escape。
不提交、不 set_input_files。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "cover_layout.json"

# 收集所有可见「编辑」候选的详细属性（含计算样式，判断 opacity/pointer-events）
CAND_JS = r"""() => {
  const out = [];
  const walk = (root, tag) => {
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, tag + '/shadow');
      // 只收文本恰为「编辑」的叶子元素
      const own = Array.from(el.childNodes)
        .filter(n => n.nodeType === 3)
        .map(n => n.textContent.trim()).join('');
      if (own !== '编辑') continue;
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      // 归属容器：最近的含 cover 的祖先
      let card = el.closest('[class*=cover], [class*=card], label, .form-item');
      out.push({
        frame: tag,
        tag: el.tagName.toLowerCase(),
        cls: String(el.className || '').slice(0, 80),
        rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        opacity: st.opacity, pointerEvents: st.pointerEvents,
        cardText: card ? (card.innerText || '').trim().slice(0, 80) : '',
        cardCls: card ? String(card.className || '').slice(0, 80) : '',
      });
    }
  };
  walk(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { walk(f.contentDocument, 'iframe'); } catch (e) {}
  }
  out.sort((a, b) => a.rect[0] - b.rect[0]);
  return out;
}"""

# 当前可见的 mask-layer（hover 遮罩）坐标
MASK_JS = r"""() => {
  const out = [];
  const walk = (root, tag) => {
    let all = [];
    try { all = root.querySelectorAll('div[class*=mask], [class*=mask-layer]'); } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, tag);
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      if (!(r.width > 0 && r.height > 0)) continue;
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
      out.push({frame: tag, cls: String(el.className || '').slice(0, 60),
                rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]});
    }
  };
  walk(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { walk(f.contentDocument, 'iframe'); } catch (e) {}
  }
  return out;
}"""

# 所有可见弹窗（.weui-desktop-dialog）标题 + 坐标
DIALOG_JS = r"""() => {
  const out = [];
  const walk = (root, tag) => {
    let all = [];
    try { all = root.querySelectorAll('.weui-desktop-dialog'); } catch (e) { return; }
    for (const el of all) {
      if (el.shadowRoot) walk(el.shadowRoot, tag);
      const r = el.getBoundingClientRect();
      const st = getComputedStyle(el);
      if (!(r.width > 0 && r.height > 0)) continue;
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      out.push({frame: tag, rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                text: (el.innerText || '').trim().slice(0, 120)});
    }
  };
  walk(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { walk(f.contentDocument, 'iframe'); } catch (e) {}
  }
  return out;
}"""


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    item = scan_source(cfg)[0]
    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    result: dict = {}
    try:
        be._enter_create_page(page)
        be._attach_upload_probe(page)
        be._upload_video(page, item)
        be._dismiss_blocking_dialogs(page)
        page.wait_for_timeout(2000)
        result["candidates_initial"] = page.evaluate(CAND_JS)
        page.screenshot(path=str(ROOT / "state" / "diag" / "cover_layout.png"), full_page=True)

        # 逐个候选：hover → 记录 mask → force click → 记录弹窗 → Escape
        trials = []
        cands = result["candidates_initial"]
        for idx, c in enumerate(cands):
            t = {"idx": idx, "cand": c}
            cx = c["rect"][0] + c["rect"][2] // 2
            cy = c["rect"][1] + c["rect"][3] // 2
            try:
                page.mouse.move(cx, cy)
                page.wait_for_timeout(900)
                t["masks_after_hover"] = page.evaluate(MASK_JS)
                # 用 playwright 在该坐标派发真实点击
                page.mouse.click(cx, cy)
                page.wait_for_timeout(1800)
                t["dialogs_after_click"] = page.evaluate(DIALOG_JS)
            except Exception as exc:  # noqa: BLE001
                t["error"] = str(exc)[:120]
            page.screenshot(path=str(ROOT / "state" / "diag" / f"cover_click_{idx}.png"), full_page=True)
            # 关闭弹窗
            for f in be._frames(page):
                try:
                    f.locator("body").press("Escape", timeout=800)
                except Exception:
                    pass
            page.wait_for_timeout(900)
            t["dialogs_after_escape"] = page.evaluate(DIALOG_JS)
            trials.append(t)
            print(f"[{idx}] x={c['rect'][0]} cls={c['cls'][:30]!r} card={c['cardText'][:30]!r} "
                  f"→ 弹窗 {len(t.get('dialogs_after_click') or [])} 个: "
                  f"{(t.get('dialogs_after_click') or [{}])[0].get('text', '')[:30]!r}")
        result["trials"] = trials
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已保存 -> {OUT}")
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
