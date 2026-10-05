#!/bin/bash
# sync_text_flags_after_backfill.sh — 大回填收官后: 清3389份陈旧text侧False(双写同步)
# 前置: 大回填(catchup_oneshot)必须已结束 — 本脚本每小时自查, 管道在跑就静默等
# 安全: ①只动 meta=True且text=False 的ES标志(ES实况仲裁过的方向) ②备份先行 ③幂等可重跑
cd /Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow

# 等大回填结束(锁文件消失且无pipeline进程)
while [ -f /tmp/catchup_pipeline.lock ] || pgrep -f 'reits_daily_rag.py pipeline' >/dev/null 2>&1; do
    sleep 3600
done

LOG=/tmp/text_flag_sync_$(date +%Y%m%d).log
echo "[$(date '+%F %T')] 大回填已结束, 开始text标志同步" >> "$LOG"

/Users/pyemini/anaconda3/bin/python - << 'PYEOF' >> "$LOG" 2>&1
import json, glob, os, shutil
BASE = 'announcement_document_processing_local'
BK = f'manifest_backups/text_es_sync_20261005'
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
        if tm.get('elasticsearch_database_done') is True: continue  # 已同步
        # 备份(只备本次要改的)
        doc = mp.rsplit('/', 1)[0]
        bkd = os.path.join(BK, doc.split('/')[-1] + '_' + doc.split('/')[-2])
        if not os.path.exists(bkd):
            os.makedirs(bkd, exist_ok=True)
            shutil.copy2(tp, f'{bkd}/text.json.bak')
        # 双写同步: text侧 True (meta侧已True不动)
        tm['elasticsearch_database_done'] = True
        tj['metadata'] = tm
        json.dump(tj, open(tp, 'w'), ensure_ascii=False)
        fixed += 1
    except Exception as e:
        print(f'ERR {mp}: {e}')
print(f'同步完成: {fixed} 份 text.json ES标志 → True (备份在 {BK})')

# 复核: 分歧应清零
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
echo "[$(date '+%F %T')] FLAG_SYNC_DONE" >> "$LOG"
echo "FLAG_SYNC_DONE"
