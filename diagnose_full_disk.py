#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Full disk scan: compare ALL docs on disk vs manifest.
Counts directories, finds orphan docs (on disk but NOT in manifest),
and analyzes meta.json flags vs artifacts for ALL docs on disk.
"""

import os, json, sys
from collections import defaultdict

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

def safe_read(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return None

def check_artifact_merge(pdf_folder):
    text_path = os.path.join(pdf_folder, "text.json")
    if not os.path.exists(text_path):
        return False
    return os.path.getsize(text_path) > 1024

def check_artifact_segmentation(pdf_folder):
    seg_path = os.path.join(pdf_folder, "text_segmentation.json")
    data = safe_read(seg_path)
    if data is None:
        return False
    return isinstance(data, list) and len(data) > 0

def check_artifact_embedding(pdf_folder):
    emb_path = os.path.join(pdf_folder, "text_segmentation_embedding.json")
    data = safe_read(emb_path)
    if data is None:
        return False
    if not isinstance(data, list) or len(data) == 0:
        return False
    first = data[0]
    if isinstance(first, dict) and "embedding" in first:
        emb = first["embedding"]
        if isinstance(emb, list) and len(emb) > 0 and isinstance(emb[0], (int, float)):
            return True
    return False

STEPS = [
    ("merge_done",           "merge",              check_artifact_merge),
    ("text_segmentation",    "segmentation",       check_artifact_segmentation),
    ("embedding_done",       "embedding",          check_artifact_embedding),
]

def main():
    # Build manifest lookup (strip .pdf)
    manifest = safe_read(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {})
    manifest_set = set()
    for k in files_map:
        if k.endswith(".pdf"):
            k = k[:-4]
        manifest_set.add(k)

    print(f"Manifest has {len(files_map)} entries")

    # Scan ALL directories on disk
    all_disk_docs = []  # (fund_code, folder_name, full_path)
    total_dirs = 0
    for fund_code in sorted(os.listdir(OUTPUT_DIR)):
        fc_path = os.path.join(OUTPUT_DIR, fund_code)
        if not os.path.isdir(fc_path):
            continue
        if fund_code.startswith(".") or fund_code in ("processed_files_local.json", "manifest_backups"):
            continue
        for folder_name in sorted(os.listdir(fc_path)):
            full_path = os.path.join(fc_path, folder_name)
            if not os.path.isdir(full_path):
                continue
            total_dirs += 1
            all_disk_docs.append((fund_code, folder_name, full_path))

    print(f"Total doc directories on disk: {total_dirs}")

    # Find docs on disk but NOT in manifest
    orphan_on_disk = []
    for fund_code, folder_name, full_path in all_disk_docs:
        if folder_name not in manifest_set:
            orphan_on_disk.append((fund_code, folder_name, full_path))

    print(f"Docs on disk NOT in manifest: {len(orphan_on_disk)}")

    # Find docs in manifest but NOT on disk
    missing_from_disk = []
    for manifest_key in files_map:
        k = manifest_key[:-4] if manifest_key.endswith(".pdf") else manifest_key
        found = False
        for _, folder_name, _ in all_disk_docs:
            if folder_name == k:
                found = True
                break
        if not found:
            # try with fund_code
            info = files_map.get(manifest_key, {})
            fc = info.get("fund_code", "")
            folder_path = os.path.join(OUTPUT_DIR, fc, k)
            if not os.path.isdir(folder_path):
                missing_from_disk.append((fc, k))

    print(f"Docs in manifest but NOT on disk: {len(missing_from_disk)}")
    if missing_from_disk and len(missing_from_disk) <= 30:
        for fc, name in missing_from_disk:
            print(f"  MISSING: {fc}/{name}")

    # Now scan ALL docs on disk (including orphans) for flag vs artifact
    counts = {}
    for _, name, _ in STEPS:
        counts[name] = {"flag_T_disk_T": 0, "flag_T_disk_F": 0, "flag_F_disk_T": 0, "flag_F_disk_F": 0}

    broken_docs = {name: [] for _, name, _ in STEPS}
    orphan_artifact_docs = {name: [] for _, name, _ in STEPS}
    last_step_groups = defaultdict(int)

    missing_meta = 0

    for fund_code, folder_name, full_path in all_disk_docs:
        meta = safe_read(os.path.join(full_path, "meta.json"))
        if meta is None:
            missing_meta += 1
            meta = {}

        checks = {}
        for flag, name, checker in STEPS:
            checks[name] = checker(full_path)

        for flag, name, checker in STEPS:
            flag_val = meta.get(flag, False) is True
            disk_val = checks[name]

            if flag_val and disk_val:
                counts[name]["flag_T_disk_T"] += 1
            elif flag_val and not disk_val:
                counts[name]["flag_T_disk_F"] += 1
                broken_docs[name].append(f"{fund_code}/{folder_name}")
            elif not flag_val and disk_val:
                counts[name]["flag_F_disk_T"] += 1
                orphan_artifact_docs[name].append(f"{fund_code}/{folder_name}")
            else:
                counts[name]["flag_F_disk_F"] += 1

        # Last step on disk
        last = None
        for _, name, _ in STEPS:
            if checks[name]:
                last = name
        last_step_groups[last or "none"] += 1

    # --- Report ---
    print(f"\n{'='*80}")
    print(f"FLAG vs DISK ARTIFACT — ALL {total_dirs} docs on disk")
    print(f"  Missing meta.json: {missing_meta}")
    print(f"{'='*80}")

    for flag, name, checker in STEPS:
        c = counts[name]
        print(f"\n--- {name} (flag: {flag}) ---")
        print(f"  flag=T  disk=T : {c['flag_T_disk_T']:>5}  (OK)")
        print(f"  flag=T  disk=F : {c['flag_T_disk_F']:>5}  BROKEN")
        print(f"  flag=F  disk=T : {c['flag_F_disk_T']:>5}  ORPHAN artifact (flag missing)")
        print(f"  flag=F  disk=F : {c['flag_F_disk_F']:>5}  (pending)")

    print(f"\n{'='*80}")
    print(f"ACTUAL LAST STEP ON DISK (all {total_dirs} docs)")
    print(f"{'='*80}")
    for step in ["none", "merge", "segmentation", "embedding"]:
        count = last_step_groups[step]
        label = {"none": "pre-merge", "merge": "merge only", "segmentation": "segmentation", "embedding": "embedding"}[step]
        print(f"  {label:>20}: {count:>5}")

    # Broken detail
    total_broken = set()
    for name, docs in broken_docs.items():
        total_broken.update(docs)
    print(f"\n{'='*80}")
    print(f"BROKEN DOCS: {len(total_broken)} unique docs with flag=True but artifact missing")
    print(f"{'='*80}")

    for name, docs in broken_docs.items():
        if docs:
            print(f"\n  {name} broken ({len(docs)} docs):")
            if len(docs) <= 50:
                for d in sorted(docs):
                    print(f"    {d}")

    # Orphan detail
    total_orphan = set()
    for name, docs in orphan_artifact_docs.items():
        total_orphan.update(docs)
    print(f"\n{'='*80}")
    print(f"ORPHAN ARTIFACTS: {len(total_orphan)} unique docs with artifact but flag=False")
    print(f"{'='*80}")
    for name, docs in orphan_artifact_docs.items():
        if docs:
            print(f"\n  {name} orphan ({len(docs)} docs):")
            if len(docs) <= 30:
                for d in sorted(docs):
                    print(f"    {d}")

    # Orphans not in manifest
    if orphan_on_disk:
        print(f"\n{'='*80}")
        print(f"DOCS ON DISK NOT IN MANIFEST: {len(orphan_on_disk)}")
        print(f"{'='*80}")
        if len(orphan_on_disk) <= 50:
            for fc, name, path in sorted(orphan_on_disk):
                print(f"  {fc}/{name}")

    # Step-by-step repair plan
    print(f"\n{'='*80}")
    print("REPAIR PLAN")
    print(f"{'='*80}")

    need_merge = set(broken_docs["merge"])
    need_seg = set(broken_docs["segmentation"]) | need_merge
    need_emb = set(broken_docs["embedding"]) | need_seg

    print(f"\n1. Add {len(orphan_on_disk)} orphan docs to manifest → rebuild_manifest.py")
    print(f"2. Fix meta.json flags for {len(total_broken)} broken docs (reset flag=False)")
    print(f"3. Fix meta.json flags for {len(total_orphan)} orphan-artifact docs (set flag=True)")
    print(f"4. Re-run pipeline steps in order:")
    print(f"   step5 (merge): {len(need_merge)} docs")
    pure_seg = need_seg - need_merge
    print(f"   step6 (segmentation): {len(pure_seg)} docs")
    pure_emb = need_emb - need_seg
    print(f"   step7 (embedding): {len(pure_emb)} docs")
    print(f"   step8_1 (ES): all docs missing it")
    print(f"   step8_2 (VectorDB): all docs missing it")

    # Write detail file
    detail_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagnosis_full_detail.json")
    detail = {
        "total_disk_dirs": total_dirs,
        "manifest_entries": len(files_map),
        "orphan_on_disk_not_in_manifest": len(orphan_on_disk),
        "missing_from_disk_in_manifest": len(missing_from_disk),
        "counts": {name: dict(c) for name, c in counts.items()},
        "broken": {name: sorted(docs) for name, docs in broken_docs.items()},
        "orphan_artifacts": {name: sorted(docs) for name, docs in orphan_artifact_docs.items()},
        "last_step_groups": dict(last_step_groups),
        "orphan_on_disk_list": [f"{fc}/{n}" for fc, n, _ in sorted(orphan_on_disk)],
        "missing_from_disk_list": [f"{fc}/{n}" for fc, n in missing_from_disk],
    }
    with open(detail_path, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
    print(f"\nDetail: {detail_path}")

if __name__ == "__main__":
    main()
