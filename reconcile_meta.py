#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
reconcile_meta.py — 双向校准 meta.json 与磁盘实况

每次 batch_runner 启动时自动运行（快层 <1秒）。
校对规则：
  - 中间物（table_image/temp_pdf_images）被 step5 合法删除 → 下游已完成则保持 True
  - 终产物（text.json/embedding.json）不存在 → 真丢失 → False
  - 产物存在但 meta=False → 补标 True

内部参数：
  VERIFY_FULL = False  → 磁盘快层（默认）
  VERIFY_FULL = True   → ES + Milvus 全量校验
"""

import datetime
import json
import os
import sys
import urllib.request

# ═══════════════════════════════════════
# 内部参数（改这里，非 --flag）
# ═══════════════════════════════════════
VERIFY_FULL = False

# ═══════════════════════════════════════
# 路径
# ═══════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")


def safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _build_es_cache() -> set:
    """Build set of codes that have data in ES (R2 anchor)."""
    try:
        req = urllib.request.Request(
            'http://localhost:9200/reits_announcements/_search',
            data=json.dumps({
                'size': 0,
                'aggs': {'by_fund': {'terms': {'field': 'fund_code', 'size': 100}}}
            }).encode(),
            headers={'Content-Type': 'application/json'})
        es_data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return {b['key'] for b in es_data['aggregations']['by_fund']['buckets']}
    except Exception:
        return set()


# 上游标志（step1 → step8_1 完成则全部为 True）
ALL_UPSTREAM_FLAGS = [
    'text_extracted',
    'table_detection_vector_done',
    'table_detection_scan_done',
    'table_describe_done',
    'not_table_describe_done',
    'merge_done',
    'text_segmentation',
    'embedding_done',
    'elasticsearch_database_done',
]


def reconcile_batch(batch_codes: list[str]) -> dict:
    """扫描指定 codes 的全部文档，双向校准 meta.json。返回校正清单。"""
    manifest = safe_read_json(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {}) or {}

    # ═══ R2 ES 锚定: 构建 ES 缓存 ═══
    es_cache = _build_es_cache()

    report = {"ok": 0, "fixed_meta_true": [], "reset_meta_false": [], "es_anchored": 0}
    intermediate_steps = ["table_detection_scan_done", "table_describe_done"]

    for file_name, info in files_map.items():
        fund_code = (info or {}).get("fund_code", "")
        if fund_code not in batch_codes:
            continue

        doc_dir = os.path.join(OUTPUT_DIR, fund_code, os.path.splitext(file_name)[0])
        meta_path = os.path.join(doc_dir, "meta.json")
        meta = safe_read_json(meta_path) or {}
        changed = False

        # ═══ R2 + R7: ES 锚定守卫（最优先规则）══════
        # ES 有数据 = step1→8_1 已过 → 所有上游标志强制 True
        # 即使磁盘上 table_image/ 或 not_table_describe.json 缺失
        # （已被 step5 合法清理），也不回退其标志
        if fund_code in es_cache:
            es_anchored = False
            for flag in ALL_UPSTREAM_FLAGS:
                if not meta.get(flag):
                    meta[flag] = True
                    es_anchored = True
                    changed = True
            if es_anchored:
                report["es_anchored"] += 1
            # ES 已锚定，跳过所有磁盘检查
            if changed:
                safe_write_json(meta_path, meta)
                report["fixed_meta_true"].append(f"{fund_code}/{os.path.basename(doc_dir)}")
            else:
                report["ok"] += 1
            continue
        # ═══ ES 锚定结束，以下仅对无 ES 数据的 code 执行 ═══

        # ── step2: text.json ──
        text_path = os.path.join(doc_dir, "text.json")
        text_exists = os.path.exists(text_path) and os.path.getsize(text_path) > 1024
        if text_exists and not meta.get("text_extracted"):
            meta["text_extracted"] = True
            changed = True
        elif not text_exists and meta.get("text_extracted"):
            meta["text_extracted"] = False
            changed = True

        # ── step3_2: table_image/ ──
        ti_dir = os.path.join(doc_dir, "table_image")
        ti_exists = os.path.isdir(ti_dir) and bool(os.listdir(ti_dir))
        merge_done = meta.get("merge_done", False)
        if ti_exists and not meta.get("table_detection_scan_done"):
            meta["table_detection_scan_done"] = True
            changed = True
        elif not ti_exists and meta.get("table_detection_scan_done") and not merge_done:
            # 中间物缺失但下游未完成 → 数据丢失
            meta["table_detection_scan_done"] = False
            changed = True

        # ── step4_1_1: table_describe.json ──
        td_path = os.path.join(doc_dir, "table_describe.json")
        td_exists = os.path.exists(td_path) and os.path.getsize(td_path) > 100
        if td_exists and not meta.get("table_describe_done"):
            meta["table_describe_done"] = True
            changed = True
        elif not td_exists and meta.get("table_describe_done") and not merge_done:
            meta["table_describe_done"] = False
            changed = True

        # ── step4_2_1: not_table_describe.json ──
        ntd_path = os.path.join(doc_dir, "not_table_describe.json")
        ntd_exists = os.path.exists(ntd_path) and os.path.getsize(ntd_path) > 100
        if ntd_exists and not meta.get("not_table_describe_done"):
            meta["not_table_describe_done"] = True
            changed = True
        elif not ntd_exists and meta.get("not_table_describe_done") and not merge_done and not meta.get("table_describe_done"):
            meta["not_table_describe_done"] = False
            changed = True

        # ── step6: text_segmentation.json ──
        seg_path = os.path.join(doc_dir, "text_segmentation.json")
        seg_exists = os.path.exists(seg_path) and os.path.getsize(seg_path) > 100
        if seg_exists and not meta.get("text_segmentation"):
            meta["text_segmentation"] = True
            changed = True
        elif not seg_exists and meta.get("text_segmentation"):
            meta["text_segmentation"] = False
            changed = True

        # ── step7: text_segmentation_embedding.json ──
        emb_path = os.path.join(doc_dir, "text_segmentation_embedding.json")
        emb_exists = os.path.exists(emb_path) and os.path.getsize(emb_path) > 100
        if emb_exists and not meta.get("embedding_done"):
            meta["embedding_done"] = True
            changed = True
        elif not emb_exists and meta.get("embedding_done"):
            meta["embedding_done"] = False
            changed = True

        if changed:
            safe_write_json(meta_path, meta)
            report["fixed_meta_true"].append(f"{fund_code}/{os.path.basename(doc_dir)}")

        if not changed:
            report["ok"] += 1

    # ── 8️⃣ VERIFY_FULL: ES + Milvus ──
    if VERIFY_FULL:
        try:
            from elasticsearch import Elasticsearch
            es = Elasticsearch(["http://localhost:9200"])
            for file_name, info in files_map.items():
                fund_code = (info or {}).get("fund_code", "")
                if fund_code not in batch_codes:
                    continue
                doc_dir = os.path.join(OUTPUT_DIR, fund_code, os.path.splitext(file_name)[0])
                meta_path = os.path.join(doc_dir, "meta.json")
                meta = safe_read_json(meta_path) or {}
                meta_changed = False

                resp = es.search(index="reits_announcements",
                                 body={"query": {"term": {"source_file.keyword": file_name}},
                                       "size": 0})
                es_count = resp["hits"]["total"]["value"]
                es_exists = es_count > 0

                if es_exists and not meta.get("elasticsearch_database_done"):
                    meta["elasticsearch_database_done"] = True
                    meta_changed = True
                elif not es_exists and meta.get("elasticsearch_database_done"):
                    meta["elasticsearch_database_done"] = False
                    meta_changed = True
                    report["reset_meta_false"].append(f"{fund_code}/{os.path.basename(doc_dir)} (ES)")

                if meta_changed:
                    safe_write_json(meta_path, meta)
        except Exception as e:
            print(f"  ⚠️  ES 校验失败: {e}")

        try:
            from pymilvus import connections, Collection
            connections.connect("default", host="localhost", port="19530")
            collection = Collection("reits_announcement")
            collection.load()
            for file_name, info in files_map.items():
                fund_code = (info or {}).get("fund_code", "")
                if fund_code not in batch_codes:
                    continue
                doc_dir = os.path.join(OUTPUT_DIR, fund_code, os.path.splitext(file_name)[0])
                meta_path = os.path.join(doc_dir, "meta.json")
                meta = safe_read_json(meta_path) or {}
                meta_changed = False

                results = collection.query(
                    expr=f'source_file == "{file_name}"',
                    output_fields=["id"],
                    limit=1
                )
                mv_exists = len(results) > 0

                if mv_exists and not meta.get("vector_database_done"):
                    meta["vector_database_done"] = True
                    meta_changed = True
                elif not mv_exists and meta.get("vector_database_done"):
                    meta["vector_database_done"] = False
                    meta_changed = True
                    report["reset_meta_false"].append(f"{fund_code}/{os.path.basename(doc_dir)} (Milvus)")

                if meta_changed:
                    safe_write_json(meta_path, meta)
        except Exception as e:
            print(f"  ⚠️  Milvus 校验失败: {e}")

    return report


def print_report(report: dict, batch_name: str):
    ok = report["ok"]
    fixed = report["fixed_meta_true"]
    reset = report["reset_meta_false"]
    total = ok + len(fixed) + len(reset)

    print(f"\n{'='*50}")
    print(f"  reconcile_meta: {batch_name}")
    print(f"  VERIFY_FULL = {VERIFY_FULL}")
    print(f"{'='*50}")
    print(f"  总文档: {total}")
    print(f"  ✅ 一致: {ok}")
    if fixed:
        print(f"  🟡 补标: {len(fixed)}（产物存在，meta=False → True）")
        for f in fixed[:5]:
            print(f"     {f}")
        if len(fixed) > 5:
            print(f"     ... 共 {len(fixed)} 项")
    if reset:
        print(f"  🔴 重置: {len(reset)}（meta=True 但产物丢失 → False）")
        for r in reset[:5]:
            print(f"     {r}")
        if len(reset) > 5:
            print(f"     ... 共 {len(reset)} 项")
    if not fixed and not reset:
        print(f"  ✅ 无需修正")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python reconcile_meta.py <BATCH>")
        print("示例: python reconcile_meta.py B2g")
        sys.exit(1)

    batch_name = sys.argv[1]
    config_path = os.path.join(SCRIPT_DIR, "BATCH_CONFIG.json")
    config = safe_read_json(config_path)
    if not config or batch_name not in config:
        print(f"批次 '{batch_name}' 不在 BATCH_CONFIG.json 中")
        sys.exit(1)

    batch_codes = config[batch_name]["codes"]
    report = reconcile_batch(batch_codes)
    print_report(report, batch_name)

    # ── MetaGuard 完成标记 ──
    metaguard_path = os.path.join(SCRIPT_DIR, ".metaguard_status.json")
    status_data = {
        "batch": batch_name,
        "status": "pass" if not report["reset_meta_false"] else "needs_review",
        "checked_at": datetime.datetime.now().isoformat(),
        "stats": {
            "ok": report["ok"],
            "fixed_meta_true": len(report["fixed_meta_true"]),
            "reset_meta_false": len(report["reset_meta_false"]),
        },
    }
    safe_write_json(metaguard_path, status_data)
    sys.exit(0)
