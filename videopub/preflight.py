#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""发布前预检：不用开浏览器，直接问平台「现在到底能不能传视频」。

为什么需要它
------------
2026-10 实测发现：视频号助手在**上传前**会先调
``POST /cgi-bin/mmfinderassistant-bin/helper/helper_upload_params`` 取上传参数。
若该接口返回非 0 的 ``errCode``（常见 ``300002``），客户端就**永远拿不到上传凭证**，
表现是页面上「视频进度条卡在 0% / 取消上传」、封面一直「生成中」，
即使点「发表」也会被平台以「请上传视频」挡回来。

关键结论（实测，别重复踩）：
  * 该失败**与自动化无关**：用保存下来的 cookie 走**纯 HTTP**重放同样返回 300002；
  * 也**与代理无关**：直连与走代理结果一致；
  * 同一 cookie 调 ``auth/auth_data``、``statistic/*`` 等只读接口**全部成功** ——
    说明登录态本身是好的，是**账号/服务端**层面拒了下发上传参数。

所以本模块用一次极轻量的 HTTP 调用把「能不能传」提前问清楚，
避免白跑一遍浏览器自动化（还要传几 MB 视频）之后才发现传不上去。

只调用只读/取参接口，不投递任何文件、不产生内容。
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .errors import AuthError, PublisherError

BASE = "https://channels.weixin.qq.com"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

API = {
    "auth_data": "/cgi-bin/mmfinderassistant-bin/auth/auth_data",
    "upload_params": "/cgi-bin/mmfinderassistant-bin/helper/helper_upload_params",
}

# 这些 errCode 见过并已定位含义
ERR_HINTS = {
    0: "正常",
    300002: ("平台拒绝下发上传参数（request failed）。这**不是**本机网络、代理或自动化指纹问题 —— "
             "同一登录态调只读接口均正常，属账号/服务端层面限制。"
             "请在浏览器里手动登录视频号助手，手动传一个视频验证；"
             "若手动也传不上，请向视频号助手客服反馈账号与时间点。"),
    300001: "请求被平台拒绝（request failed）。通常为参数/权限不匹配，可先重新扫码登录再试。",
    300036: "该接口对当前账号未开放。",
    300800: "该接口对当前账号未开放（多为功能未开通）。",
}


class PreflightError(PublisherError):
    code = "EPREFLIGHT"


def load_cookies(storage_state: str | Path) -> dict[str, str]:
    """从 playwright ``storage_state`` 文件里取出 cookie 键值对。"""
    p = Path(storage_state)
    if not p.exists():
        raise AuthError(f"未找到登录态文件：{p}", detail={"hint": "先执行 `python main.py login`"})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"登录态文件无法解析：{p}（{exc}）") from exc
    return {str(c.get("name")): str(c.get("value", "")) for c in data.get("cookies", [])}


def load_local_storage(storage_state: str | Path) -> dict[str, str]:
    p = Path(storage_state)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for o in data.get("origins", []) or []:
        for kv in o.get("localStorage", []) or []:
            out[str(kv.get("name"))] = str(kv.get("value", ""))
    return out


def _post(opener, path: str, *, cookies: dict[str, str], body: dict[str, Any],
          page_url: str, timeout: float, uin: str = "0000000000") -> dict[str, Any]:
    url = f"{BASE}/{path.lstrip('/')}"
    q = (f"?_aid={uuid.uuid4()}&_rid={uuid.uuid4()}&"
         f"_pageUrl={urllib.parse.quote(page_url, safe='')}")
    req = urllib.request.Request(
        url + q,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN",
            "Referer": page_url,
            "Origin": BASE,
            "x-wechat-uin": uin,
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
            "User-Agent": DEFAULT_UA,
        },
        method="POST",
    )
    out: dict[str, Any] = {"path": path, "http_status": None, "errCode": None, "errMsg": "",
                           "raw": "", "error": ""}
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            out["http_status"] = r.status
    except urllib.error.HTTPError as e:  # pragma: no cover - 取决于服务端
        out["http_status"] = e.code
        raw = e.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    # ⚠️ 必须先解析**完整**响应再截断留档：
    # 早期版本先 `raw[:1500]` 再 `json.loads`，而 auth_data 的响应远超 1500 字符，
    # 结果 JSON 被截断 → errCode 恒为 None → 预检误报「登录态不可用」。
    try:
        j = json.loads(raw)
        out["errCode"] = j.get("errCode")
        out["errMsg"] = j.get("errMsg", "")
        out["data"] = j.get("data")
    except Exception:
        pass
    out["raw"] = raw[:1500]
    return out


def check_upload_ready(config: Config, *, timeout: float = 20.0,
                       direct: bool = False, page_url: str | None = None,
                       opener=None) -> dict[str, Any]:
    """返回一份预检报告。

    ``direct=True`` 时绕过所有代理（含环境变量里的 ``http_proxy``）直连。
    ``opener`` 仅供测试注入（任何带 ``open(req, timeout=...)`` 的对象）。
    """
    creds = (config.credentials or {}).get("channels_web", {}) or {}
    storage = Path(creds.get("storage_state") or (config.resolve("state") / "channels_storage.json"))

    if opener is None:
        if direct:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()

    report: dict[str, Any] = {
        "ok": False,
        "storage_state": str(storage),
        "storage_exists": storage.exists(),
        "cookie_names": [],
        "checks": [],
        "account": {},
        "upload_err_code": None,
        "ready": False,
        "hint": "",
    }

    try:
        cookies = load_cookies(storage)
    except AuthError as exc:
        report["hint"] = exc.message
        report["checks"].append({"check": "storage_state", "ok": False, "error": exc.message})
        return report
    report["cookie_names"] = sorted(cookies)
    report["checks"].append({"check": "storage_state", "ok": True, "cookies": sorted(cookies)})

    ls = load_local_storage(storage)
    finder_id = ls.get("finder_username", "")
    report["finder_username"] = finder_id
    uin = cookies.get("wxuin", "") or "0000000000"

    pu = page_url or f"{BASE}/platform"
    body = {
        "timestamp": str(int(time.time() * 1000)),
        "_log_finder_uin": "",
        "_log_finder_id": finder_id,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }

    # 1) 登录态是否真的有效
    a = _post(opener, API["auth_data"], cookies=cookies, body=body, page_url=pu,
              timeout=timeout, uin=uin)
    login_ok = a.get("errCode") == 0
    acct = (a.get("data") or {}).get("userAttr") or {}
    fu = (a.get("data") or {}).get("finderUser") or {}
    report["account"] = {
        "nickname": acct.get("nickname") or fu.get("nickname"),
        "finder_nickname": fu.get("nickname"),
        "feeds_count": fu.get("feedsCount"),
        "fans_count": fu.get("fansCount"),
        "auth_company_name": fu.get("authCompanyName"),
    }
    report["checks"].append({
        "check": "login", "ok": login_ok,
        "errCode": a.get("errCode"), "errMsg": a.get("errMsg"),
        "error": a.get("error", ""),
    })
    if not login_ok:
        report["hint"] = (f"登录态不可用（errCode={a.get('errCode')}）。"
                          "请重新执行 `python main.py login` 扫码登录。")
        return report

    # 2) 平台是否愿意下发上传参数（**决定性检查**）
    u = _post(opener, API["upload_params"], cookies=cookies, body=body,
              page_url=page_url or f"{BASE}/platform/post/create",
              timeout=timeout, uin=uin)
    code = u.get("errCode")
    report["upload_err_code"] = code
    report["upload_err_msg"] = u.get("errMsg")
    report["checks"].append({
        "check": "upload_params", "ok": code == 0,
        "errCode": code, "errMsg": u.get("errMsg"), "error": u.get("error", ""),
        "http_status": u.get("http_status"),
    })
    report["ready"] = code == 0
    report["ok"] = code == 0

    if code == 0:
        report["hint"] = "平台可正常下发上传参数，可以发布。"
    else:
        report["hint"] = ERR_HINTS.get(
            code, f"平台未下发上传参数（errCode={code}），发布一定会卡在 0%。")
    return report


def format_report(rep: dict[str, Any]) -> str:
    """把预检报告渲染成终端可读文本。"""
    verdict = "✅ 可以发布" if rep.get("ready") else "❌ 暂不可发布"
    lines = [f"{verdict}", f"  登录态文件：{rep.get('storage_state')}"]
    ck = rep.get("checks") or []
    for c in ck:
        mark = "ok " if c.get("ok") else "FAIL"
        extra = ""
        if c.get("check") == "login" and c.get("ok"):
            extra = f" errCode={c.get('errCode')}"
        elif not c.get("ok"):
            extra = f" errCode={c.get('errCode')} {c.get('errMsg') or c.get('error') or ''}"
        lines.append(f"  [{mark}] {c.get('check')}{extra}")
    acct = rep.get("account") or {}
    if acct.get("nickname") or acct.get("finder_nickname"):
        lines.append(f"  账号：{acct.get('nickname') or ''} / 视频号：{acct.get('finder_nickname') or ''}"
                     f"（作品 {acct.get('feeds_count')} 粉丝 {acct.get('fans_count')}）")
    if rep.get("hint"):
        lines.append(f"  结论：{rep['hint']}")
    return "\n".join(lines)
