#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证「上传态判定」与「上传参数探针」这两件生产代码里的仪表到底准不准。

动机：实盘时 ``_upload_video`` 判定「上传完成」并照常提交，但提交现场（截图 + 各
frame 文案）里明明写着 ``0% | 取消上传``。说明仪表在 lie。这个脚本把生产用的
``_upload_state`` / ``_upload_progress`` / ``_attach_upload_probe`` 直接拉出来跑，
每 3 秒打一次真值，看它们什么时候变 True、探针有没有命中。

⚠️ 只投递文件、不点提交 → 不会在账号上产生内容。

用法：.venv/Scripts/python.exe scripts/probe_gate.py [headless|headful]
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


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    headless = "headful" not in args
    cfg = Config.load("config.json")
    item = scan_source(cfg)[0]
    OUT.mkdir(parents=True, exist_ok=True)

    backend = create_backend("channels_web", cfg)
    rows: list[dict] = []
    # 也让底层把每次命中都记下来（生产探针内部会打 INFO 日志）
    seen_hits: list[str] = []

    try:
        ctx = backend._launch(headless=headless)
        page = backend._page or ctx.new_page()
        backend.authenticate()
        backend._enter_create_page(page)
        backend._attach_upload_probe(page)
        page.wait_for_timeout(2000)

        # 额外加一个「全量响应」监听，用来判断生产探针是不是根本没挂上
        all_resp: list[str] = []
        page.on("response", lambda r: all_resp.append(r.url) if "mmfinderassistant" in r.url else None)

        loc = backend._locate(page, "file_input")
        t0 = time.time()
        loc.set_input_files(item.video_path)
        print(f"[{0:5.1f}s] 已投递 {Path(item.video_path).name}")

        for i in range(30):   # 90s
            page.wait_for_timeout(3000)
            el = time.time() - t0
            # 生产代码用的两个判据
            try:
                state = backend._upload_state(page)
            except Exception as exc:
                state = f"<ERR {exc!r}>"
            try:
                prog = backend._upload_progress(page)
            except Exception as exc:
                prog = f"<ERR {exc!r}>"
            # 兜底真值：逐 frame 的 body.innerText 里直接找关键词
            raw = {}
            for idx, f in enumerate(backend._frames(page)):
                try:
                    t = f.inner_text("body") or ""
                except Exception:
                    t = ""
                if any(k in t for k in ("取消上传", "重新上传", "上传失败")):
                    raw[idx] = "UPLOADING"
                elif "视频描述" in t:
                    raw[idx] = "form-ok"
            row = {"t": round(el, 1), "state": state, "progress": prog[:5], "raw": raw,
                   "param_err": backend._upload_param_err,
                   "param_ok": backend._upload_param_ok}
            rows.append(row)
            print(f"[{el:5.1f}s] _upload_state={state} progress={prog[:4]} "
                  f"raw={raw} paramErr={backend._upload_param_err} paramOk={backend._upload_param_ok}")

        try:
            page.screenshot(path=str(OUT / "probe_gate.png"), full_page=True)
        except Exception:
            pass

        hits = [u.split("?")[0] for u in all_resp if "helper_upload_params" in u]
        print(f"\n本页一共 {len(all_resp)} 个 mmfinderassistant 响应；"
              f"其中 helper_upload_params {len(hits)} 次")
        print(f"生产探针记录：param_err={backend._upload_param_err} param_ok={backend._upload_param_ok}")
        ok = True
    except Exception as exc:  # noqa: BLE001
        import traceback
        ok = False
        print(f"FATAL {type(exc).__name__}: {exc}")
        print(traceback.format_exc()[-1500:])
    finally:
        try:
            backend.close()
        except Exception:
            pass

    p = OUT / f"probe_gate_{'headless' if headless else 'headful'}.json"
    p.write_text(json.dumps({"ok": ok, "rows": rows}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    print(f"报告：{p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
