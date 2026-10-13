#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取**真实页面自己发出的** helper_upload_params 请求（请求体 + 全部请求头 + 响应），
和本地 preflight 手搓的请求做逐字段 diff。

之前所有结论都建立在「12 种手搓变体全部 300002」上，但从未证明
「手搓请求 == 页面真实请求」。如果真实请求也 300002，才坐实服务端问题；
如果真实请求和手搓的存在字段差异，那就是我们自己的问题。

只读操作：只拦截并放行，不修改任何请求。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "state" / "diag" / "real_request_capture.json"
MATCH = "**/helper/helper_upload_params*"


def main() -> int:
    from playwright.sync_api import sync_playwright

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cw = cfg["credentials"]["channels_web"]
    storage = ROOT / cw["storage_state"]

    captured: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(storage_state=str(storage), viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        def on_route(route):
            req = route.request
            try:
                body = req.post_data
            except Exception as e:  # noqa: BLE001
                body = f"<post_data error: {e}>"
            entry = {
                "url": req.url,
                "method": req.method,
                "headers": dict(req.headers),
                "post_data": body,
            }
            resp = route.fetch()
            try:
                entry["status"] = resp.status
                entry["response_headers"] = dict(resp.headers)
                entry["response_body"] = resp.text()
            except Exception as e:  # noqa: BLE001
                entry["response_body"] = f"<resp error: {e}>"
            captured.append(entry)
            print(f"[capture] {entry['url'][:110]}")
            print(f"          -> status={entry.get('status')} body={str(entry.get('response_body'))[:120]}")
            route.fulfill(response=resp)

        page.route(MATCH, on_route)

        print("== 打开后台首页 ==")
        page.goto("https://channels.weixin.qq.com/platform", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        print("== 客户端路由进发布页 ==")
        page.evaluate("""() => {
            history.pushState({}, '', '/platform/post/create');
            window.dispatchEvent(new PopStateEvent('popstate'));
        }""")
        page.wait_for_timeout(6000)

        # 再补一刀：直接在页面上下文里用 fetch 复刻一次（同源、同 cookie、同 UA）
        print("== 页面内 fetch 复刻 ==")
        in_page = page.evaluate("""async () => {
            const r = await fetch('/cgi-bin/mmfinderassistant-bin/helper/helper_upload_params?_aid=' + crypto.randomUUID() + '&_rid=' + crypto.randomUUID() + '&_pageUrl=' + encodeURIComponent(location.href), {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({timestamp: String(Date.now()), _log_finder_uin: '', _log_finder_id: localStorage.getItem('finder_username') || '', rawKeyBuff: '', pluginSessionId: null, scene: 7, reqScene: 7})
            });
            return {status: r.status, body: await r.text()};
        }""")
        print("in-page fetch:", json.dumps(in_page, ensure_ascii=False)[:300])

        browser.close()

    out = {"captured": captured, "in_page_fetch": in_page, "ts": time.time()}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存 {len(captured)} 条真实请求 -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
