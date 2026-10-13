"""定时时间解析。

把配置里的几种「想什么时候发」的写法，统一算成一个明确的本地 ``datetime``：

    * ``scheduled_at``         —— 绝对时间 ``YYYY-MM-DD HH:MM``（最高优先级）
    * ``scheduled_daily_time`` —— 每日时刻 ``HH:MM``，取「下一个该时刻」
      （今天还没到就今天，已经过了就明天）→ 天然支持「每天早上 8 点」
    * ``scheduled_delay_minutes`` —— 现在 + N 分钟

另外提供 ``min_lead_minutes`` 兜底：平台侧定时发表通常要求留出一定提前量，
时间窗太近会被静默抹掉或直接当成立即发布 —— 那是**很危险**的失败模式
（本该明天发的内容当场就发出去了），所以这里强制抬高到安全水位。

本模块只依赖标准库，便于单测。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .errors import ConfigError

_DT_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")
_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

DISPLAY_FMT = "%Y-%m-%d %H:%M"


def parse_datetime(value: str) -> datetime:
    """解析 ``YYYY-MM-DD HH:MM``（也接受 ISO 的 ``T`` 分隔与秒）。"""
    m = _DT_RE.match(str(value).strip())
    if not m:
        raise ConfigError(
            f"时间格式应为 YYYY-MM-DD HH:MM，当前为 {value!r}",
            detail={"field": "scheduled_at", "got": value},
        )
    y, mo, d, h, mi, s = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError as exc:
        raise ConfigError(f"时间非法: {value!r}（{exc}）",
                          detail={"field": "scheduled_at", "got": value}) from exc


def parse_hm(value: str) -> tuple[int, int]:
    """解析 ``HH:MM`` 为 ``(hour, minute)``。"""
    m = _HM_RE.match(str(value).strip())
    if not m:
        raise ConfigError(
            f"时刻格式应为 HH:MM，当前为 {value!r}",
            detail={"field": "scheduled_daily_time", "got": value},
        )
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ConfigError(f"时刻越界: {value!r}", detail={"field": "scheduled_daily_time"})
    return h, mi


def next_occurrence(hour: int, minute: int, *, now: datetime) -> datetime:
    """下一个 ``hour:minute``：今天未到取今天，已到（或正好）取明天。"""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def resolve_scheduled_at(
    *,
    publish_mode: str = "draft",
    absolute: str = "",
    daily_time: str = "",
    delay_minutes: int = 0,
    min_lead_minutes: int = 30,
    now: datetime | None = None,
) -> datetime | None:
    """算出平台侧定时时间。

    返回 ``None`` 表示「不定时 / 立即发布」。
    """
    if publish_mode != "scheduled":
        return None
    now = now or datetime.now()

    if absolute and str(absolute).strip():
        target = parse_datetime(absolute)
    elif daily_time and str(daily_time).strip():
        h, m = parse_hm(daily_time)
        target = next_occurrence(h, m, now=now)
    elif delay_minutes > 0:
        target = now + timedelta(minutes=int(delay_minutes))
    else:
        return None

    # 兜底：不能离现在太近（太近平台会当成立即发布）
    floor = now + timedelta(minutes=max(0, int(min_lead_minutes)))
    if target < floor:
        target = floor
    return target.replace(second=0, microsecond=0)


def format_for_picker(dt: datetime) -> tuple[str, str, str, str]:
    """把 datetime 拆成时间选择控件需要的四段：年、月、日、``HH:MM``。"""
    return f"{dt.year}", f"{dt.month}", f"{dt.day}", dt.strftime("%H:%M")
