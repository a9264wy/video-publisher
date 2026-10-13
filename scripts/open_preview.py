#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打开有头浏览器，用已登录账号进视频号助手管理页，供人工预览。

行为：
  1. 复用 state/channels_storage.json 登录态启动系统 Chrome（有头）；
  2. 进 /platform/post/list 作品管理页，等列表渲染；
  3. 打印当前定时作品摘要（应与排期一致）；
  4. 然后挂住进程（默认 7200 秒）保持窗口不关，供人工查看；
     期间登录态过期会提示，用户可直接在窗口里扫码续期。

用法：
    .venv/Scripts/python.exe scripts/open_preview.py            # 挂 2 小时
    .venv/Scripts/python.exe scripts/open_preview.py 1800       # 挂 30 分钟
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from videopub.config import Config  # noqa: E402
from videopub.backends import create_backend  # noqa: E402

HOLD_SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 7200

_stop = False


def _on_signal(signum, frame):  # noqa: ANN001
    global _stop
    _stop = True
    print(f"\n收到信号 {signum}，正在关闭浏览器……")


def open_list_page(be) -> None:
    page = be._page
    page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded",
              timeout=60_000)
    page.wait_for_timeout(5_000)
    page.evaluate("""() => {
        history.pushState({}, '', '/platform/post/list');
        window.dispatchEvent(new PopStateEvent('popstate'));
    }""")
    page.wait_for_timeout(6_000)


def collect_cards(be) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    frames = [f for f in be._frames(be._page) if f is not be._page.main_frame] + [be._page.main_frame]
    for f in frames:
        try:
            cards = f.locator(".post-feed-item")
            n = min(cards.count(), 60)
        except Exception:
            continue
        for i in range(n):
            cand = cards.nth(i)
            try:
                txt = (cand.inner_text() or "").strip()
            except Exception:
                continue
            if not txt or txt in seen:
                continue
            seen.add(txt)
            first = txt.splitlines()[0].strip() if txt.splitlines() else ""
            when = ""
            for line in txt.splitlines():
                line = line.strip()
                if line.startswith("将于"):
                    when = line
                    break
            out.append({"title": first, "when": when, "scheduled": bool(when)})
    return out


def main() -> int:
    cfg = Config.load(ROOT / "config.json")
    be = create_backend("channels_web", cfg)
    be._launch(headless=False)  # 强制有头，供人工预览
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        open_list_page(be)
        url = be._page.url
        logged_in = be._has_login_cookie(be._context)
        print(f"当前地址: {url}")
        print(f"登录态: {'有效' if logged_in else '疑似失效（如页面要求扫码，请在窗口里直接扫）'}")

        cards = collect_cards(be)
        sched = sorted([c for c in cards if c["scheduled"]], key=lambda x: (x["when"], x["title"]))
        print(f"\n== 管理页卡片 {len(cards)} 张，其中定时未发表 {len(sched)} 条 ==")
        for c in sched:
            print(f"  {c['when']:<26s} {c['title']}")

        print(f"\n浏览器窗口已打开，挂住 {HOLD_SECONDS} 秒供预览……")
        print("（关闭窗口或 Ctrl-C 即可结束；此进程为只读预览，不会发布/删除任何内容）")
        t0 = time.time()
        while not _stop and time.time() - t0 < HOLD_SECONDS:
            # 顺带监控登录态是否掉线
            time.sleep(30)
            try:
                if not be._has_login_cookie(be._context):
                    print(f"[{time.strftime('%H:%M:%S')}] 警告：登录态可能已失效，"
                          f"如需继续查看请在窗口里重新扫码")
            except Exception:
                pass
        print("预览结束，关闭浏览器。")
        return 0
    finally:
        try:
            be.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
