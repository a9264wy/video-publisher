#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探查封面「编辑」弹窗与「位置」控件结构。

流程：进发布页 → 投递视频 → 等上传完成 →
依次点开「个人主页卡片 3:4」和「分享卡片 4:3」的「编辑」，
dump 弹窗结构 + 截图，再 Escape 关闭。不提交。
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

OUT = ROOT / "state" / "diag" / "cover_dialog.json"

DUMP_JS = r"""() => {
  const out = [];
  const grab = (root, tag) => {
    for (const el of root.querySelectorAll('[class*=dialog], [class*=cover], [class*=modal], [class*=popover]')) {
      const r = el.getBoundingClientRect();
      if (!(r.width > 50 && r.height > 30)) continue;
      out.push({
        frame: tag,
        cls: String(el.className || '').slice(0, 90),
        rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        text: (el.innerText || '').trim().slice(0, 300),
        inputs: Array.from(el.querySelectorAll('input[type=file], button, .weui-desktop-btn')).map(b => ({
          tag: b.tagName.toLowerCase(),
          cls: String(b.className || '').slice(0, 60),
          text: (b.innerText || '').trim().slice(0, 30),
          accept: b.accept || undefined,
        })),
      });
    }
  };
  grab(document, 'main');
  for (const f of document.querySelectorAll('iframe')) {
    try { grab(f.contentDocument, 'iframe:' + f.src.slice(0, 50)); } catch (e) {}
  }
  for (const h of document.querySelectorAll('*')) {
    if (h.shadowRoot) { try { grab(h.shadowRoot, 'shadow:' + h.tagName.toLowerCase()); } catch (e) {} }
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
        page.wait_for_timeout(1000)

        # 位置控件当前状态
        loc_texts = be._dump_state(page, "cover_probe")  # 顺便留档
        result["frames"] = loc_texts.get("frames", [])[:6]

        for slot in ("3:4", "4:3"):
            # 找到对应封面卡片的「编辑」按钮
            sel = f"text=个人主页卡片" if slot == "3:4" else "text=分享卡片"
            # 编辑按钮是卡片 hover 出现的；卡片容器是 label/编辑 的父级。
            # 直接点可见的「编辑」（有两处，按容器文本定位）：
            clicked = False
            for f in be._frames(page):
                try:
                    cards = f.locator("text=编辑")
                    n = min(cards.count(), 10)
                    for i in range(n):
                        cand = cards.nth(i)
                        try:
                            if not cand.is_visible():
                                continue
                            # 父容器含 3:4 / 4:3 判定归属
                            card_txt = ""
                            try:
                                card_txt = cand.evaluate(
                                    "el => (el.closest('div[class*=cover], label, .form-item')?.innerText || '').slice(0, 60)")
                            except Exception:
                                pass
                            if slot not in card_txt and slot not in (card_txt.replace("3：4", "3:4")):
                                continue
                            cand.click(timeout=4_000)
                            clicked = True
                            result.setdefault("clicked", []).append({"slot": slot, "card": card_txt})
                            break
                        except Exception:
                            continue
                    if clicked:
                        break
                except Exception:
                    continue
            print(f"点开 {slot} 封面编辑：{clicked}")
            page.wait_for_timeout(2500)
            page.screenshot(path=str(ROOT / "state" / "diag" / f"cover_dialog_{slot.replace(':', 'x')}.png"))
            result[slot] = page.evaluate(DUMP_JS)
            # 关闭弹窗
            for f in be._frames(page):
                try:
                    f.locator("body").press("Escape", timeout=1_000)
                except Exception:
                    pass
            page.wait_for_timeout(800)

        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已保存 -> {OUT}")
        for slot in ("3:4", "4:3"):
            print(f"\n===== {slot} 弹窗命中 {len(result.get(slot) or [])} 个容器 =====")
            for d in (result.get(slot) or [])[:6]:
                print(f"  [{d['frame']}] {d['cls']}")
                print(f"    text: {d['text'][:120]!r}")
                for b in d["inputs"][:8]:
                    print(f"    <{b['tag']} {b['cls'][:40]}> {b['text']!r} accept={b.get('accept')}")
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
