#!/bin/bash
# trap94_finish_watchdog.sh — 回填收官看门狗
# 职责(2026-10-05 用户批准"按照正确的做法执行"):
#   1. 每10分钟探测回填管道是否结束(reits_daily_rag pipeline / reits_rag_catchup)
#   2. 结束后: 等队列空(dry-run=0) → 跑 trap94_final_scan.py 锁定终版清单
#   3. 结果写入日志 + 触发Hermes通知(通过 marker 文件)
# 幂等: 若终扫清单已存在当天文件则跳过
BASE=/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow
LOG=/tmp/trap94_watchdog.log
MARKER=/tmp/TRAP94_FINAL_SCAN_READY
PY=/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python
TODAY=$(date +%Y%m%d)

say() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

say "看门狗启动 (PID $$)"

for i in $(seq 1 72); do   # 72×10min = 12小时上限
  # 管道活性探测
  if pgrep -f 'reits_daily_rag.py pipeline' >/dev/null 2>&1 || pgrep -f 'reits_rag_catchup.py --batch' >/dev/null 2>&1; then
    say "轮次$i: 回填仍在跑, 继续等待"
    sleep 600
    continue
  fi
  # 管道结束 → 队列检查
  DRY=$(cd "$BASE" && env -u PYTHONHOME -u PYTHONPATH "$PY" /Users/pyemini/.hermes/scripts/reits_rag_catchup.py --dry-run --max-total 0 2>/dev/null | grep -o '待处理 [0-9]* 份' | head -1)
  say "轮次$i: 管道已停, 队列状态: ${DRY:-探测失败}"
  N=$(echo "$DRY" | grep -o '[0-9]*' | head -1)
  if [ "${N:-1}" = "0" ]; then
    say "队列空 → 执行定点终扫"
    cd "$BASE" && /Users/pyemini/anaconda3/bin/python trap94_final_scan.py >> "$LOG" 2>&1
    RC=$?
    if [ $RC -eq 0 ]; then
      echo "TRAP94_FINAL_SCAN_READY $(date '+%F %T')" > "$MARKER"
      say "终扫完成, marker 已置: $MARKER"
    else
      say "终扫失败 RC=$RC (下轮重试)"
      sleep 600
      continue
    fi
    exit 0
  else
    say "队列还有 ${N} 份, 但管道已停 — 等待接力/重跑(10分钟后再查)"
    sleep 600
  fi
done
say "12小时超时退出(回填未收官或队列未清空) — 需人工介入"
exit 1
