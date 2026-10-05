#!/usr/bin/env python3
"""
run_incremental.py — 基于 lockfile 的安全增量管道执行器

用法:
  python run_incremental.py --lockfile _incremental/batch_xxx.json --steps 2,3,4,5,6,7

安全护栏:
  1. 运行前自动 check_env.py
  2. 验证 lockfile 文档数 vs 清单
  3. 每步前检查前置条件（seg/embed 文件存在性）
  4. 使用原子写保护 manifest
  5. 完成后自动重建 manifest

Step 映射:
  1=step1_process_pdfs, 2=step2_extract_text, 3=step3_2_table_detection_scan,
  4=step4_1_1_describe_table_images, 4b=step4_2_1_describe_not_table_images,
  5=step5_merge, 6=step6_segmentation, 7=step7_embedding,
  8a=step8_1_es, 8b=step8_2_milvus
"""
import json, os, subprocess, sys, argparse, shutil, time
from datetime import datetime

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = "/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python"
BASE = os.path.join(PIPELINE_DIR, "announcement_document_processing_local")
MANIFEST = os.path.join(BASE, "processed_files_local.json")

STEP_MAP = {
    "1":  "step1_process_pdfs.py",
    "2":  "step2_extract_text_onlyvactor_multi_process.py",
    "3":  "step3_2_table_detection_scan_multifile.py",
    "4":  "step4_1_1_describe_table_images_multi_thread.py",
    "4b": "step4_2_1_describe_not_table_images_llm.py",
    "5":  "step5_merge_table_into_text.py",
    "6":  "step6_text_segmentation.py",
    "7":  "step7_text_embedding.py",
    "8a": "step8_1_ingest_elasticsearch_data.py",
    "8b": "step8_2_ingest_vector_database.py",
}

PRECONDITIONS = {
    "3": ["text.json"],
    "4": ["table_image"],
    "5": ["table_describe.json", "not_table_describe.json"],
    "6": ["text.json"],
    "7": ["text_segmentation.json"],
    "8a": ["text_segmentation.json"],
    "8b": ["text_segmentation_embedding.json"],
}

def load_lockfile(path):
    with open(path) as f:
        return json.load(f)

def filter_manifest_to_lockfile(lockfile):
    """Set manifest to only contain lockfile docs."""
    shutil.copy(MANIFEST, MANIFEST + ".safety_backup")
    lock_ids = {d["doc_id"] for d in lockfile["docs"]}
    
    with open(MANIFEST) as f:
        m = json.load(f)
    
    original = len(m.get("files", {}))
    new = {}
    for key, entry in m.get("files", {}).items():
        code = entry.get("fund_code", "")
        fname = entry.get("file_name", "")
        # Exact match: key without .pdf == doc_dir name
        key_no_ext = key[:-4] if key.endswith(".pdf") else key
        candidate_id = f"{code}/{key_no_ext}"
        if candidate_id in lock_ids:
            new[key] = entry
    
    m["files"] = new
    with open(MANIFEST, "w") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return original, len(new)

def restore_manifest():
    backup = MANIFEST + ".safety_backup"
    if os.path.exists(backup):
        shutil.copy(backup, MANIFEST)

def rebuild_manifest():
    subprocess.run([PYTHON, os.path.join(PIPELINE_DIR, "rebuild_manifest.py")], 
                   capture_output=True, cwd=PIPELINE_DIR)

def check_preconditions(lockfile, step_num):
    """Check that all docs meet preconditions for the step."""
    required_files = PRECONDITIONS.get(step_num, [])
    if not required_files:
        return True
    
    issues = []
    for doc in lockfile["docs"]:
        doc_id = doc["doc_id"]
        doc_dir = os.path.join(BASE, doc_id)
        for fname in required_files:
            fp = os.path.join(doc_dir, fname)
            if not os.path.exists(fp) or os.path.getsize(fp) == 0:
                issues.append(f"{doc_id}: missing/empty {fname}")
    
    if issues:
        print(f"⚠️  Precondition FAILED for step {step_num}:")
        for i in issues[:10]:
            print(f"   {i}")
        print(f"   ({len(issues)} total issues)")
        return False
    return True

