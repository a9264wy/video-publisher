#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复现「上传完成后弹二维码对话框」并落盘内容。

流程：进发布页 → 投递视频 → 等上传完成 → 等 5s →
截屏 + 把页面上所有 dialog 的文本/结构存下来。不提交。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.scanner import scan_source  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

OUT = ROOT / "state" / "diag" / "qr_dialog.json"


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    items = scan_source(cfg)
    item = items[0]

    be = create_backend("channels_web", cfg)
    be._launch()
    page = be._page
    try:
        be._enter_create_page(page)
        be._attach_upload_probe(page)
        be._upload_video(page, item)
        page.wait_for_timeout(5000)

        page.screenshot(path=str(ROOT / "state" / "diag" / "qr_dialog.png"), full_page=True)

        dialogs = page.evaluate("""() => {
            const out = [];
            const grab = (root, tag) => {
                for (const el of root.querySelectorAll('.weui-desktop-dialog__wrp, [class*=dialog]')) {
                    const r = el.getBoundingClientRect();
                    out.push({
                        frame: tag,
                        cls: String(el.className || '').slice(0, 80),
                        visible: r.width > 0 && r.height > 0,
                        text: (el.innerText || '').trim().slice(0, 500),
                        hasQR: !!el.querySelector('.qr-code-img, canvas, img[src^="data"]'),
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
        }""")
        OUT.write_text(json.dumps(dialogs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"捕获 {len(dialogs)} 个 dialog 元素 -> {OUT}")
        for d in dialogs:
            if d["visible"] or d["hasQR"] or d["text"]:
                print(f"\n=== [{ '可见' if d['visible'] else '隐藏'}] QR={d['hasQR']} {d['frame']} {d['cls']}")
                print(d["text"][:400])
    finally:
        try:
            be.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
