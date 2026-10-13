"""发布文案解析。

源目录中每条内容带一份 ``发布.md``，内含面向不同平台的文案。本模块只负责
抽取 **视频号** 段落（标题 / 简介 / 话题标签），并保留其它平台的文案到 ``meta``
以便后续复用。

解析策略（逐级降级，永不因解析失败而中断整批发布）：
  1. 定位 ``## 视频号`` 小节（兼容 ``视频号（横版 4:3 封面）`` 等写法）；
  2. 该节内第一个围栏代码块的首行 → 标题；``简介：`` 之后的围栏代码块 → 描述；
  3. 描述中的 ``#xxx`` → 话题标签；
  4. 任一步失败 → 用目录名（去掉 ``NN-`` 前缀）兜底标题，描述留空，并抛
     :class:`ContentParseError` 供调用方记录告警（不会中断）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ContentParseError

FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```[^\n]*$", re.S | re.M)
FENCE_SPAN_RE = re.compile(r"^```[^\n]*\n.*?^```[^\n]*$", re.S | re.M)
HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$", re.M)
TOPIC_RE = re.compile(r"#([0-9A-Za-z_\u4e00-\u9fff]{1,30})")
TITLE_CLEAN_RE = re.compile(r"^[《<\"'\s]+|[》>\"'\s]+$")
SECTION_ALIASES = ("视频号", "wechat channels", "channels")


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """返回所有围栏代码块的 (start, end) 区间，用于把代码块内的 '#' 排除在标题识别之外。"""
    return [(m.start(), m.end()) for m in FENCE_SPAN_RE.finditer(text)]


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


@dataclass
class ParsedCopy:
    title: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _iter_sections(text: str) -> list[tuple[str, str]]:
    """把 md 按二级及以下标题切分成 (heading, body) 列表。

    关键点：**忽略代码块内部的 ``#话题``**，否则 ``#白版 #少儿科普`` 会被误判为
    标题，导致「视频号」段落被提前截断（这是实际踩到过的坑）。
    """
    spans = _fence_spans(text)
    marks = [m for m in HEADING_RE.finditer(text) if not _inside(m.start(), spans)]
    out: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(2).strip(), text[m.end():end]))
    return out


def _fenced_blocks(body: str) -> list[str]:
    return [b.strip("\n") for b in FENCE_RE.findall(body)]


def _clean_title(raw: str) -> str:
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    line = TITLE_CLEAN_RE.sub("", line).strip()
    return line


def extract_topics(text: str) -> list[str]:
    """按出现顺序去重提取话题标签。"""
    seen: list[str] = []
    for m in TOPIC_RE.finditer(text or ""):
        tag = m.group(1)
        if tag not in seen:
            seen.append(tag)
    return seen


def parse_copy(md_text: str, *, fallback_title: str = "") -> ParsedCopy:
    """解析发布文案 md。"""
    result = ParsedCopy()
    if not md_text or not md_text.strip():
        result.title = fallback_title
        result.warnings.append("发布文案为空，使用目录名兜底")
        return result

    sections = _iter_sections(md_text)
    for heading, body in sections:
        result.sections[heading] = body.strip()

    chan_body = ""
    for heading, body in sections:
        low = heading.lower()
        if any(alias in low for alias in SECTION_ALIASES):
            chan_body = body
            break
    if not chan_body:
        result.warnings.append("未找到「视频号」小节，回退到全文解析")
        chan_body = md_text

    blocks = _fenced_blocks(chan_body)
    # 1) 标题：优先取「简介」之前的第一个代码块
    desc_idx = chan_body.find("简介")
    title_block = ""
    for block in blocks:
        if chan_body.find(block) < desc_idx or desc_idx < 0:
            title_block = block
            break
    if not title_block and blocks:
        title_block = blocks[0]
    if title_block:
        result.title = _clean_title(title_block)

    # 2) 描述：取「简介」之后的第一个代码块
    if desc_idx >= 0:
        for block in blocks:
            if chan_body.find(block) > desc_idx:
                result.description = block.strip()
                break
    if not result.description and len(blocks) > 1:
        result.description = blocks[-1].strip()

    # 3) 话题标签
    result.topics = extract_topics(chan_body)

    if not result.title:
        result.warnings.append("未能解析出标题，使用目录名兜底")
        result.title = fallback_title
    return result


def parse_file(path: str | Path, *, fallback_title: str = "") -> ParsedCopy:
    p = Path(path)
    if not p.exists():
        raise ContentParseError(f"发布文案文件不存在: {p}", detail={"path": str(p)})
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ContentParseError(f"发布文案读取失败: {p}", detail={"path": str(p), "error": str(exc)}) from exc
    return parse_copy(text, fallback_title=fallback_title)


def title_from_dirname(name: str) -> str:
    return re.sub(r"^\s*\d+\s*[-_.、]\s*", "", name).strip() or name
