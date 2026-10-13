#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：监听投递视频后的网络请求，看上传到底有没有发出去。

现象：``set_input_files`` 后上传区停在 ``0% / 取消上传``，平台点发表回
「请上传视频」。文件和代理都已排除，所以直接看网络层。

记录：
  * 所有 XHR/fetch 与 POST 请求（方法、URL、资源类型、post 长度）
  * 失败的请求及失败原因（这是最有价值的）
  * 非 2xx 响应

⚠️ 只投递文件，不点提交。

用法：
    python scripts/probe_network.py [seconds]
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

SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff", ".woff2", ".ttf",
            ".css", ".ico", ".webp")
INTERESTING = ("upload", "post", "media", "video", "material", "cgi", "chunk",
               "create", "file", "aegis", "trpc")


def interesting(url: str) -> bool:
    u = url.lower()
    if u.endswith(SKIP_EXT):
        return False
    return True


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    cfg = Config.load("config.json")
    items = scan_source(cfg)
    item = items[0]

    backend = create_backend("channels_web", cfg)
    events: list[dict] = []
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()

        def on_request(req):
            try:
                if not interesting(req.url):
                    return
                pd = None
                try:
                    pd = req.post_data_length if req.post_data_length else None
                except Exception:
                    pass
                events.append({"ev": "request", "t": round(time.time(), 2),
                               "method": req.method, "type": req.resource_type,
                               "url": req.url[:220], "post_len": pd})
            except Exception:
                pass

        def on_response(resp):
            try:
                if not interesting(resp.url):
                    return
                if 200 <= resp.status < 300:
                    return
                events.append({"ev": "response_non2xx", "t": round(time.time(), 2),
                               "status": resp.status, "url": resp.url[:220]})
            except Exception:
                pass

        def on_failed(req):
            try:
                fail = None
                try:
                    fail = req.failure
                except Exception:
                    pass
                events.append({"ev": "requestfailed", "t": round(time.time(), 2),
                               "method": req.method, "url": req.url[:220],
                               "failure": str(fail)})
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)

        events.append({"ev": "--- set_input_files ---", "t": round(time.time(), 2)})
        loc = backend._locate(page, "file_input")
        loc.set_input_files(item.video_path)
        print(f"已投递 {Path(item.video_path).name}，监听 {seconds}s …")
        time.sleep(seconds)

        (ROOT / "state" / "network.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n共记录 {len(events)} 条事件：\n")
        for e in events:
            if e["ev"] == "request":
                print(f"  [REQ ] {e['method']:5s} {e['type']:8s} "
                      f"post={e.get('post_len')} {e['url']}")
            elif e["ev"] == "requestfailed":
                print(f"  [FAIL] {e['method']:5s} {e['url']}\n         → {e['failure']}")
            elif e["ev"] == "response_non2xx":
                print(f"  [HTTP] {e['status']} {e['url']}")
            else:
                print(f"  ---- {e['ev']} ----")
        print(f"\n已写出：{ROOT / 'state' / 'network.json'}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
