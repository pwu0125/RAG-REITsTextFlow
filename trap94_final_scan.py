#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trap94_final_scan.py — 陷阱94 定点终扫：回填收官后锁定最终修复清单
用法:
  python trap94_final_scan.py            # 扫描+落盘清单+报告
  python trap94_final_scan.py --check    # 只读检查(不落盘)

规则(2026-10-05 用户裁定"按照正确的做法执行"):
  1. 只统计 ES=True 且 全扫描(pages_needing_ocr >= pdf_detect_page_count) 的文档
  2. 分 core(manifest 内) / non_core 两口径, 不混合
  3. 按 text.json 正文页/PDF页 比率分桶:
     severe  < 10%  (正文严重缺失)
     partial 10-90% (部分缺失)
     ok      >= 90% (基本完整, 不修)
  4. 清单锁定后写 docs/trap94_repair_manifest_YYYYMMDD.json — 此后以该文件为准, 不再移动靶
"""
import json, glob, os, sys, datetime

BASE = '/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow'
DATA = os.path.join(BASE, 'announcement_document_processing_local')
MANIFEST = os.path.join(DATA, 'processed_files_local.json')
DOCS = os.path.join(BASE, 'docs')


def scan():
    mf = json.load(open(MANIFEST))
    mkeys = set(mf['files'].keys())
    rows = {'core': [], 'non_core': []}
    for mp in glob.glob(os.path.join(DATA, '*/*/meta.json')):
        try:
            m = json.load(open(mp))
            if m.get('elasticsearch_database_done') is not True:
                continue
            pno = m.get('pages_needing_ocr')
            total = m.get('pdf_detect_page_count')
            if not (isinstance(pno, list) and isinstance(total, int) and total > 0 and len(pno) >= total):
                continue
            tp = mp.replace('meta.json', 'text.json')
            pages = len(json.load(open(tp)).get('pages', {})) if os.path.exists(tp) else 0
            ratio = pages / total
            bucket = 'severe' if ratio < 0.1 else ('partial' if ratio < 0.9 else 'ok')
            rows['core' if m.get('file_name') in mkeys else 'non_core'].append({
                'file': m.get('file_name'),
                'dir': os.path.relpath(mp.rsplit('/', 1)[0], DATA),
                'pdf_pages': total,
                'text_pages': pages,
                'ratio': round(ratio, 3),
                'bucket': bucket,
            })
        except Exception as e:
            print(f'ERR {mp[:60]}: {e}')
    return rows


def main():
    check_only = '--check' in sys.argv
    rows = scan()
    out = {'generated_at': datetime.datetime.now().isoformat(), 'rule': 'ratio: severe<0.1, partial<0.9, ok>=0.9; core/non_core split'}
    print(f"=== 陷阱94 定点终扫 {out['generated_at'][:19]} ===")
    total_repair_pages = 0
    for k in ('core', 'non_core'):
        r = rows[k]
        c = {b: sum(1 for x in r if x['bucket'] == b) for b in ('severe', 'partial', 'ok')}
        pages = sum(x['pdf_pages'] for x in r if x['bucket'] != 'ok')
        total_repair_pages += pages if k == 'core' else 0
        print(f"[{k}] 合计 {len(r)} = 严重缺 {c['severe']} + 部分缺 {c['partial']} + 基本完整 {c['ok']} | 待修页数 {pages}")
        out[k] = {'count': len(r), 'buckets': c, 'repair_pages': pages, 'items': sorted(r, key=lambda x: x['ratio'])}
    print(f"\n[核心类待修费用估算] ~{total_repair_pages} 页, 大头走API(qwen-vl-ocr), 估算 30-60 元")
    if not check_only:
        path = os.path.join(DOCS, f"trap94_repair_manifest_{datetime.date.today():%Y%m%d}.json")
        json.dump(out, open(path, 'w'), ensure_ascii=False, indent=1)
        print(f"清单已锁定: {path}")
        print("此后修复范围以本文件为准(移动靶问题终结)")


if __name__ == '__main__':
    main()
