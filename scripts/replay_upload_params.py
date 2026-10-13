#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用**纯 HTTP**重放 ``helper_upload_params``，判定 300002 是谁的锅。

三种可能：
  A. 不带浏览器重放也返回 300002  → 账号/服务端层面拒绝，浏览器怎么调都没用
  B. 重放成功（errCode 0）        → 是浏览器侧请求构造/指纹问题，可继续修
  C. 全部网络失败                 → 先把网络打通再说

同时做变体对照（换 x-wechat-uin、补 appType 等），定位服务端到底依赖哪个字段。

只调用「取上传参数」接口，不投递任何文件、不产生内容。
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

URL = "https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/helper/helper_upload_params"
FINDER_ID = "v2_060000227c00b4e03daaa37a34a752d074338ffea675@finder"


def load_cookies() -> dict[str, str]:
    d = json.loads((ROOT / "state" / "channels_storage.json").read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in d.get("cookies", [])}


def call(name: str, *, uin: str, body: dict, page_url: str, extra_headers=None,
         timeout=25) -> dict:
    cks = load_cookies()
    q = (f"?_aid={uuid.uuid4()}&_rid={uuid.uuid4()}&"
         f"_pageUrl={urllib.parse.quote(page_url, safe='')}")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN",
        "Referer": page_url,
        "Origin": "https://channels.weixin.qq.com",
        "x-wechat-uin": uin,
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        "Cookie": "; ".join(f"{k}={v}" for k, v in cks.items()),
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode()
    req = urllib.request.Request(URL + q, data=data, headers=headers, method="POST")
    ctx = ssl.create_default_context()
    out = {"name": name, "uin": uin, "body": body, "page_url": page_url,
           "extra": extra_headers}
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            out["status"] = r.status
            out["resp"] = raw
    except urllib.error.HTTPError as e:
        out["status"] = e.code
        out["resp"] = e.read().decode("utf-8", "replace")[:800]
    except Exception as exc:  # noqa: BLE001
        out["status"] = None
        out["resp"] = f"<{type(exc).__name__}: {exc}>"
    out["errCode"] = None
    try:
        out["errCode"] = json.loads(out["resp"]).get("errCode")
    except Exception:
        pass
    return out


def base_body(extra=None) -> dict:
    b = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": FINDER_ID,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    if extra:
        b.update(extra)
    return b


def main() -> int:
    cks = load_cookies()
    real_uin = cks.get("wxuin", "")
    print("cookie 名单：", list(cks))
    print("wxuin =", real_uin)
    print()

    variants = [
        ("V1-exact-0000", dict(uin="0000000000", body=base_body(),
                               page_url="https://channels.weixin.qq.com/platform/post/create")),
        ("V2-real-uin", dict(uin=real_uin, body=base_body(),
                             page_url="https://channels.weixin.qq.com/platform/post/create")),
        ("V3-plus-appType", dict(uin=real_uin, body=base_body({"appType": 1}),
                                 page_url="https://channels.weixin.qq.com/platform/post/create")),
        ("V4-platform-page", dict(uin=real_uin, body=base_body(),
                                  page_url="https://channels.weixin.qq.com/platform")),
        ("V5-extra-hdrs", dict(uin=real_uin, body=base_body(),
                               page_url="https://channels.weixin.qq.com/platform/post/create",
                               extra_headers={"X-Requested-With": "XMLHttpRequest",
                                              "X-WECHAT-KEY": "",
                                              "X-WECHAT-UIN": real_uin})),
    ]

    results = []
    for name, kw in variants:
        r = call(name, **kw)
        results.append(r)
        print(f"[{r['status']}] {name}  errCode={r['errCode']}")
        print(f"      resp: {str(r['resp'])[:220]}")
        print()

    # 对照：一个已知成功的接口
    ok_url = ("https://channels.weixin.qq.com/cgi-bin/mmfinderassistant-bin/auth/auth_data")
    cks_s = "; ".join(f"{k}={v}" for k, v in cks.items())
    q = (f"?_aid={uuid.uuid4()}&_rid={uuid.uuid4()}&"
         f"_pageUrl={urllib.parse.quote('https://channels.weixin.qq.com/platform', safe='')}")
    body = base_body({"_log_finder_id": FINDER_ID})
    req = urllib.request.Request(
        ok_url + q, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/plain, */*",
                 "Referer": "https://channels.weixin.qq.com/platform",
                 "x-wechat-uin": real_uin, "Cookie": cks_s,
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0"},
        method="POST")
    print("=== 对照：auth_data（浏览器里是成功的） ===")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            txt = r.read().decode("utf-8", "replace")
            print(f"[{r.status}] {txt[:300]}")
    except Exception as exc:  # noqa: BLE001
        print(f"<{type(exc).__name__}: {exc}>")

    out = ROOT / "state" / "diag" / "replay_upload_params.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
