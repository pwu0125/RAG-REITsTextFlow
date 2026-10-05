#!/bin/bash
# catchup_oneshot.sh — 大回填一次性跑完 v2(A线 qwen 视觉, 2026-10-05)
# v2 修复陷阱92: v1看门狗的pgrep模式含"reits_daily_rag.py"字样, 会被catchup的
#   GUARD_PATTERNS自匹配 → catchup判定"管道在跑"静默让路秒退。改锁文件判活。
# 284份: 52轻量(分红/运营) + ~40本招募书 + 144评估报告 + 历史扩募等
# 熔断43,200s(12h) + 看门狗90min零进展自杀; 进度逐阶段落盘, 断了重跑本脚本自动续
cd /Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow
PY=/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python
LOG=/tmp/catchup_oneshot_$(date +%Y%m%d_%H%M).log
LOCK=/tmp/catchup_pipeline.lock

# 零进展看护 v2: 锁文件判活, 不pgrep(避免命令行字样被GUARD自匹配)
(
    while true; do
        sleep 300
        if [ -f "$LOCK" ]; then
            RECENT=$(find announcement_document_processing_local -name 'text.json' -mmin -90 2>/dev/null | wc -l | tr -d ' ')
            if [ "$RECENT" -eq 0 ]; then
                echo "[$(date '+%F %T')] 看门狗: 管道活着但90min零进展, kill" >> "$LOG"
                PID_IN_LOCK=$(cat "$LOCK" 2>/dev/null)
                [ -n "$PID_IN_LOCK" ] && kill "$PID_IN_LOCK" 2>/dev/null
                pkill -f 'reits_daily_rag.py pipeline' 2>/dev/null
                exit 0
            fi
        fi
    done
) &
WATCHDOG=$!
trap "kill $WATCHDOG 2>/dev/null; rm -f $LOCK" EXIT

echo "[$(date '+%F %T')] 一次性大回填启动: 284份, 熔断12h, 看门狗90min" >> "$LOG"
echo $$ > "$LOCK"
CATCHUP_PIPELINE_TIMEOUT=43200 "$PY" /Users/pyemini/.hermes/scripts/reits_rag_catchup.py --batch 500 --max-total 0 --max-attempts 2 >> "$LOG" 2>&1
RC=$?
rm -f "$LOCK"
kill $WATCHDOG 2>/dev/null

# 复核对账
ES_N=$(curl -s -m 8 'http://127.0.0.1:9200/reits_announcements/_count' | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])' 2>/dev/null)
"$PY" /Users/pyemini/.hermes/scripts/reits_rag_catchup.py --dry-run >> "$LOG" 2>&1
echo "[$(date '+%F %T')] 完成 RC=$RC ES=$ES_N" >> "$LOG"
echo "ONESHOT_DONE RC=$RC ES=$ES_N LOG=$LOG"
