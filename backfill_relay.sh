#!/bin/bash
# backfill_relay.sh — 12h熔断接力器(2026-10-05): 熔断到点后续跑, 直至dry-run队列为0, 然后做text标志同步+总对账
# 背景: 熔断是防僵尸保险丝不是限速; 大回填需跨多轮12h窗口一次完成(用户裁定"不要分开进行")
cd /Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow
PY=/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python
export PYTHONHOME= PYTHONPATH=
LOG=/tmp/backfill_relay_$(date +%Y%m%d_%H%M).log
LOCK=/tmp/catchup_pipeline.lock

say() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
say "接力器启动"

for ROUND in 1 2 3; do
    # 等管道进程退出(上一轮熔断/完成)
    while pgrep -f 'reits_daily_rag.py pipeline' >/dev/null 2>&1; do sleep 60; done
    # 清可能的残留锁
    rm -f "$LOCK"
    # 残留队列检查
    PENDING=$(env -u PYTHONHOME -u PYTHONPATH "$PY" /Users/pyemini/.hermes/scripts/reits_rag_catchup.py --dry-run --max-total 0 2>/dev/null | grep -m1 DRY-RUN)
    say "第${ROUND}轮 残留: $PENDING"
    echo "$PENDING" | grep -qE '待处理 0 份' && break
    # 有残留 → 再拉一轮(12h熔断 + 环境变量)
    say "第${ROUND}轮 续跑启动"
    echo $$ > "$LOCK"
    CATCHUP_PIPELINE_TIMEOUT=43200 env -u PYTHONHOME -u PYTHONPATH "$PY" /Users/pyemini/.hermes/scripts/reits_rag_catchup.py --batch 500 --max-total 0 --max-attempts 2 >> "$LOG" 2>&1
    RC=$?
    say "第${ROUND}轮结束 RC=$RC"
    rm -f "$LOCK"
done

# ==== 收官: text标志同步(3千余份陈旧False → True, 备份先行) ====
say "开始text标志同步"
env -u PYTHONHOME -u PYTHONPATH "$PY" - << 'PYEOF' >> "$LOG" 2>&1
import json, glob, os, shutil
BASE = 'announcement_document_processing_local'
BK = 'manifest_backups/text_es_sync_20261005'
os.makedirs(BK, exist_ok=True)
fixed = 0
for mp in glob.glob(f'{BASE}/*/*/meta.json'):
    tp = mp.replace('meta.json', 'text.json')
    if not os.path.exists(tp): continue
    try:
        m = json.load(open(mp))
        if m.get('elasticsearch_database_done') is not True: continue
        tj = json.load(open(tp))
        tm = tj.get('metadata', {})
        if tm.get('elasticsearch_database_done') is True: continue
        doc = mp.rsplit('/', 1)[0]
        bkd = os.path.join(BK, doc.split('/')[-1] + '_' + doc.split('/')[-2])
        if not os.path.exists(bkd):
            os.makedirs(bkd, exist_ok=True)
            shutil.copy2(tp, f'{bkd}/text.json.bak')
        tm['elasticsearch_database_done'] = True
        tj['metadata'] = tm
        json.dump(tj, open(tp, 'w'), ensure_ascii=False)
        fixed += 1
    except Exception as e:
        print(f'ERR {mp}: {e}')
print(f'同步完成: {fixed} 份 (备份在 {BK})')
diff = 0
for mp in glob.glob(f'{BASE}/*/*/meta.json'):
    tp = mp.replace('meta.json', 'text.json')
    if not os.path.exists(tp): continue
    try:
        m = json.load(open(mp)).get('elasticsearch_database_done')
        t = json.load(open(tp)).get('metadata', {}).get('elasticsearch_database_done')
        if m != t: diff += 1
    except Exception: pass
print(f'复核: 剩余分歧 {diff} 份 (预期0)')
PYEOF

# ==== 总对账 ====
ES_N=$(curl -s -m 8 'http://127.0.0.1:9200/reits_announcements/_count' | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])' 2>/dev/null)
FINAL=$(env -u PYTHONHOME -u PYTHONPATH "$PY" /Users/pyemini/.hermes/scripts/reits_rag_catchup.py --dry-run --max-total 0 2>/dev/null | grep -m1 DRY-RUN)
say "总对账: ES=$ES_N | 最终残留: $FINAL"
say "RELAY_ALL_DONE"
echo "RELAY_ALL_DONE ES=$ES_N"
