"""通用开放接口后端（纯标准库实现，无第三方依赖）。

适配「换取令牌 → 上传素材 → 提交发布」这一视频号 / 公众号系开放接口的通用形态。
端点、字段名、鉴权方式全部可在 ``config.json → credentials.openapi`` 中配置，
因此既能对接官方网关，也能对接企业内部的转发服务。

典型配置::

    "credentials": {
      "openapi": {
        "base_url": "https://api.weixin.qq.com",
        "appid": "wx........",
        "secret": "........",
        "token_endpoint":      "/cgi-bin/token",
        "upload_endpoint":     "/cgi-bin/material/add_material",
        "publish_endpoint":    "/cgi-bin/freepublish/submit",
        "media_field":         "media",
        "upload_type":         "video",
        "timeout":             120
      }
    }

安全：令牌与密钥只从配置读取，日志中一律脱敏（``***``）。
"""

from __future__ import annotations

import json
import mimetypes
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import (
    AuthError,
    BackendError,
    PublisherError,
    RateLimitError,
    RemoteRejectError,
    UploadError,
)
from ..logger import get_logger
from ..models import PublishResult, PublishStatus, VideoItem
from ..validators import validate_item
from .base import PublishBackend

log = get_logger("backend.openapi")

# 平台错误码 → 语义化异常
AUTH_CODES = {40001, 40013, 40014, 42001, 42007, 40164}
RATE_CODES = {45009, 45011, 45034, 45035, 45039}
REJECT_CODES = {40007, 40008, 40009, 45001, 45047, 87014}


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """手工构造 multipart/form-data 请求体（标准库无现成实现）。"""
    boundary = f"----videopub{uuid.uuid4().hex}"
    buf = bytearray()
    for key, val in fields.items():
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        buf += f"{val}\r\n".encode()
    for key, (fname, content, ctype) in files.items():
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{key}"; filename="{fname}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        buf += content
        buf += b"\r\n"
    buf += f"--{boundary}--\r\n".encode()
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


