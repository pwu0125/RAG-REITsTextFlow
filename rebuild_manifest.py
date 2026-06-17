#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从磁盘 meta.json 反扫重建 processed_files_local.json
- 只纳入扁平结构（parent dir = fund_code）
- 用目录名作为 file_name（避免截断和撞名）
"""
import os
import json
import logging
from collections import Counter

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "announcement_document_processing_local", "processed_files_local.json")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

CORE_KEYWORDS = ["年度报告", "中期报告", "季度报告", "招募说明书"]
EXCLUDE_KEYWORDS = ["审计报告", "提示性"]

FLAGS = [
    "text_extracted",
    "table_detection_vector_done",
    "table_detection_scan_done",
    "table_describe_done",
    "not_table_describe_done",
    "merge_done",
    "text_segmentation",
    "embedding_done",
    "vector_database_done",
    "elasticsearch_database_done",
]


def is_core_doc(title: str) -> bool:
    if not title:
        return False
    for kw in EXCLUDE_KEYWORDS:
        if kw in title:
            return False
    for kw in CORE_KEYWORDS:
        if kw in title:
            return True
    return False


def rebuild():
    data_dir = os.path.join(OUTPUT_DIR, "announcement_document_processing_local")
    if not os.path.isdir(data_dir):
        logging.error("Data dir not found: %s", data_dir)
        return

    files_entry = {}
    meta_total = 0
    core_total = 0
    nested_skip = 0

    for root, _, filenames in os.walk(data_dir):
        if "meta.json" not in filenames:
            continue
        meta_total += 1
        meta_path = os.path.join(root, "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        title = meta.get("announcement_title", "")
        if not is_core_doc(title):
            continue

        # Only flat structure: parent dir = fund_code (6 digits)
        parent_dir = os.path.basename(os.path.dirname(root))
        if not (len(parent_dir) == 6 and parent_dir.isdigit()):
            nested_skip += 1
            continue

        core_total += 1
        dir_name = os.path.basename(root)
        file_name = dir_name + ".pdf"

        entry = {
            "file_name": file_name,
            "file_path": meta.get("file_path", ""),
            "date": meta.get("date", ""),
            "fund_code": meta.get("fund_code", ""),
            "short_name": meta.get("short_name", ""),
            "announcement_title": title,
            "doc_type_1": meta.get("doc_type_1", ""),
            "doc_type_2": meta.get("doc_type_2", ""),
            "announcement_link": meta.get("announcement_link", ""),
        }
        for flag in FLAGS:
            entry[flag] = bool(meta.get(flag, False))

        files_entry[file_name] = entry

    # Build manifest
    codes = set(e.get("fund_code", "") for e in files_entry.values())
    step_counts = Counter()
    for e in files_entry.values():
        for flag in FLAGS:
            if e.get(flag):
                step_counts[flag] += 1

    import datetime
    manifest = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": "",
        "output_dir": data_dir,
        "directory_structure": "{fund_code}/{pdf_folder}/",
        "summary": {
            "total": len(files_entry),
            "reits": len(codes),
            **{flag: step_counts.get(flag, 0) for flag in FLAGS},
        },
        "files": files_entry,
    }

    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    logging.info(
        "Scanned %d meta.json → %d core (flat) + %d nested skipped → %d manifest entries",
        meta_total, core_total, nested_skip, len(files_entry),
    )
    logging.info("REITs: %d", len(codes))
    for flag in FLAGS:
        cnt = step_counts.get(flag, 0)
        if cnt > 0:
            logging.info("  %s: %d/%d", flag, cnt, len(files_entry))

    # ═══ Phase C: 双向交叉校验 (manifest ↔ ES ↔ Milvus) ═══
    _cross_validate(files_entry)


def _cross_validate(files_entry: dict):
    """双向交叉校验: manifest ↔ ES ↔ Milvus
    正向: manifest 标记完成 → 查数据库实体验证
    反向: 数据库有实体 → 查 manifest 标志验证
    """
    import urllib.request

    es_false_positive = []   # manifest es_done=True 但 ES 无数据
    es_false_negative = []   # ES 有数据 但 manifest es_done=False
    mv_false_positive = []   # manifest vd_done=True 但 Milvus 无数据
    mv_false_negative = []   # Milvus 有数据 但 manifest vd_done=False

    # ── Fetch ES per-code data ──
    es_docs = {}
    try:
        req = urllib.request.Request(
            'http://localhost:9200/reits_announcements/_search',
            data=json.dumps({
                'size': 0,
                'aggs': {'by_fund': {'terms': {'field': 'fund_code', 'size': 100}}}
            }).encode(),
            headers={'Content-Type': 'application/json'})
        es_data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        es_docs = {b['key']: b['doc_count'] for b in es_data['aggregations']['by_fund']['buckets']}
    except Exception as e:
        logging.warning("  ⚠️ ES 交叉校验失败: %s", e)

    # ── Fetch Milvus per-code presence ──
    mv_docs = {}
    try:
        from pymilvus import connections, Collection
        connections.connect(host='127.0.0.1', port='19530', timeout=10)
        col = Collection('reits_announcement')
        col.load()
        codes = set(e.get('fund_code', '') for e in files_entry.values())
        for code in codes:
            try:
                if col.query(expr=f'fund_code == "{code}"', output_fields=['id'], limit=1):
                    mv_docs[code] = True
            except Exception:
                pass
    except Exception as e:
        logging.warning("  ⚠️  Milvus 交叉校验失败: %s", e)

    # ── Per-doc validation ──
    for file_name, entry in files_entry.items():
        code = entry.get('fund_code', '')

        # 正向: es_done=True → ES 有数据?
        if entry.get('elasticsearch_database_done') and code not in es_docs:
            es_false_positive.append(code)
        # 反向: ES 有数据 → es_done=True?
        if code in es_docs and not entry.get('elasticsearch_database_done'):
            es_false_negative.append(f"{code}/{file_name[:40]}")

        # 正向: vd_done=True → Milvus 有数据?
        if entry.get('vector_database_done') and code not in mv_docs:
            mv_false_positive.append(code)
        # 反向: Milvus 有数据 → vd_done=True?
        if code in mv_docs and not entry.get('vector_database_done'):
            mv_false_negative.append(f"{code}/{file_name[:40]}")

    # ── Report ──
    es_fp = list(set(es_false_positive))
    es_fn = list(set(es_false_negative))[:10]
    mv_fp = list(set(mv_false_positive))
    mv_fn = list(set(mv_false_negative))[:10]

    if es_fp:
        logging.warning("  🔴 ES 假阳性 (es_done=True 但 ES 无数据): %d codes — %s",
                       len(es_fp), ", ".join(es_fp[:10]))
    if es_fn:
        logging.warning("  🟡 ES 假阴性 (ES 有数据 但 es_done=False): %d docs — %s ...",
                       len(es_fn), ", ".join(es_fn[:5]))
    if mv_fp:
        logging.warning("  🔴 MV 假阳性 (vd_done=True 但 Milvus 无数据): %d codes — %s",
                       len(mv_fp), ", ".join(mv_fp[:10]))
    if mv_fn:
        logging.warning("  🟡 MV 假阴性 (Milvus 有数据 但 vd_done=False): %d docs — %s ...",
                       len(mv_fn), ", ".join(mv_fn[:5]))

    if not (es_fp or es_fn or mv_fp or mv_fn):
        logging.info("  ✅ 交叉校验通过: manifest ↔ ES ↔ Milvus 一致")


if __name__ == "__main__":
    rebuild()