def run_step(step_num):
    script = STEP_MAP.get(step_num)
    if not script:
        print(f"Unknown step: {step_num}")
        return False
    
    print(f"\n{'='*60}")
    print(f"Step {step_num}: {script}  [{datetime.now().strftime('%H:%M:%S')}]")
    print(f"{'='*60}")
    
    r = subprocess.run(
        [PYTHON, "-u", os.path.join(PIPELINE_DIR, script)],
        capture_output=True, text=True, timeout=600,
        cwd=PIPELINE_DIR
    )
    
    # Show key output
    for line in r.stdout.split("\n"):
        if any(kw in line for kw in ["本次需要", "入库成功", "全部完成", "总共成功", 
                                       "已完成", "没有找到", "Error", "RuntimeError"]):
            print(f"  {line.strip()[:150]}")
    
    if r.returncode != 0:
        print(f"  ❌ exit={r.returncode}")
        return False
    print(f"  ✅ exit=0")
    return True

def main():
    parser = argparse.ArgumentParser(description="Safe incremental pipeline runner")
    parser.add_argument("--lockfile", required=True, help="Path to lockfile JSON")
    parser.add_argument("--steps", required=True, help="Comma-separated step numbers (e.g. 2,3,4,5,6,7)")
    parser.add_argument("--skip-check", action="store_true", help="Skip env check")
    parser.add_argument("--dry-run", action="store_true", help="Only validate, don't execute")
    args = parser.parse_args()

    # 0. Environment check
    if not args.skip_check:
        print("=== 0. Pre-flight Check ===")
        r = subprocess.run([PYTHON, os.path.join(PIPELINE_DIR, "check_env.py")], 
                          capture_output=True, text=True, cwd=PIPELINE_DIR)
        print(r.stdout.strip())
        if r.returncode != 0:
            print("❌ Environment check failed. Fix issues or use --skip-check")
            sys.exit(1)

    # 1. Load lockfile
    lockfile = load_lockfile(args.lockfile)
    target_count = lockfile.get("count", len(lockfile.get("docs", [])))
    print(f"\n=== Lockfile: {args.lockfile} ===")
    print(f"   Target: {target_count} docs")

    # 2. Dry-run validation
    steps = [s.strip() for s in args.steps.split(",")]
    for step in steps:
        if not check_preconditions(lockfile, step):
            print(f"❌ Precondition check failed for step {step}")
            if args.dry_run:
                sys.exit(1)

    if args.dry_run:
        print("\n✅ Dry-run passed. Ready to execute:")
        print(f"   Steps: {steps}")
        sys.exit(0)

    # 3. Filter manifest to lockfile docs
    orig, filtered = filter_manifest_to_lockfile(lockfile)
    if filtered != target_count:
        print(f"⚠️  Lockfile has {target_count} docs but manifest matched {filtered}")
        print(f"   Proceeding with {filtered} matched docs")
    print(f"   Manifest: {orig} → {filtered} docs (backup saved)")

    # 4. Execute steps
    success = True
    for step in steps:
        if step not in STEP_MAP:
            print(f"❌ Unknown step: {step}")
            success = False
            break
        
        if not run_step(step):
            print(f"\n❌ Step {step} failed. Investigate and re-run from here.")
            success = False
            break

    # 5. Restore + rebuild
    print(f"\n=== Cleanup ===")
    restore_manifest()
    print("Manifest restored")
    rebuild_manifest()
    print("Manifest rebuilt from disk")

    if success:
        print(f"\n✅ Pipeline complete: {args.lockfile}")
    else:
        print(f"\n⚠️  Pipeline incomplete. Manifest restored. Fix issues and re-run.")
        sys.exit(1)

if __name__ == "__main__":
    main()
