"""发布前校验：把「可预知的失败」挡在联网之前。

校验项覆盖：文件存在性 / 大小 / 扩展名 / 标题与描述长度 / 话题数量 / 封面尺寸。
失败抛 :class:`ValidationError`，携带 ``errors`` 列表，便于一次性告知全部问题。
"""

from __future__ import annotations

import struct
from pathlib import Path

from .config import Config
from .errors import ValidationError
from .models import VideoItem

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or not head.startswith(_PNG_SIG):
        return None
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def _jpeg_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def image_size(path: str | Path) -> tuple[int, int] | None:
    p = Path(path)
    if not p.exists():
        return None
    return _png_size(p) or _jpeg_size(p)


def validate_item(item: VideoItem, cfg: Config) -> dict:
    """校验单条内容。返回信息字典；有问题则抛 ValidationError。"""
    errors: list[str] = []
    warnings: list[str] = []
    lim = cfg.limits
    con = cfg.content

    # --- 视频 -------------------------------------------------------------
    vp = Path(item.video_path) if item.video_path else None
    if not vp or not vp.exists():
        errors.append("视频文件缺失")
    else:
        ext = vp.suffix.lower()
        if ext not in lim.allowed_video_ext:
            errors.append(f"视频格式不支持: {ext}（允许 {lim.allowed_video_ext}）")
        size_mb = vp.stat().st_size / 1024 / 1024
        if size_mb > lim.max_video_size_mb:
            errors.append(f"视频过大: {size_mb:.1f}MB > {lim.max_video_size_mb}MB")
        if vp.stat().st_size / 1024 < lim.min_video_size_kb:
            errors.append(f"视频过小: < {lim.min_video_size_kb}KB，疑似损坏")

    # --- 封面 -------------------------------------------------------------
    cp = Path(item.cover_path) if item.cover_path else None
    cover_info: dict = {}
    if not cp or not cp.exists():
        warnings.append("封面缺失，将使用平台默认取帧")
    else:
        ext = cp.suffix.lower()
        if ext not in lim.allowed_cover_ext:
            errors.append(f"封面格式不支持: {ext}")
        size_mb = cp.stat().st_size / 1024 / 1024
        if size_mb > lim.max_cover_size_mb:
            errors.append(f"封面过大: {size_mb:.1f}MB > {lim.max_cover_size_mb}MB")
        wh = image_size(cp)
        if wh:
            w, h = wh
            if w <= 0 or h <= 0:
                # 头部畸形（宽或高为 0）——不阻断，仅提示，避免除零
                warnings.append(f"封面尺寸异常: {w}x{h}")
            else:
                ratio = round(w / h, 3)
                cover_info = {"width": w, "height": h, "ratio": ratio}
                if ratio > 1.2:
                    cover_info["orientation"] = "landscape"
                elif ratio < 0.85:
                    cover_info["orientation"] = "portrait"
                else:
                    cover_info["orientation"] = "square"
                if w < 480 or h < 480:
                    warnings.append(f"封面分辨率偏低: {w}x{h}")

    # --- 文案 -------------------------------------------------------------
    if not item.title.strip():
        errors.append("标题为空")
    if len(item.title) > con.title_max_length:
        warnings.append(f"标题较长({len(item.title)}字)，视频号可能截断")
    if len(item.description) > con.description_max_length:
        errors.append(f"描述超长: {len(item.description)} > {con.description_max_length}")
    if len(item.topics) > lim.max_topics:
        errors.append(f"话题过多: {len(item.topics)} > {lim.max_topics}")

    # --- 汇总 -------------------------------------------------------------
    info = {
        "item_id": item.item_id,
        "video_size_mb": round(item.video_size / 1024 / 1024, 2) if item.video_size else 0,
        "cover": cover_info,
        "title_length": len(item.title),
        "description_length": len(item.description),
        "topics": len(item.topics),
        "warnings": warnings,
    }
    if errors:
        raise ValidationError(
            f"条目 {item.item_id} 校验不通过：{'; '.join(errors)}",
            detail={"item_id": item.item_id, "errors": errors, "warnings": warnings, "info": info},
        )
    return info
