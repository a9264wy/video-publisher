#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：点侧边栏「草稿箱 / 视频」，确认内容到底落在哪个桶。

pushState 猜路由不可靠（``/platform/post/draft`` 渲染不出东西），
所以这里走**真实用户路径**：点菜单项。

⚠️ 只读。

用法：
    python scripts/inspect_drafts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

FRAME_TEXT = r"""() => {
  let t = '';
  try { t = (document.body ? document.body.innerText : '').trim(); } catch (e) {}
  return {url: location.href, len: t.length, text: t.slice(0, 2000)};
}"""


def dump(label: str, page, backend) -> dict:
    print(f"\n{'='*72}\n{label}  →  {page.url}\n{'='*72}")
    out = {"label": label, "url": page.url, "frames": []}
    for i, f in enumerate(backend._frames(page)):
        try:
            info = f.evaluate(FRAME_TEXT)
        except Exception as exc:
            info = {"url": "?", "error": str(exc)[:150], "len": 0}
        if info.get("len"):
            out["frames"].append({"index": i, **info})
            print(f"  frame[{i}] {info['url']} len={info['len']}")
            print("  " + info["text"][:800].replace("\n", "\n  "))
    return out


def main() -> int:
    cfg = Config.load("config.json")
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {}
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
                  timeout=60_000)
        for _ in range(15):
            page.wait_for_timeout(1000)
            if backend._is_logged_in(page):
                break
        page.wait_for_timeout(5000)

        for label in ("草稿箱", "视频"):
            links = page.locator("a.finder-ui-desktop-menu__link", has_text=label)
            n = links.count()
            print(f"\n[菜单] {label!r} 命中 {n} 个")
            clicked = False
            for i in range(n):
                try:
                    if not links.nth(i).is_visible():
                        continue
                    links.nth(i).click(timeout=8000)
                    clicked = True
                    break
                except Exception as exc:
                    print(f"  第 {i} 个点击失败：{str(exc)[:100]}")
            if not clicked:
                print("  未能点击")
                report[label] = {"clicked": False}
                continue
            page.wait_for_timeout(8000)
            report[label] = dump(label, page, backend)
            report[label]["clicked"] = True
            shot = ROOT / "state" / f"bucket_{label}.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"  截图：{shot}")
            # 回到首页再点下一个
            page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
                      timeout=60_000)
            page.wait_for_timeout(5000)

        out = ROOT / "state" / "inspect_drafts.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
