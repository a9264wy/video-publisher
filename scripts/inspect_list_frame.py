#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""只读探查：进入「视频管理」后，逐 frame 挖出内容列表（含 shadow root）。

上一轮结论：pushState 到 ``/platform/post/list`` 后，子 frame 确实变成了
``/micro/content/post/list``，但 ``_page_text`` 只拿到 10 个字符 —— 说明
列表内容不在 light DOM 的可读文本里（大概率又是一层 shadow root）。

⚠️ 只读。

用法：
    python scripts/inspect_list_frame.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

PROBE = r"""() => {
  let lightText = '';
  try { lightText = (document.body ? document.body.innerText : '').trim(); } catch (e) {}
  const shadowTexts = [];
  const stack = [document];
  const seen = new Set();
  while (stack.length) {
    const root = stack.pop();
    let all = [];
    try { all = root.querySelectorAll('*'); } catch (e) { continue; }
    for (const el of all) {
      if (el.shadowRoot && !seen.has(el)) {
        seen.add(el);
        let t = '';
        try { t = (el.shadowRoot.textContent || '').trim(); } catch (e) {}
        if (t) shadowTexts.push({host: el.tagName + '.' + String(el.className||'').slice(0,40),
                                 len: t.length, text: t.slice(0, 1500)});
        stack.push(el.shadowRoot);
      }
    }
  }
  return {
    url: location.href,
    lightLen: lightText.length,
    lightText: lightText.slice(0, 1200),
    shadowHosts: seen.size,
    shadowTexts: shadowTexts.slice(0, 6)
  };
}"""


def main() -> int:
    cfg = Config.load("config.json")
    backend = create_backend("channels_web", cfg)
    report: dict[str, object] = {"frames": []}
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
        page.wait_for_timeout(4000)

        page.evaluate("() => { history.pushState({}, '', '/platform/post/list');"
                      " window.dispatchEvent(new PopStateEvent('popstate')); }")
        page.wait_for_timeout(8000)

        for i, f in enumerate(backend._frames(page)):
            try:
                info = f.evaluate(PROBE)
            except Exception as exc:
                info = {"url": getattr(f, "url", "?"), "error": str(exc)[:200]}
            info["index"] = i
            report["frames"].append(info)
            print(f"\n{'='*70}\nframe[{i}] {info.get('url')}\n{'='*70}")
            print(f"  lightLen={info.get('lightLen')} shadowHosts={info.get('shadowHosts')}")
            if info.get("lightText"):
                print(f"  --- light ---\n{info['lightText'][:800]}")
            for st in (info.get("shadowTexts") or []):
                print(f"  --- shadow <{st['host']}> len={st['len']} ---")
                print(f"  {st['text'][:800]}")

        page.screenshot(path=str(ROOT / "state" / "list_frame.png"), full_page=True)
        print(f"\n截图：{ROOT / 'state' / 'list_frame.png'}")
        out = ROOT / "state" / "inspect_list_frame.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出：{out}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
