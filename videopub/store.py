"""发布状态存储。

两份数据：
  * ``state/publish_state.json``  —— 幂等索引（当前每个条目的最新状态），原子写。
  * ``state/history.jsonl``        —— 追加型历史，每次发布**尝试**一行，用于结果查询与报表。

历史文件只追加、不覆盖，保证审计可追溯；状态文件用于快速判断「是否已发布」。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .errors import StateError
from .logger import get_logger
from .models import PublishResult, PublishStatus

log = get_logger("store")
STATE_VERSION = 1


class StateStore:
    def __init__(self, state_file: str | Path) -> None:
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.history_file = self.state_file.with_name("history.jsonl")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._load()

    # -- 基础 IO -----------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"version": STATE_VERSION, "updated_at": "", "items": {}}
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as exc:
            bak = self.state_file.with_suffix(f".corrupt-{int(time.time())}.json")
            log.error("状态文件损坏，已备份到 %s：%s", bak, exc)
            try:
                self.state_file.replace(bak)
            except OSError:
                pass
            return {"version": STATE_VERSION, "updated_at": "", "items": {}}
        if not isinstance(raw, dict) or "items" not in raw:
            return {"version": STATE_VERSION, "updated_at": "", "items": {}}
        return raw

    def _flush(self) -> None:
        self._data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.state_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_file)  # 原子替换，避免半截文件
        except OSError as exc:
            raise StateError(f"状态文件写入失败: {self.state_file}", detail={"error": str(exc)}) from exc

    # -- 查询 --------------------------------------------------------------
    @property
    def items(self) -> dict[str, dict[str, Any]]:
        return self._data.get("items", {})

    def get(self, item_id: str) -> dict[str, Any] | None:
        return self.items.get(item_id)

    def is_published(self, item_id: str, *, include_dry_run: bool = False) -> bool:
        """是否已成功发布（演练结果默认不算「已发布」）。"""
        rec = self.items.get(item_id)
        if not rec:
            return False
        status = rec.get("status")
        if status == PublishStatus.SUCCESS.value:
            return True
        if include_dry_run and status == PublishStatus.DRY_RUN.value:
            return True
        return False

    def published_ids(self) -> set[str]:
        return {k for k, v in self.items.items() if v.get("status") == PublishStatus.SUCCESS.value}

    def status_of(self, item_id: str) -> str:
        rec = self.items.get(item_id)
        return rec.get("status", "pending") if rec else "pending"

    # -- 写入 --------------------------------------------------------------
    def record(self, result: PublishResult) -> None:
        """记录一次发布尝试：更新索引 + 追加历史。"""
        with self._lock:
            rec = self.items.get(result.item_id, {})
            rec.update(
                {
                    "item_id": result.item_id,
                    "title": result.title,
                    "status": result.status.value,
                    "status_label": result.status.label,
                    "backend": result.backend,
                    "remote_id": result.remote_id,
                    "remote_url": result.remote_url,
                    "error_code": result.error_code,
                    "error_message": result.error_message,
                    "attempts": (rec.get("attempts", 0) or 0) + max(result.attempts, 1),
                    "last_run_at": result.finished_at or result.started_at,
                    "duration_ms": result.duration_ms,
                    "trigger": result.trigger,
                    "batch_id": result.batch_id,
                }
            )
            if result.status == PublishStatus.SUCCESS:
                rec["published_at"] = result.finished_at or result.started_at
            self.items[result.item_id] = rec
            self._flush()
            self._append_history(result)

    def _append_history(self, result: PublishResult) -> None:
        line = json.dumps(result.to_dict(), ensure_ascii=False, default=str)
        try:
            with self.history_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            log.warning("历史记录写入失败: %s", exc)

    def reset(self, item_id: str) -> bool:
        """清除某条目的发布状态（用于重新发布）。"""
        with self._lock:
            if item_id in self.items:
                del self.items[item_id]
                self._flush()
                return True
            return False

    def reset_all(self) -> int:
        with self._lock:
            n = len(self.items)
            self._data["items"] = {}
            self._flush()
            return n

    # -- 统计 / 历史 -------------------------------------------------------
    def counts(self) -> dict[str, int]:
        c = Counter(v.get("status", "pending") for v in self.items.values())
        for st in PublishStatus:
            c.setdefault(st.value, 0)
        c["total"] = len(self.items)
        return dict(c)

    def history(self, limit: int | None = None, *, status: str | None = None,
                backend: str | None = None, item_id: str | None = None) -> list[dict[str, Any]]:
        if not self.history_file.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with self.history_file.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if status and obj.get("status") != status:
                        continue
                    if backend and obj.get("backend") != backend:
                        continue
                    if item_id and obj.get("item_id") != item_id:
                        continue
                    out.append(obj)
        except OSError as exc:
            raise StateError(f"历史文件读取失败: {self.history_file}", detail={"error": str(exc)}) from exc
        out.reverse()
        return out[:limit] if limit else out

    def history_summary(self, days: int = 7) -> dict[str, Any]:
        """近 N 天历史汇总（按天 × 状态计数）。"""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
        per_day: dict[str, Counter] = {}
        for rec in self.history():
            ts = rec.get("finished_at") or rec.get("started_at") or ""
            try:
                dt = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            if dt.replace(tzinfo=None) < cutoff:
                continue
            day = dt.strftime("%Y-%m-%d")
            per_day.setdefault(day, Counter())[rec.get("status", "unknown")] += 1
        return {d: dict(c) for d, c in sorted(per_day.items())}

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_file": str(self.state_file),
            "history_file": str(self.history_file),
            "counts": self.counts(),
            "items": self.items,
        }

    def export(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return p


def iter_history_paged(records: Iterable[dict[str, Any]], page: int, size: int) -> list[dict[str, Any]]:
    records = list(records)
    start = max(page - 1, 0) * size
    return records[start:start + size]
