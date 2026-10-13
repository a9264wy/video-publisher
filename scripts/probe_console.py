#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断脚本：抓控制台日志 + 错误上报的请求体，定位 rhino(FFmpeg-WASM) 为什么失败。

上一步网络监听发现：
  * 投递视频后页面去拉 ``aladin.wxqcloud.qq.com/aladin/ffmepeg/rhino-media-suite/
    1.5.18/rhino_video.wasm``（FFmpeg 编译成 WASM）与 ``worker_release.js``；
  * 紧接着出现 ``astra-web.weixin.qq.com/astra/report-error`` 与
    ``.../report-error`` 请求 —— SDK 在自己的 worker 里**报错了**；
  * 但没有任何 requestfailed / 非 2xx，所以是纯前端逻辑失败。

本脚本抓：
  1. ``console`` 消息（worker 里的报错常在这里）；
  2. ``pageerror`` 未捕获异常；
  3. 所有 URL 含 report-error / report 的 POST **请求体**（里面通常带原因）。

⚠️ 只投递文件，不点提交。

用法：
    python scripts/probe_console.py [seconds]
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


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 45
    cfg = Config.load("config.json")
    item = scan_source(cfg)[0]

    backend = create_backend("channels_web", cfg)
    logs: list[dict] = []
    reports: list[dict] = []
    try:
        ctx = backend._launch(headless=True)
        page = backend._page or ctx.new_page()

        def on_console(msg):
            try:
                txt = msg.text
                # 过滤掉噪音（大量 SDK 的 debug 输出）
                if any(k in txt for k in ("rhino", "ffmpeg", "FFmpeg", "wasm", "WASM",
                                          "worker", "Worker", "Error", "error",
                                          "fail", "Fail", "media", "video", "decode")):
                    logs.append({"type": msg.type, "loc": str(msg.location)[:120],
                                 "text": txt[:600]})
            except Exception:
                pass

        def on_pageerror(err):
            logs.append({"type": "pageerror", "text": str(err)[:800]})

        def on_request(req):
            try:
                u = req.url
                if "report-error" not in u and "/report" not in u:
                    return
                body = None
                try:
                    body = req.post_data
                except Exception:
                    pass
                reports.append({"url": u[:200], "len": len(body) if body else 0,
                                "body": (body or "")[:1500]})
            except Exception:
                pass

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("request", on_request)

        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)

        logs.append({"type": "---", "text": "set_input_files"})
        backend._locate(page, "file_input").set_input_files(item.video_path)
        print(f"已投递，抓 {seconds}s 日志 …")
        time.sleep(seconds)

        (ROOT / "state" / "console_log.json").write_text(
            json.dumps({"logs": logs, "reports": reports}, ensure_ascii=False, indent=2),
            encoding="utf-8")

        print(f"\n{'='*72}\n控制台相关日志（{len(logs)} 条）\n{'='*72}")
        for e in logs:
            tag = e.get("type", "?")
            loc = f" @{e['loc']}" if e.get("loc") else ""
            print(f"  [{tag}]{loc}\n    {e['text']}")
        print(f"\n{'='*72}\n错误上报请求体（{len(reports)} 条）\n{'='*72}")
        for r in reports:
            print(f"  {r['url']} (len={r['len']})\n    {r['body'][:1200]}")
        print(f"\n已写出：{ROOT / 'state' / 'console_log.json'}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
