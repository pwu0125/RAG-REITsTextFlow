#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_all.py — disk → meta.json → manifest 三层单命令同步

合并 reconcile_meta.py + rebuild_manifest.py：
  1. reconcile: 磁盘产物状态 → meta.json（双向校准）
  2. rebuild:  meta.json → processed_files_local.json

用法:
  python sync_all.py <BATCH>
  python sync_all.py --all
"""

import datetime
import os
import sys
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from reconcile_meta import reconcile_batch, print_report as reconcile_print

OUTPUT_DIR = os.path.join(SCRIPT_DIR, "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

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


def safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_core_doc(title):
    if not title:
        return False
    for kw in EXCLUDE_KEYWORDS:
        if kw in title:
            return False
    for kw in CORE_KEYWORDS:
        if kw in title:
            return True
    return False


def rebuild_manifest():
    """从磁盘 meta.json 重建 processed_files_local.json"""
    if not os.path.isdir(OUTPUT_DIR):
        print(f"ERROR: Output dir not found: {OUTPUT_DIR}")
        return

    from collections import Counter

    files_entry = {}
    meta_total = 0
    core_total = 0
    nested_skip = 0

    for root, _, filenames in os.walk(OUTPUT_DIR):
        if "meta.json" not in filenames:
            continue
        meta_total += 1
        meta_path = os.path.join(root, "meta.json")
        meta = safe_read_json(meta_path)
        if not meta:
            continue

        title = meta.get("announcement_title", "")
        if not is_core_doc(title):
            continue

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

    codes = set(e.get("fund_code", "") for e in files_entry.values())
    step_counts = Counter()
    for e in files_entry.values():
        for flag in FLAGS:
            if e.get(flag):
                step_counts[flag] += 1

    manifest = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": "",
        "output_dir": OUTPUT_DIR,
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

    print(f"\n  rebuild_manifest: {meta_total} meta.json → "
          f"{core_total} core (flat) + {nested_skip} nested skipped → "
          f"{len(files_entry)} manifest entries")
    print(f"  REITs: {len(codes)}")
    for flag in FLAGS:
        cnt = step_counts.get(flag, 0)
        if cnt > 0:
            print(f"    {flag}: {cnt}/{len(files_entry)}")


def sync_all(batch_name=None, batch_codes=None):
    """执行完整三层同步: disk → meta.json → manifest"""

    # Step 1: disk → meta.json (reconcile)
    if batch_codes is None:
        if batch_name is None:
            print("ERROR: 需要指定 --all 或 <BATCH>")
            sys.exit(1)
        config_path = os.path.join(SCRIPT_DIR, "BATCH_CONFIG.json")
        config = safe_read_json(config_path)
        if not config or batch_name not in config:
            print(f"批次 '{batch_name}' 不在 BATCH_CONFIG.json 中")
            sys.exit(1)
        batch_codes = config[batch_name]["codes"]

    print(f"\n{'='*50}")
    print(f"  sync_all: disk → meta.json → manifest")
    print(f"  Batch: {batch_name or 'ALL'}")
    print(f"{'='*50}")

    report = reconcile_batch(batch_codes)
    reconcile_print(report, batch_name or "ALL")

    # Step 2: meta.json → manifest (rebuild)
    rebuild_manifest()

    print(f"\n✅ disk → meta.json → manifest 三层同步完成\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python sync_all.py <BATCH>")
        print("      python sync_all.py --all")
        print("示例: python sync_all.py B2_splmnt")
        sys.exit(1)

    if sys.argv[1] == "--all":
        # Scan all fund codes from BATCH_CONFIG
        config_path = os.path.join(SCRIPT_DIR, "BATCH_CONFIG.json")
        config = safe_read_json(config_path)
        if not config:
            print("ERROR: BATCH_CONFIG.json not found")
            sys.exit(1)
        all_codes = set()
        for batch_info in config.values():
            if isinstance(batch_info, dict) and "codes" in batch_info:
                all_codes.update(batch_info["codes"])
        sync_all(batch_name="ALL", batch_codes=list(all_codes))
    else:
        sync_all(batch_name=sys.argv[1])
