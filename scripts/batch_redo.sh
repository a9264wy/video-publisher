#!/usr/bin/env bash
# 全部重新做：把 9 条内容按 3/3/3 排到 10-14/15/16 的 08:00。
# 潮汐（14）已在 01:20 单独验证过（双封面 + 活动 + 无位置 + 话题去重），排 10-17。
#
# 每条独立跑：--item 只接受单条前缀，且 scheduled_at 是整批共用的，
# 所以只能一条一个命令。全程输出落盘 batch_redo.log。

set -u
cd "$(dirname "$0")/.."

LOG=logs/batch_redo.log
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

run_one "01-风是从哪来的"     "2026-10-14 08:00"
run_one "02-海水为什么是咸的" "2026-10-14 08:00"
run_one "03-地震是怎么来的"   "2026-10-14 08:00"
run_one "08-火山为什么爆发"   "2026-10-15 08:00"
run_one "09-冰为什么浮在水上" "2026-10-15 08:00"
run_one "10-回声是怎么来的"   "2026-10-15 08:00"
run_one "11-磁铁为什么吸铁"   "2026-10-16 08:00"
run_one "12-影子是怎么来的"   "2026-10-16 08:00"
run_one "13-叶子为什么是绿的" "2026-10-16 08:00"

echo "ALL DONE" | tee -a "$LOG"
