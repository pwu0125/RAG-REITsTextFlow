#!/usr/bin/env python3
"""Batch-scoped step executor for the REITs Text Data Pipeline.

Usage:
    python run_step.py <batch_name> <step_script.py>
    python run_step.py --list

Example:
    python run_step.py B1a step4_1_1_describe_table_images_multi_thread.py

The executor:
1. Reads BATCH_CONFIG.json to get fund codes for the batch
2. Backs up the master manifest
3. Filters the manifest to only include docs matching batch codes
4. Runs the step script (which reads the filtered manifest)
5. Restores the full manifest from backup
6. Prints a summary
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)  # Ensure CWD is correct even when invoked via absolute path from elsewhere
PYTHON_BIN = "/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python"
BATCH_CONFIG_PATH = SCRIPT_DIR / "BATCH_CONFIG.json"
MANIFEST_DIR = SCRIPT_DIR / "announcement_document_processing_local"
MANIFEST_PATH = MANIFEST_DIR / "processed_files_local.json"
# 每个 run_step 实例使用唯一备份文件名，防止并发踩踏
def _make_backup_path(batch_name, step_script):
    """生成唯一备份路径：{batch}_{step}_{pid}.json"""
    safe_step = step_script.replace(".py", "").replace("/", "_")
    return MANIFEST_DIR / f"_backup_{batch_name}_{safe_step}_{os.getpid()}.json"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_batches():
    config = load_json(BATCH_CONFIG_PATH)
    print("Available batches:")
    for name, info in config.items():
        codes = ", ".join(info["codes"])
        print(f"  {name}: {info['docs']} docs, codes: [{codes}]")


def run_step(batch_name, step_script, extra_args=None):
    # Load batch config
    if not BATCH_CONFIG_PATH.exists():
        print(f"ERROR: BATCH_CONFIG.json not found at {BATCH_CONFIG_PATH}")
        sys.exit(1)

    config = load_json(BATCH_CONFIG_PATH)
    if batch_name not in config:
        print(f"ERROR: Unknown batch '{batch_name}'. Available: {list(config.keys())}")
        sys.exit(1)

    batch = config[batch_name]
    batch_codes = set(batch["codes"])
    step_path = SCRIPT_DIR / step_script

    if not step_path.exists():
        print(f"ERROR: Step script not found: {step_path}")
        sys.exit(1)

    if not MANIFEST_PATH.exists():
        print(f"ERROR: Manifest not found at {MANIFEST_PATH}")
        sys.exit(1)

    # Backup manifest (unique per invocation to prevent concurrent stomping)
    backup_path = _make_backup_path(batch_name, step_script)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Backing up manifest to {backup_path.name}")
    shutil.copy2(MANIFEST_PATH, backup_path)

    # Filter manifest (structure: {"files": {...}})
    manifest = load_json(MANIFEST_PATH)
    all_docs = len(manifest["files"])
    filtered = {k: v for k, v in manifest["files"].items() if v.get("fund_code") in batch_codes}
    filtered_docs = len(filtered)
    skipped_docs = all_docs - filtered_docs

    if filtered_docs == 0:
        print("ERROR: No documents in manifest match batch codes. Nothing to process.")
        os.remove(backup_path)
        sys.exit(1)

    save_json(MANIFEST_PATH, {"files": filtered})
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Manifest filtered: {filtered_docs} docs "
          f"(skipped {skipped_docs}), batch codes: {batch['codes']}")

    # Run step
    cmd = [PYTHON_BIN, str(step_path)]
    if extra_args:
        cmd.extend(extra_args)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Running: {' '.join(cmd)}")
    print("-" * 60)
    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    print("-" * 60)

    # Restore manifest
    if not backup_path.exists():
        print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠️ Backup missing ({backup_path.name}), falling back to rebuild_manifest.py")
        subprocess.run([PYTHON_BIN, str(SCRIPT_DIR / "rebuild_manifest.py")], cwd=str(SCRIPT_DIR))
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Restored manifest via rebuild (backup was lost)")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Restoring manifest from {backup_path.name}")
        shutil.move(str(backup_path), str(MANIFEST_PATH))

    # [FlagSync B] 从 meta.json 同步标志回 manifest
    manifest = load_json(MANIFEST_PATH)
    reconciled = 0
    FLAGS_TO_SYNC = [
        "text_extracted", "table_detection_vector_done",
        "table_detection_scan_done", "table_describe_done",
        "not_table_describe_done", "merge_done",
        "text_segmentation", "embedding_done",
        "vector_database_done", "elasticsearch_database_done",
    ]
    for file_name, info in manifest["files"].items():
        fund_code = (info or {}).get("fund_code", "")
        pdf_folder = os.path.splitext(file_name)[0]
        meta_path = os.path.join(str(MANIFEST_DIR), fund_code, pdf_folder, "meta.json")
        if not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        for flag in FLAGS_TO_SYNC:
            if meta.get(flag) and not info.get(flag):
                info[flag] = True
                reconciled += 1
    if reconciled:
        save_json(MANIFEST_PATH, manifest)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [FlagSync] {reconciled} flags synced from meta.json → manifest")

    # Summary
    rc = result.returncode
    status = "SUCCESS" if rc == 0 else f"FAILED (exit code {rc})"
    print(f"\n  Batch:    {batch_name}")
    print(f"  Codes:    {batch['codes']}")
    print(f"  Docs:     {filtered_docs} (expected {batch['docs']})")
    print(f"  Script:   {step_script}")
    print(f"  Result:   {status}")

    sys.exit(rc)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        list_batches()
    elif len(sys.argv) == 2 and sys.argv[1] == "--help":
        print(__doc__)
    elif len(sys.argv) >= 3:
        extra = sys.argv[3:] if len(sys.argv) > 3 else None
        run_step(sys.argv[1], sys.argv[2], extra)
    else:
        print("Usage: python run_step.py <batch_name> <step_script.py> [extra_args...]")
        print("       python run_step.py --list")
        print("       python run_step.py --help")
        sys.exit(1)
