#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓「上传参数」接口的响应体 + WebSocket 帧（定位上传为何不发车）。

已知：
  * 文件确实被 app 接受（控制台 ``@@@[nameTest]beforeUpload name: final.mp4``）
  * 本地视频初始化 / 首帧 / 封面均成功
  * ``POST /cgi-bin/mmfinderassistant-bin/helper/helper_upload_params`` 被**反复**调用
  * 但**没有任何** COS / 上传域名的请求 → 字节从未出网

本脚本回答：``helper_upload_params`` 到底返回了什么？返回体里有没有上传地址？
有没有被风控/权限拒绝？WebSocket（``auth_socket_init``）通不通？

⚠️ 只投递文件、不点提交 → 不会产生内容。

用法：.venv/Scripts/python.exe scripts/probe_upload_params.py [headless|headful]
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

OUT = ROOT / "state" / "diag"

INTEREST = ("helper_upload_params", "upload", "get_finder_post", "post_list",
            "auth_socket", "report-error")


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    headless = "headful" not in args
    cfg = Config.load("config.json")
    items = scan_source(cfg)
    item = items[0]
    OUT.mkdir(parents=True, exist_ok=True)

    backend = create_backend("channels_web", cfg)
    rep: dict = {"mode": "headless" if headless else "headful", "responses": [],
                 "ws": [], "console": [], "all_requests": []}
    try:
        ctx = backend._launch(headless=headless)
        page = backend._page or ctx.new_page()

        def on_ws(ws):
            rep["ws"].append({"event": "open", "url": ws.url})
            ws.on("framesent", lambda p: rep["ws"].append(
                {"event": "sent", "len": len(p), "data": str(p)[:400]}))
            ws.on("framereceived", lambda p: rep["ws"].append(
                {"event": "recv", "len": len(p), "data": str(p)[:400]}))
            ws.on("socketerror", lambda e: rep["ws"].append({"event": "error", "err": str(e)}))
            ws.on("close", lambda: rep["ws"].append({"event": "close"}))

        page.on("websocket", on_ws)
        page.on("console", lambda m: rep["console"].append(
            {"type": m.type, "text": (m.text or "")[:500]}))
        page.on("pageerror", lambda e: rep["console"].append(
            {"type": "pageerror", "text": str(e)[:500]}))

        def on_req(r):
            if len(rep["all_requests"]) < 400:
                rep["all_requests"].append({"m": r.method, "u": r.url[:250],
                                            "t": r.resource_type})

        page.on("request", on_req)

        def on_resp(r):
            if any(k in r.url for k in INTEREST):
                rec = {"status": r.status, "url": r.url[:250], "len": None, "body": ""}
                try:
                    b = r.body()
                    rec["len"] = len(b)
                    if b and len(b) < 4000 and "helper_upload_params" in r.url:
                        rec["body"] = b.decode("utf-8", "replace")
                except Exception as exc:
                    rec["body"] = f"<{type(exc).__name__}>"
                rep["responses"].append(rec)

        page.on("response", on_resp)

        backend.authenticate()
        backend._enter_create_page(page)
        page.wait_for_timeout(3000)
        rep["marker_before_set"] = len(rep["all_requests"])

        loc = backend._locate(page, "file_input")
        loc.set_input_files(item.video_path)
        page.wait_for_timeout(2000)

        # 之后 60 秒，全量记录（找上传 POST）
        for _ in range(12):
            page.wait_for_timeout(5000)

        # 关键：从 file input 的 locator 句柄直接读 files（穿透 shadow DOM）
        try:
            rep["files_readback"] = loc.evaluate(
                "el => el.files ? Array.from(el.files).map(f => [f.name, f.size, f.type]) : null")
        except Exception as exc:
            rep["files_readback"] = f"<{type(exc).__name__}: {exc}>"

        rep["ok"] = True
    except Exception as exc:  # noqa: BLE001
        import traceback
        rep["ok"] = False
        rep["fatal"] = f"{type(exc).__name__}: {exc}"
        rep["traceback"] = traceback.format_exc()[-2500:]
    finally:
        try:
            backend.close()
        except Exception:
            pass

    p = OUT / f"upload_params_{rep['mode']}.json"
    p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"ok={rep.get('ok')}  mode={rep['mode']}")
    print(f"files_readback = {json.dumps(rep.get('files_readback'), ensure_ascii=False)[:300]}")
    print("\n=== helper_upload_params / 相关接口响应 ===")
    for r in rep["responses"]:
        print(f"  {r['status']} len={r['len']} {r['url'][:120]}")
        if r["body"]:
            print(f"      body: {r['body'][:600]}")
    print("\n=== WebSocket ===")
    for w in rep["ws"][:30]:
        print(f"  {json.dumps(w, ensure_ascii=False)[:300]}")
    print("\n=== 投递后新增的请求（非 res.wx.qq.com）===")
    n0 = rep.get("marker_before_set", 0)
    seen = set()
    for r in rep["all_requests"][n0:]:
        if "res.wx.qq.com" in r["u"]:
            continue
        key = r["u"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        print(f"  {r['m']:5s} {r['t']:10s} {key[:120]}")
    print(f"\n报告：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