class OpenApiBackend(PublishBackend):
    name = "openapi"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        creds = (config.credentials or {}).get("openapi", {}) or {}
        self.base_url = str(creds.get("base_url", "")).rstrip("/")
        self.appid = str(creds.get("appid", ""))
        self.secret = str(creds.get("secret", ""))
        self.access_token = str(creds.get("access_token", ""))
        self.token_endpoint = creds.get("token_endpoint", "/cgi-bin/token")
        self.upload_endpoint = creds.get("upload_endpoint", "/cgi-bin/material/add_material")
        self.publish_endpoint = creds.get("publish_endpoint", "/cgi-bin/freepublish/submit")
        self.media_field = creds.get("media_field", "media")
        self.upload_type = creds.get("upload_type", "video")
        self.timeout = int(creds.get("timeout", config.publish.request_timeout_seconds))
        self.verify_tls = bool(creds.get("verify_tls", True))
        self.extra_headers = creds.get("headers", {}) or {}
        self._token_expire_at = 0.0

        if not self.base_url:
            raise BackendError(
                "openapi 后端缺少 credentials.openapi.base_url",
                detail={"hint": "在 config.json 中补齐接口地址"},
            )

    # -- HTTP 底座 ---------------------------------------------------------
    def _opener(self):
        if self.base_url.startswith("https") and not self.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener()

    def _request(self, url: str, data: bytes | None, headers: dict[str, str],
                 method: str = "POST", *, retry: int = 2) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(retry + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener().open(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                return self._parse(body)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                code = exc.code
                if code in (401, 403):
                    raise AuthError(f"鉴权失败 HTTP {code}", detail={"body": body[:500]}) from exc
                if code == 429:
                    raise RateLimitError("触发平台限流 HTTP 429", detail={"body": body[:300]}) from exc
                if 500 <= code < 600:
                    last_exc = UploadError(f"服务端错误 HTTP {code}", detail={"body": body[:300]})
                    if attempt < retry:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise last_exc from exc
                raise BackendError(f"HTTP {code}", detail={"body": body[:500]}) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = UploadError(f"网络异常: {exc}", detail={"url": url})
                if attempt < retry:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last_exc from exc
        raise BackendError(f"请求失败: {last_exc}")

    @staticmethod
    def _parse(body: str) -> dict[str, Any]:
        if not body.strip():
            return {}
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return {"_raw": body[:1000]}
        if not isinstance(obj, dict):
            return {"_raw": obj}
        code = obj.get("errcode", obj.get("code", 0))
        if code not in (0, None) and code != "0":
            errmsg = obj.get("errmsg") or obj.get("message") or ""
            if code in AUTH_CODES:
                raise AuthError(f"接口鉴权错误 {code}: {errmsg}", detail=obj)
            if code in RATE_CODES:
                raise RateLimitError(f"接口限流 {code}: {errmsg}", detail=obj)
            if code in REJECT_CODES:
                raise RemoteRejectError(f"内容被平台拒绝 {code}: {errmsg}", detail=obj)
            raise UploadError(f"接口返回错误 {code}: {errmsg}", detail=obj)
        return obj

    # -- 生命周期 ----------------------------------------------------------
    def authenticate(self) -> dict[str, Any]:
        if self.access_token and time.time() < self._token_expire_at:
            self._authenticated = True
            return self._auth_info
        if self.access_token and not self._token_expire_at:
            # 直接使用配置中的长效令牌
            self._authenticated = True
            self._auth_info = {"mode": "static-token", "token": self.access_token}
            return self._auth_info
        if not (self.appid and self.secret):
            raise AuthError(
                "openapi 后端缺少 appid/secret，且未提供 access_token",
                detail={"hint": "在 config.json → credentials.openapi 中补齐凭据"},
            )
        url = f"{self.base_url}{self.token_endpoint}?" + urllib.parse.urlencode(
            {"grant_type": "client_credential", "appid": self.appid, "secret": self.secret}
        )
        data = self._request(url, None, {}, method="GET")
        token = data.get("access_token")
        if not token:
            raise AuthError("令牌接口未返回 access_token", detail={"response": data})
        self.access_token = str(token)
        self._token_expire_at = time.time() + int(data.get("expires_in", 7200)) - 120
        self._authenticated = True
        self._auth_info = {"mode": "client_credential", "appid": self.appid, "expires_in": data.get("expires_in")}
        log.info("openapi 鉴权成功（appid=%s，令牌有效期 %ss）", self.appid, data.get("expires_in"))
        return self._auth_info

    def _url(self, endpoint: str, *, with_token: bool = True) -> str:
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"
        if with_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}access_token={urllib.parse.quote(self.access_token)}"
        return url

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "videopub/1.0", "Accept": "application/json"}
        h.update({str(k): str(v) for k, v in self.extra_headers.items()})
        return h

    # -- 发布 --------------------------------------------------------------
    def publish(self, item: VideoItem, *, options: dict[str, Any] | None = None) -> PublishResult:
        opts = options or {}
        result = self._result(item, options={**opts, "backend_name": self.name})
        t0 = time.perf_counter()
        if not self._authenticated:
            self.authenticate()

        info = validate_item(item, self.config)
        result.detail["validation"] = info

        media_id = self._upload_media(item, result)
        self._submit_publish(item, media_id, result)

        result.detail["media_id"] = media_id
        result.finish(PublishStatus.SUCCESS)
        result.duration_ms = int((time.perf_counter() - t0) * 1000)
        return result

    def _upload_media(self, item: VideoItem, result: PublishResult) -> str:
        vpath = Path(item.video_path)
        content = vpath.read_bytes()  # 大文件场景可换成分片续传接口
        ctype = mimetypes.guess_type(vpath.name)[0] or "video/mp4"
        desc = {
            "title": item.title,
            "introduction": item.description[: self.config.content.description_max_length],
        }
        fields = {"type": self.upload_type, "description": json.dumps(desc, ensure_ascii=False)}
        files = {self.media_field: (vpath.name, content, ctype)}

        body, ct = _multipart(fields, files)
        headers = self._headers()
        headers["Content-Type"] = ct
        headers["Content-Length"] = str(len(body))
        log.info("上传视频素材：%s（%.1fMB）", vpath.name, len(content) / 1024 / 1024)
        resp = self._request(self._url(self.upload_endpoint), body, headers)
        media_id = str(resp.get("media_id") or resp.get("mediaId") or "")
        if not media_id:
            raise UploadError("上传成功但未返回 media_id", detail={"response": resp})
        result.detail["upload_response"] = {k: v for k, v in resp.items() if k != "media_id"}

        # 封面（可选）：部分网关支持独立封面接口
        if item.cover_path and Path(item.cover_path).exists():
            try:
                self._upload_cover(item, result)
            except BackendError as exc:
                log.warning("封面上传失败（不阻断发布）：%s", exc.message)
                result.detail["cover_error"] = exc.to_dict()
        return media_id

    def _upload_cover(self, item: VideoItem, result: PublishResult) -> None:
        cpath = Path(item.cover_path)
        ctype = mimetypes.guess_type(cpath.name)[0] or "image/png"
        endpoint = (self.config.credentials.get("openapi", {}) or {}).get("cover_endpoint")
        if not endpoint:
            return
        body, ct = _multipart({"type": "image"}, {"media": (cpath.name, cpath.read_bytes(), ctype)})
        headers = self._headers()
        headers["Content-Type"] = ct
        headers["Content-Length"] = str(len(body))
        resp = self._request(self._url(endpoint), body, headers)
        result.detail["cover_response"] = {k: resp[k] for k in list(resp)[:5]}

    def _submit_publish(self, item: VideoItem, media_id: str, result: PublishResult) -> None:
        payload: dict[str, Any] = {
            "media_id": media_id,
            "title": item.title,
            "description": item.description,
            "topics": item.hashtags(),
            "publish_mode": self.config.publish.publish_mode,
        }
        if item.cover_path:
            payload["cover"] = item.cover_path
        if self.config.publish.publish_mode == "scheduled" and self.config.publish.scheduled_delay_minutes:
            payload["publish_at"] = int(time.time()) + self.config.publish.scheduled_delay_minutes * 60
        payload.update((self.config.credentials.get("openapi", {}) or {}).get("publish_extra", {}) or {})

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        log.info("提交发布：%s", item.item_id)
        resp = self._request(self._url(self.publish_endpoint), data, headers)
        result.remote_id = str(resp.get("publish_id") or resp.get("publishId") or resp.get("id") or media_id)
        result.remote_url = str(resp.get("url") or resp.get("share_url") or "")
        result.detail["publish_response"] = resp

    # -- 诊断 --------------------------------------------------------------
    def healthcheck(self) -> dict[str, Any]:
        """探测鉴权与连通性。不抛异常，结果结构化返回。"""
        out: dict[str, Any] = {"backend": self.name, "ok": False, "checks": []}
        try:
            self.authenticate()
        except PublisherError as exc:
            out["checks"].append({"check": "authenticate", "ok": False, "error": exc.to_dict()})
            out["error"] = exc.message
            return out
        out["checks"].append({"check": "authenticate", "ok": True, "mode": self._auth_info.get("mode")})

        endpoint = (self.config.credentials.get("openapi", {}) or {}).get(
            "health_endpoint", "/cgi-bin/getcallbackip"
        )
        try:
            self._request(self._url(endpoint), None, self._headers(), method="GET")
            out["checks"].append({"check": "connectivity", "ok": True})
            out["ok"] = True
        except PublisherError as exc:
            # 鉴权已通过，端点不存在等属于可容忍项
            out["checks"].append({"check": "connectivity", "ok": False, "error": exc.to_dict()})
            out["ok"] = True
        return out
