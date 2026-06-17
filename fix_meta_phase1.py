#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 1: Fix meta.json AND text.json metadata flags for all 9 completed batches.
For docs that have text_segmentation_embedding.json on disk, set:
  text_segmentation=true, embedding_done=true,
  elasticsearch_database_done=false, vector_database_done=false

CRITICAL: Both meta.json and text.json metadata must be updated because
_infer_status_from_files() merges them (meta first, then text.json metadata
overwrites). If text.json still has old True flags from a prior run, the
merge result will be wrong and step8 scripts will skip the doc.
"""
import json, os, sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BATCH_CONFIG_PATH = SCRIPT_DIR / "BATCH_CONFIG.json"
OUTPUT_DIR = SCRIPT_DIR / "announcement_document_processing_local"

# 9 completed batches
COMPLETED_BATCHES = ["B1a","B1b","B1c","B2a","B2b","B2c","B2d","B2e","B2f"]

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# Get all fund codes from completed batches
config = load_json(BATCH_CONFIG_PATH)
codes = set()
for batch_name in COMPLETED_BATCHES:
    if batch_name in config:
        codes.update(config[batch_name]["codes"])

print(f"Completed batches: {COMPLETED_BATCHES}")
print(f"Unique fund codes: {sorted(codes)}")
print(f"Total codes: {len(codes)}")

updated = 0
skipped_no_emb = 0
skipped_no_meta = 0
errors = 0

for code in sorted(codes):
    code_dir = OUTPUT_DIR / code
    if not code_dir.is_dir():
        print(f"  WARN: fund_code dir not found: {code_dir}")
        continue

    for doc_folder in code_dir.iterdir():
        if not doc_folder.is_dir():
            continue
        seg_path = doc_folder / "text_segmentation_embedding.json"
        if not seg_path.exists():
            skipped_no_emb += 1
            continue

        meta_path = doc_folder / "meta.json"
        if not meta_path.exists():
            skipped_no_meta += 1
            continue

        try:
            # Fix meta.json
            meta = load_json(meta_path)
            meta["text_segmentation"] = True
            meta["embedding_done"] = True
            meta["elasticsearch_database_done"] = False
            meta["vector_database_done"] = False
            save_json(meta_path, meta)

            # Fix text.json metadata (which overrides meta.json in _infer_status_from_files)
            text_path = doc_folder / "text.json"
            if text_path.exists():
                text_json = load_json(text_path)
                if isinstance(text_json, dict) and "metadata" in text_json:
                    text_meta = text_json["metadata"]
                    if isinstance(text_meta, dict):
                        text_meta["text_segmentation"] = True
                        text_meta["embedding_done"] = True
                        text_meta["elasticsearch_database_done"] = False
                        text_meta["vector_database_done"] = False
                        save_json(text_path, text_json)

            updated += 1
        except Exception as e:
            print(f"  ERROR updating {meta_path}: {e}")
            errors += 1

print(f"\nResults:")
print(f"  Updated:   {updated}")
print(f"  Skipped (no embedding file): {skipped_no_emb}")
print(f"  Skipped (no meta.json):      {skipped_no_meta}")
print(f"  Errors:    {errors}")
