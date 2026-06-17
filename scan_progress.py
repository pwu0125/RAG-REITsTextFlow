#!/usr/bin/env python3
"""Scan meta.json for target batches using MANIFEST (curated docs only), output human + JSON."""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(SCRIPT_DIR, "announcement_document_processing_local", "processed_files_local.json")
BATCH_CONFIG_PATH = os.path.join(SCRIPT_DIR, "BATCH_CONFIG.json")

STEPS = [
    ("vec", "table_detection_vector_done"),
    ("scan", "table_detection_scan_done"),
    ("describe", "table_describe_done"),
    ("not_table", "not_table_describe_done"),
    ("merge", "merge_done"),
    ("segmentation", "text_segmentation"),
    ("embedding", "embedding_done"),
    ("es", "elasticsearch_database_done"),
]


def load_json(path, label):
    if not os.path.exists(path):
        print(f"ERROR: {label} not found at {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    config = load_json(BATCH_CONFIG_PATH, "Batch config")
    manifest = load_json(MANIFEST_PATH, "Manifest")

    lines = []
    json_output = {}

    for batch_name, batch_info in config.items():
        codes = set(batch_info["codes"])
        metas = []

        for fname in manifest["files"]:
            code = fname.split("-")[0]
            if code not in codes:
                continue
            dirname = fname.replace(".pdf", "")
            meta_path = os.path.join(SCRIPT_DIR, "announcement_document_processing_local",
                                     code, dirname, "meta.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        metas.append(json.load(f))
                except (json.JSONDecodeError, IOError) as e:
                    print(f"WARNING: failed to read {meta_path}: {e}", file=sys.stderr)

        total = len(metas)
        if total == 0:
            lines.append(f"{batch_name}: 0 docs — no manifest matches")
            lines.append("")
            json_output[batch_name] = {"total": 0}
            continue

        lines.append(f"{batch_name}: {total} docs")
        json_batch = {"total": total}

        for label, key in STEPS:
            done = sum(1 for m in metas if m.get(key))
            pct = (done / total * 100) if total > 0 else 0
            mark = " ✅" if done == total and total > 0 else ""
            lines.append(f"   {label}: {done}/{total} ({pct:.1f}%){mark}")
            json_batch[label] = {"done": done, "total": total, "pct": round(pct, 1)}

        lines.append("")
        json_output[batch_name] = json_batch

    for line in lines:
        print(line)

    print("--- JSON ---")
    json.dump(json_output, sys.stdout, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
