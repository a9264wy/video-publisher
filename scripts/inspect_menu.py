#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：列出视频号助手侧边栏所有菜单项（文案 + href），并进入「视频管理」。

⚠️ 只读：只导航与读取，不修改任何内容。

用法：
    python scripts/inspect_menu.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

MENU_JS = r"""() => {
  const out = [];
  const seen = new Set();
  function walk(root, depth) {
    if (!root || depth > 12) return;
    let nodes = [];
    try { nodes = root.querySelectorAll('a,li,div[class*=menu]'); } catch (e) { return; }
    for (const el of nodes) {
      const cls = String(el.getAttribute('class') || '');
      if (!/menu/i.test(cls)) continue;
      if (seen.has(el)) continue;
      seen.add(el);
      const t = (el.innerText || '').trim().replace(/\n/g, ' ').slice(0, 24);
      out.push({
        tag: el.tagName,
        cls: cls.slice(0, 70),
        href: el.getAttribute('href') || '',
        txt: t,
        w: Math.round(el.getBoundingClientRect().width),
        h: Math.round(el.getBoundingClientRect().height)
      });
    }
    let hosts = [];
    try { hosts = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of hosts) if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
  }
  walk(document, 0);
  return out.filter(x => x.w > 0 && x.h > 0);
}"""


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
        page.wait_for_timeout(6000)

        # 主 frame 与各子 frame 都扫一遍
        menu: list[dict] = []
        for f in backend._frames(page):
            try:
                items = f.evaluate(MENU_JS)
            except Exception:
                continue
            for it in items:
                it["frame_url"] = f.url
                menu.append(it)
        # 按位置排序去重
        uniq: dict[tuple, dict] = {}
        for it in menu:
            key = (it["txt"], it["href"], it["cls"])
            uniq.setdefault(key, it)
        menu = sorted(uniq.values(), key=lambda x: (x["frame_url"], x["tag"], x["cls"]))
        report["menu"] = menu

        print(f"\n侧边栏候选项 {len(menu)} 个：")
        for it in menu:
            print(f"  <{it['tag']}> {it['txt']!r:28s} href={it['href']!r:40s} "
                  f"cls={it['cls']!r} {it['w']}x{it['h']}")

        # 逐个点击菜单项，记录 URL / 是否出现内容列表
        links = page.locator("a.finder-ui-desktop-menu__link")
        n = links.count()
        print(f"\n主要菜单项 a.finder-ui-desktop-menu__link 共 {n} 个")
        visited = []
        for i in range(n):
            try:
                txt = (links.nth(i).inner_text() or "").strip()
            except Exception:
                txt = ""
            href = links.nth(i).get_attribute("href") or ""
            visited.append({"idx": i, "text": txt, "href": href})
            print(f"  [{i}] {txt!r} href={href!r}")

        report["links"] = visited

        # 进入「视频管理」——通常文案含「视频管理」/「内容管理」
        for i, info in enumerate(visited):
            if any(k in info["text"] for k in ("视频管理", "内容管理", "作品管理", "动态管理")):
                print(f"\n点击菜单项 [{i}] {info['text']!r} …")
                links.nth(i).click()
                page.wait_for_timeout(7000)
                print(f"  当前 URL = {page.url}")
                text = backend._page_text(page)
                report["target_url"] = page.url
                report["target_text"] = text
                print(f"  文案长度 {len(text)}")
                print(f"  ---- 前 2000 字 ----\n{text[:2000]}")
                shot = ROOT / "state" / "menu_target.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"  截图：{shot}")
                break

        out = ROOT / "state" / "inspect_menu.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
