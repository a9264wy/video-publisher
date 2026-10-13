#!/usr/bin/env bash
# 目录里剩余 6 条《》条目：继续每隔一天发 3 条（10-18 / 10-19，早 8:00）。
# 承接 10-14/15/16（各 3 条）与 10-17（潮汐 1 条）的排期节奏。

set -u
cd "$(dirname "$0")/.."

LOG=logs/batch_redo2.log
: > "$LOG"

run_one() {
  local item="$1" when="$2"
  echo "======== $item → $when ========" | tee -a "$LOG"
  ./.venv/Scripts/python.exe main.py publish --item "$item" --limit 1 \
      --live --scheduled-at "$when" --force >> "$LOG" 2>&1
  local rc=$?
  grep -E "封面已上传|已参加活动|位置已重置|平台原生定时发表已设定|已提交|成功：|失败|ERROR" "$LOG" \
    | tail -8 | sed 's/^/    /'
  echo "    [exit=$rc]" | tee -a "$LOG"
}

run_one "《为什么会下雨》"   "2026-10-18 08:00"
run_one "《为什么会有四季》" "2026-10-18 08:00"
run_one "《星星为什么眨眼》" "2026-10-18 08:00"
run_one "《雪为什么是白的》" "2026-10-19 08:00"
run_one "《鱼怎么在水里呼吸》" "2026-10-19 08:00"
run_one "《鸟为什么会飞》"   "2026-10-19 08:00"

echo "ALL DONE" | tee -a "$LOG"
