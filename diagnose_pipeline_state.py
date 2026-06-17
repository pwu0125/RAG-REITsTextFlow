#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Diagnostic script: scan ALL docs, compare meta.json flags vs actual disk artifacts.
Read-only — no writes to disk.
"""

import os, json, sys

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

def safe_read(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def check_artifact_merge(pdf_folder):
    """merge_done: text.json exists and is > 1KB (GATE1 threshold)"""
    text_path = os.path.join(pdf_folder, "text.json")
    if not os.path.exists(text_path):
        return False
    size = os.path.getsize(text_path)
    return size > 1024

def check_artifact_segmentation(pdf_folder):
    """text_segmentation: text_segmentation.json exists and is a non-empty list"""
    seg_path = os.path.join(pdf_folder, "text_segmentation.json")
    data = safe_read(seg_path)
    if data is None:
        return False
    return isinstance(data, list) and len(data) > 0

def check_artifact_embedding(pdf_folder):
    """embedding_done: text_segmentation_embedding.json exists and has embeddings"""
    emb_path = os.path.join(pdf_folder, "text_segmentation_embedding.json")
    data = safe_read(emb_path)
    if data is None:
        return False
    if not isinstance(data, list) or len(data) == 0:
        return False
    # check at least first chunk has embedding vector
    first = data[0]
    if isinstance(first, dict) and "embedding" in first:
        emb = first["embedding"]
        if isinstance(emb, list) and len(emb) > 0 and isinstance(emb[0], (int, float)):
            return True
    return False

# Step order for "last completed step" determination
STEPS = [
    ("merge_done",           "merge",              check_artifact_merge),
    ("text_segmentation",    "segmentation",       check_artifact_segmentation),
    ("embedding_done",       "embedding",          check_artifact_embedding),
    # vector_database_done and elasticsearch_database_done have no local artifacts to verify
]

def infer_last_step_on_disk(checks):
    """Return the last step whose artifact exists on disk (or None)"""
    last = None
    for flag, name, _ in STEPS:
        if checks[name]:
            last = name
    return last

def main():
    manifest = safe_read(MANIFEST_FILE)
    if not manifest:
        print("ERROR: Cannot read manifest")
        sys.exit(1)

    files_map = manifest.get("files", {})
    total_in_manifest = len(files_map)
    print(f"Manifest has {total_in_manifest} entries")

    # Counters for each step: (flag=T/disk=T, flag=T/disk=F, flag=F/disk=T, flag=F/disk=F)
    counts = {}
    for flag, name, _ in STEPS:
        counts[name] = {"flag_T_disk_T": 0, "flag_T_disk_F": 0, "flag_F_disk_T": 0, "flag_F_disk_F": 0}

    # Track docs by "actual last step on disk"
    last_step_groups = {"none": 0, "merge": 0, "segmentation": 0, "embedding": 0}
    # Docs with flag=T but artifact=F (broken):
    broken_docs = {name: [] for _, name, _ in STEPS}
    # Docs with flag=F but artifact=T (orphan artifact):
    orphan_docs = {name: [] for _, name, _ in STEPS}

    missing_meta = 0
    missing_folder = 0
    scanned = 0

    for file_name, info in files_map.items():
        if not isinstance(info, dict):
            continue
        fund_code = info.get("fund_code", "")
        if not fund_code:
            continue

        # Manifest keys have .pdf suffix, actual dirs don't
        folder_name = file_name
        if folder_name.endswith(".pdf"):
            folder_name = folder_name[:-4]

        pdf_folder = os.path.join(OUTPUT_DIR, fund_code, folder_name)
        if not os.path.isdir(pdf_folder):
            missing_folder += 1
            continue

        scanned += 1

        # Read meta.json flags
        meta = safe_read(os.path.join(pdf_folder, "meta.json"))
        if meta is None:
            missing_meta += 1
            meta = {}

        # Check disk artifacts
        checks = {}
        for flag, name, checker in STEPS:
            checks[name] = checker(pdf_folder)

        # Count flag vs disk
        for flag, name, checker in STEPS:
            flag_val = meta.get(flag, False) is True
            disk_val = checks[name]

            if flag_val and disk_val:
                counts[name]["flag_T_disk_T"] += 1
            elif flag_val and not disk_val:
                counts[name]["flag_T_disk_F"] += 1
                broken_docs[name].append(file_name)
            elif not flag_val and disk_val:
                counts[name]["flag_F_disk_T"] += 1
                orphan_docs[name].append(file_name)
            else:
                counts[name]["flag_F_disk_F"] += 1

        # Determine actual last step on disk
        last = infer_last_step_on_disk(checks)
        if last is None:
            last_step_groups["none"] += 1
        else:
            last_step_groups[last] += 1

    # --- Print report ---
    print(f"\n{'='*80}")
    print(f"SCAN RESULTS: {scanned} docs scanned")
    print(f"  Missing folders: {missing_folder}")
    print(f"  Missing meta.json: {missing_meta}")
    print(f"{'='*80}")

    print(f"\n{'='*80}")
    print("PER-STEP: flag vs disk artifact comparison")
    print(f"{'='*80}")

    for flag, name, checker in STEPS:
        c = counts[name]
        print(f"\n--- {name} (flag: {flag}) ---")
        print(f"  flag=T  disk=T : {c['flag_T_disk_T']:>5}  (OK)")
        print(f"  flag=T  disk=F : {c['flag_T_disk_F']:>5}  ⚠️  BROKEN — flag set but artifact missing!")
        print(f"  flag=F  disk=T : {c['flag_F_disk_T']:>5}  ⚠️  ORPHAN — artifact exists but flag not set")
        print(f"  flag=F  disk=F : {c['flag_F_disk_F']:>5}  (pending)")

    print(f"\n{'='*80}")
    print("DOCS GROUPED BY ACTUAL LAST STEP COMPLETED ON DISK")
    print(f"{'='*80}")
    for step in ["none", "merge", "segmentation", "embedding"]:
        count = last_step_groups[step]
        label = step if step != "none" else "pre-merge (no merge artifact)"
        print(f"  {label:>35}: {count:>5}")

    # --- Repair plan ---
    print(f"\n{'='*80}")
    print("REPAIR PLAN: Minimum pipeline steps to fix")
    print(f"{'='*80}")

    broken_merge_count = len(broken_docs["merge"])
    broken_seg_count = len(broken_docs["segmentation"])
    broken_emb_count = len(broken_docs["embedding"])

    orphan_merge_count = len(orphan_docs["merge"])
    orphan_seg_count = len(orphan_docs["segmentation"])
    orphan_emb_count = len(orphan_docs["embedding"])

    # Docs that need merge re-run
    need_merge = set(broken_docs["merge"])
    # Docs that need segmentation re-run (including those that need merge first)
    need_seg = set(broken_docs["segmentation"]) | need_merge
    # Docs that need embedding re-run (including those that need prior steps)
    need_emb = set(broken_docs["embedding"]) | need_seg

    # Docs with broken merge need: step5 first
    if broken_merge_count > 0:
        print(f"\n1. Fix merge flags → then re-run step5 for {broken_merge_count} docs")
        print(f"   These docs have merge_done=True but text.json is missing or <1KB")
        print(f"   → Reset merge_done=False in meta.json → rebuild_manifest.py → re-run step5")
        if broken_merge_count <= 30:
            print(f"   Docs: {sorted(broken_docs['merge'])}")

    # Docs with broken seg (merge OK but no segmentation file)
    pure_seg = set(broken_docs["segmentation"]) - need_merge
    if pure_seg:
        print(f"\n2. Fix segmentation flags → then re-run step6 for {len(pure_seg)} docs")
        print(f"   These docs have text_segmentation=True but text_segmentation.json missing")
        print(f"   → Reset text_segmentation=False in meta.json → rebuild_manifest.py → re-run step6")
        if len(pure_seg) <= 30:
            print(f"   Docs: {sorted(pure_seg)}")

    # Docs with broken embedding (seg OK but no embedding file)
    pure_emb = set(broken_docs["embedding"]) - need_merge - pure_seg
    if pure_emb:
        print(f"\n3. Fix embedding flags → then re-run step7 for {len(pure_emb)} docs")
        print(f"   These docs have embedding_done=True but text_segmentation_embedding.json missing")
        print(f"   → Reset embedding_done=False in meta.json → rebuild_manifest.py → re-run step7")
        if len(pure_emb) <= 30:
            print(f"   Docs: {sorted(pure_emb)}")

    # Orphan artifacts
    if orphan_merge_count > 0 or orphan_seg_count > 0 or orphan_emb_count > 0:
        print(f"\n4. Orphan artifacts (disk exists but flag=False) — set flag=True:")
        if orphan_merge_count > 0:
            print(f"   merge: {orphan_merge_count} docs → set merge_done=True in meta.json")
        if orphan_seg_count > 0:
            print(f"   segmentation: {orphan_seg_count} docs → set text_segmentation=True in meta.json")
        if orphan_emb_count > 0:
            print(f"   embedding: {orphan_emb_count} docs → set embedding_done=True in meta.json")

    total_broken = (
        broken_merge_count + broken_seg_count + broken_emb_count
        - len(need_merge & set(broken_docs["segmentation"]))  # double-counted
        - len(need_seg & set(broken_docs["embedding"]))       # double-counted
    )
    total_broken = len(need_emb)  # all unique docs that need at least one fix

    print(f"\n{'='*80}")
    print(f"SUMMARY: {total_broken} docs need flag repair + re-run")
    print(f"  {broken_merge_count} need merge (step5)")
    print(f"  {broken_seg_count} need segmentation (step6)")
    print(f"  {broken_emb_count} need embedding (step7)")
    print(f"  Repair order: step5 → step6 → step7 → step8_1 → step8_2")
    print(f"{'='*80}")

    # Write detailed broken docs list to file
    detail_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnosis_detail.json")
    detail = {
        "broken": {name: docs for name, docs in broken_docs.items()},
        "orphan": {name: docs for name, docs in orphan_docs.items()},
        "last_step_groups": last_step_groups,
        "counts": {name: c for name, c in counts.items()},
    }
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    print(f"\nDetailed results written to: {detail_path}")

if __name__ == "__main__":
    main()
