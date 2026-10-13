#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把视频号助手「账号状态」相关接口的**完整响应**拉下来，找上传被拒的原因。

背景：``helper_upload_params`` 恒定返回 ``errCode 300002``，而同一 cookie
调 ``auth_data`` 正常 → 排除自动化指纹，问题在**账号/服务端**。
本脚本把能反映账号权限/状态的接口全部打全，便于判断是「未实名」「未开通」
「被风控」还是别的。

只读接口，不产生任何内容。
"""

from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://channels.weixin.qq.com"

ENDPOINTS = [
    "/cgi-bin/mmfinderassistant-bin/auth/get_auth_info",
    "/cgi-bin/mmfinderassistant-bin/auth/auth_data",
    "/cgi-bin/mmfinderassistant-bin/auth/mp_finder_window_init",
    "/cgi-bin/mmfinderassistant-bin/active-auth/is-in-finder-whitelist",
    "/cgi-bin/mmfinderassistant-bin/auth/list_talent_relation_by_bind_uin",
    "/cgi-bin/mmfinderassistant-bin/helper/helper_upload_params",
    "/cgi-bin/mmfinderassistant-bin/post/post_list",
    "/cgi-bin/mmfinderassistant-bin/statistic/new_post_total_data",
    "/cgi-bin/mmfinderassistant-bin/vip/get-user-member-service-status",
]


def load_cookies() -> dict[str, str]:
    d = json.loads((ROOT / "state" / "channels_storage.json").read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in d.get("cookies", [])}


def post(path: str, *, page_url: str, body_extra=None, family="cgi-bin") -> dict:
    cks = load_cookies()
    cookie = "; ".join(f"{k}={v}" for k, v in cks.items())
    u = f"{BASE}/{path.lstrip('/')}"
    q = (f"?_aid={uuid.uuid4()}&_rid={uuid.uuid4()}&"
         f"_pageUrl={urllib.parse.quote(page_url, safe='')}")
    body = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": "v2_060000227c00b4e03daaa37a34a752d074338ffea675@finder",
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    if body_extra:
        body.update(body_extra)
    req = urllib.request.Request(
        u + q, data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Referer": page_url,
            "Origin": "https://channels.weixin.qq.com",
            "x-wechat-uin": cks.get("wxuin", ""),
            "Cookie": cookie,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/140.0.0.0 Safari/537.36"),
        }, method="POST")
    out = {"path": path}
    try:
        with urllib.request.urlopen(req, timeout=25,
                                    context=ssl.create_default_context()) as r:
            out["status"] = r.status
            out["raw"] = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["raw"] = e.read().decode("utf-8", "replace")[:1200]
    except Exception as exc:  # noqa: BLE001
        out["status"] = None
        out["raw"] = f"<{type(exc).__name__}: {exc}>"
    return out


def main() -> int:
    results = []
    for p in ENDPOINTS:
        r = post(p, page_url="https://channels.weixin.qq.com/platform")
        results.append(r)
        raw = r["raw"]
        try:
            j = json.loads(raw)
            err = j.get("errCode")
            print(f"\n=== [{r['status']}] errCode={err}  {p}")
            print(json.dumps(j, ensure_ascii=False, indent=1)[:2200])
        except Exception:
            print(f"\n=== [{r['status']}] (非 JSON) {p}\n{raw[:500]}")

    out = ROOT / "state" / "diag" / "account_state.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
