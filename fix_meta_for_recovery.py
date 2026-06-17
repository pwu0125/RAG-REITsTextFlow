#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 1: Fix meta.json flags for ES + Milvus recovery.
Scans all docs in the 9 completed batches (B1a-B2f, 26 fund codes).
If text_segmentation_embedding.json exists on disk, sets:
  - text_segmentation = true
  - embedding_done = true
  - elasticsearch_database_done = false
  - vector_database_done = false
"""

import os
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

from common_utils import safe_json_load, safe_json_dump

PROCESSING_DIR = SCRIPT_DIR / "announcement_document_processing_local"
BATCH_CONFIG_PATH = SCRIPT_DIR / "BATCH_CONFIG.json"

# 9 completed batches and their 26 fund codes
COMPLETED_BATCHES = ["B1a", "B1b", "B1c", "B2a", "B2b", "B2c", "B2d", "B2e", "B2f"]

def main():
    config = safe_json_load(str(BATCH_CONFIG_PATH))

    # Collect unique fund codes from completed batches
    fund_codes = set()
    for batch_name in COMPLETED_BATCHES:
        codes = config.get(batch_name, {}).get("codes", [])
        fund_codes.update(codes)

    print(f"Completed batches: {COMPLETED_BATCHES}")
    print(f"Unique fund codes: {sorted(fund_codes)} ({len(fund_codes)} total)")
    print()

    fixed_count = 0
    skipped_no_seg = 0
    skipped_no_meta = 0
    total_checked = 0

    for code in sorted(fund_codes):
        code_dir = PROCESSING_DIR / code
        if not code_dir.is_dir():
            print(f"  WARN: fund code dir not found: {code_dir}")
            continue

        for doc_dir in sorted(code_dir.iterdir()):
            if not doc_dir.is_dir():
                continue
            total_checked += 1

            meta_path = doc_dir / "meta.json"
            seg_emb_path = doc_dir / "text_segmentation_embedding.json"

            if not seg_emb_path.exists():
                skipped_no_seg += 1
                continue

            if not meta_path.exists():
                skipped_no_meta += 1
                print(f"  WARN: meta.json missing but seg_emb exists: {doc_dir.name}")
                continue

            meta = safe_json_load(str(meta_path))
            if not isinstance(meta, dict):
                print(f"  WARN: meta.json is not a dict: {meta_path}")
                continue

            meta["text_segmentation"] = True
            meta["embedding_done"] = True
            meta["elasticsearch_database_done"] = False
            meta["vector_database_done"] = False
            safe_json_dump(meta, str(meta_path))

            # Also fix text.json metadata — it takes precedence in _infer_status_from_files
            text_path = doc_dir / "text.json"
            if text_path.exists():
                text_json = safe_json_load(str(text_path))
                if isinstance(text_json, dict):
                    text_meta = text_json.get("metadata", {})
                    if isinstance(text_meta, dict):
                        text_meta["elasticsearch_database_done"] = False
                        text_meta["vector_database_done"] = False
                        text_json["metadata"] = text_meta
                        safe_json_dump(text_json, str(text_path))

            fixed_count += 1

    print(f"\n  Total docs checked: {total_checked}")
    print(f"  Fixed (seg_emb exists): {fixed_count}")
    print(f"  Skipped (no seg_emb): {skipped_no_seg}")
    if skipped_no_meta:
        print(f"  Skipped (no meta.json): {skipped_no_meta}")
    print(f"\n  Phase 1 done: {fixed_count} docs flagged for recovery")

if __name__ == "__main__":
    main()
