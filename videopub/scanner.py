"""内容源扫描：把目录结构翻译成 :class:`VideoItem` 列表。

约定目录结构（用户在任务中指定的源目录即此形态）::

    <source_dir>/
      01-风是从哪来的/
        final.mp4        # 视频
        发布.md           # 标题 / 描述 / 话题标签
        封面-3x4.png      # 竖版封面
        封面-4x3.png      # 横版封面（视频号首选）
      02-海水为什么是咸的/
        ...

扫描是**只读**的：只读取文件元信息与文案，绝不修改源目录。
"""

from __future__ import annotations

import glob
import re
from pathlib import Path
from typing import Iterable

from .config import Config
from .content import parse_file, title_from_dirname
from .errors import ContentParseError, ScanError
from .logger import get_logger
from .models import VideoItem

log = get_logger("scanner")
_ORDER_RE = re.compile(r"^\s*(\d+)")


def _match_first(directory: Path, patterns: Iterable[str]) -> str:
    """按给定模式顺序在目录内查找第一个匹配文件（不递归）。"""
    for pat in patterns:
        hits = sorted(glob.glob(str(directory / pat)))
        if hits:
            return hits[0]
    return ""


def _cover_variants(directory: Path, cfg: Config) -> tuple[str, str]:
    """返回 (竖版3x4, 横版4x3) 封面路径。"""
    vertical = _match_first(directory, ["封面-3x4.*", "*3x4*.png", "*vertical*.*", "封面-*.png"])
    horizontal = _match_first(directory, ["封面-4x3.*", "*4x3*.png", "*horizontal*.*", "封面-*.png"])
    return vertical, horizontal


def _pick_cover(vertical: str, horizontal: str, cfg: Config) -> str:
    pref = (cfg.content.cover_preference or "4x3").lower()
    if pref in ("3x4", "vertical", "竖版") and vertical:
        return vertical
    if horizontal:
        return horizontal
    return vertical or horizontal


def build_item(directory: Path, cfg: Config) -> VideoItem:
    """把单个子目录构建成 VideoItem（含文案解析与封面选择）。"""
    name = directory.name
    fallback = title_from_dirname(name)

    video = _match_first(directory, cfg.content.video_patterns)
    vertical, horizontal = _cover_variants(directory, cfg)

    copy_path = directory / cfg.content.copy_file
    warnings: list[str] = []
    title, description, topics, sections = fallback, "", [], {}
    try:
        parsed = parse_file(copy_path, fallback_title=fallback) if copy_path.exists() else None
        if parsed is None:
            warnings.append(f"缺少发布文案 {cfg.content.copy_file}，仅用目录名作标题")
        else:
            title = parsed.title or fallback
            description = parsed.description
            topics = list(parsed.topics)
            sections = parsed.sections
            warnings.extend(parsed.warnings)
    except ContentParseError as exc:
        warnings.append(f"文案解析失败：{exc.message}")

    if not topics and cfg.content.default_topics:
        topics = list(cfg.content.default_topics)
    if cfg.content.append_topics:
        missing = [t for t in cfg.content.default_topics if t not in topics]
        topics.extend(missing)

    if not video:
        warnings.append(f"未找到视频文件（已尝试 {cfg.content.video_patterns}）")
    if not (vertical or horizontal):
        warnings.append("未找到封面图")

    item = VideoItem(
        item_id=name,
        directory=str(directory),
        title=title,
        description=description,
        video_path=video,
        cover_path=_pick_cover(vertical, horizontal, cfg),
        cover_3x4=vertical,
        cover_4x3=horizontal,
        topics=topics,
        tags={
            "cover_preference": cfg.content.cover_preference,
            "source_name": name,
        },
        meta={
            "copy_file": str(copy_path) if copy_path.exists() else "",
            "warnings": warnings,
            "sections": sections,
        },
    )
    return item


def _sort_key(directory: Path) -> tuple[int, str]:
    m = _ORDER_RE.match(directory.name)
    return (int(m.group(1)) if m else 10_000, directory.name)


def scan_source(cfg: Config, *, source_dir: str | None = None) -> list[VideoItem]:
    """扫描源目录，返回按序号排序的待发布条目。"""
    root = Path(source_dir or cfg.source_dir)
    if not root.exists():
        raise ScanError(f"源目录不存在: {root}", detail={"path": str(root)})
    if not root.is_dir():
        raise ScanError(f"源目录不是目录: {root}", detail={"path": str(root)})

    subdirs = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]
    subdirs.sort(key=_sort_key)
    if not subdirs:
        raise ScanError(f"源目录下没有子目录: {root}", detail={"path": str(root)})

    items: list[VideoItem] = []
    for d in subdirs:
        item = build_item(d, cfg)
        for w in item.meta.get("warnings", []):
            log.warning("条目 %s 告警：%s", item.item_id, w)
        items.append(item)

    log.info("扫描完成：目录 %s，共 %d 个条目", root, len(items))
    return items


def pending_items(cfg: Config, store, *, source_dir: str | None = None) -> list[VideoItem]:
    """返回尚未成功发布的条目（待发布队列）。"""
    items = scan_source(cfg, source_dir=source_dir)
    if not cfg.publish.skip_published:
        return items
    out = []
    for it in items:
        if store.is_published(it.item_id):
            log.info("跳过已发布条目：%s", it.item_id)
            continue
        out.append(it)
    return out
