#!/usr/bin/env python3
"""
Diagnostic script v2: Compare meta.json flags vs actual disk artifacts for ALL documents.
Uses REAL pipeline artifact knowledge:
  - Step 1: temp_pdf_images/ (deleted after GATE1 merge pass)
  - Step 2: text.json (overwritten by step5 merge)
  - Step 3: table_image/ (deleted after GATE1 merge pass)
  - Step 4.1: table_describe.json
  - Step 4.2: not_table_describe.json (may be legitimately absent for pure-text PDFs)
  - Step 5 (merge): overwrites text.json with merged content + table descriptions.
      GATE1 deletes temp_pdf_images/ and table_image/ on pass.
  - Step 6: text_segmentation.json
  - Step 7: text_segmentation_embedding.json (embedding vectors inlined in chunks)
  - Step 8.1: ES ingest (local flag only)
  - Step 8.2: Milvus ingest (local flag only)
"""
import json
import os
import sys
from collections import defaultdict, Counter
from pathlib import Path

OUTPUT_DIR = Path("/Users/pyemini/REITs/REITs_Text_data_pipeline/RAG-REITsTextFlow/announcement_document_processing_local")

def check_file(path, min_bytes=100):
    """Check if file exists and is larger than min_bytes."""
    if not path.is_file():
        return False
    try:
        return path.stat().st_size >= min_bytes
    except OSError:
        return False

def check_dir(path):
    """Check if directory exists and is non-empty."""
    if not path.is_dir():
        return False
    try:
        return any(path.iterdir())
    except (OSError, PermissionError):
        return False

def get_file_size(path):
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0

def count_dir_files(path):
    if not path.is_dir():
        return 0
    try:
        return sum(1 for _ in path.iterdir())
    except (OSError, PermissionError):
        return -1


def scan_all_documents():
    all_docs = {}
    fund_dirs = sorted(d for d in OUTPUT_DIR.iterdir() if d.is_dir())

    for fund_dir in fund_dirs:
        fund_code = fund_dir.name
        doc_dirs = sorted(d for d in fund_dir.iterdir() if d.is_dir())
        for doc_dir in doc_dirs:
            meta_path = doc_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                with open(meta_path, "r") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, PermissionError):
                continue

            # ── Disk artifacts ──
            text_json = doc_dir / "text.json"
            table_desc = doc_dir / "table_describe.json"
            not_table_desc = doc_dir / "not_table_describe.json"
            text_seg = doc_dir / "text_segmentation.json"
            text_seg_emb = doc_dir / "text_segmentation_embedding.json"
            table_img_dir = doc_dir / "table_image"
            temp_img_dir = doc_dir / "temp_pdf_images"
            embeddings_dir = doc_dir / "embeddings"

            # Check text.json for internal metadata flags (the pitfall source)
            text_meta_flags = {}
            if check_file(text_json):
                try:
                    with open(text_json) as f:
                        tj = json.load(f)
                    if isinstance(tj, dict) and "metadata" in tj:
                        meta_block = tj["metadata"]
                        if isinstance(meta_block, dict):
                            for flag in ["text_extracted", "table_detection_scan_done",
                                         "table_describe_done", "not_table_describe_done",
                                         "merge_done", "text_segmentation", "embedding_done",
                                         "vector_database_done", "elasticsearch_database_done"]:
                                if flag in meta_block:
                                    text_meta_flags[flag] = meta_block[flag]
                except (json.JSONDecodeError, OSError):
                    pass

            # Read meta.json flags
            flags = {}
            for fname in ["text_extracted", "table_detection_vector_done",
                          "table_detection_scan_done", "table_describe_done",
                          "not_table_describe_done", "merge_done", "text_segmentation",
                          "embedding_done", "vector_database_done",
                          "elasticsearch_database_done"]:
                flags[fname] = meta.get(fname, False)

            # Count pages in text.json for rough size check
            text_page_count = 0
            if check_file(text_json):
                try:
                    with open(text_json) as f:
                        tj = json.load(f)
                    if isinstance(tj, dict) and "pages" in tj:
                        text_page_count = len(tj["pages"])
                except (json.JSONDecodeError, OSError):
                    pass

            # Determine actual pipeline stage from disk evidence
            stage_num, stage_name = 0, "no_text"

            has_text = check_file(text_json, min_bytes=500)
            has_table_img = check_dir(table_img_dir)
            has_table_desc = check_file(table_desc)
            has_not_table_desc = check_file(not_table_desc)
            has_text_seg = check_file(text_seg, min_bytes=500)
            has_text_seg_emb = check_file(text_seg_emb, min_bytes=500)
            has_temp_img = check_dir(temp_img_dir)
            has_emb_dir = check_dir(embeddings_dir)

            if has_text_seg_emb:
                stage_num, stage_name = 7, "embedding_done"
            elif has_text_seg:
                stage_num, stage_name = 6, "text_segmentation"
            elif has_text and (has_table_desc or has_not_table_desc):
                # Has text + descriptions, merge may or may not have run
                # If text.json metadata says merge_done=True, merge likely ran
                if text_meta_flags.get("merge_done") or flags.get("merge_done"):
                    stage_num, stage_name = 5, "merge_done"
                elif has_table_desc:
                    stage_num, stage_name = 4.1, "table_describe_done"
                else:
                    stage_num, stage_name = 4.2, "not_table_describe_done"
            elif has_text and has_table_img:
                stage_num, stage_name = 3, "table_detection_scan_done"
            elif has_text:
                stage_num, stage_name = 2, "text_extracted"
            elif has_temp_img:
                stage_num, stage_name = 1, "pdf_images"
            else:
                stage_num, stage_name = 0, "no_artifacts"

            # ── Mismatch detection ──
            mismatches = {}

            # merge_done inflated: meta=True but text_seg doesn't exist
            # (merge is prerequisite for segmentation)
            if flags.get("merge_done") and not has_text_seg and not has_text_seg_emb:
                mismatches["merge_done"] = "meta=True, but no text_segmentation.json (merge prereq missing or merge didn't produce usable output)"

            # text_segmentation inflated
            if flags.get("text_segmentation") and not has_text_seg and not has_text_seg_emb:
                mismatches["text_segmentation"] = "meta=True, but no text_segmentation.json on disk"

            # embedding_done inflated
            if flags.get("embedding_done") and not has_text_seg_emb and not has_emb_dir:
                mismatches["embedding_done"] = "meta=True, but no text_segmentation_embedding.json on disk"

            # table_describe_done inflated
            if flags.get("table_describe_done") and not has_table_desc:
                mismatches["table_describe_done"] = "meta=True, but no table_describe.json on disk"

            # not_table_describe_done inflated (may be legit)
            if flags.get("not_table_describe_done") and not has_not_table_desc:
                mismatches["not_table_describe_done"] = "meta=True, but no not_table_describe.json"

            # table_detection_scan_done inflated
            if flags.get("table_detection_scan_done") and not has_table_img:
                # table_image may have been deleted by GATE1 after merge
                if not flags.get("merge_done"):
                    mismatches["table_detection_scan_done"] = "meta=True, but no table_image/ and merge not done"

            # text_extracted deflated
            if not flags.get("text_extracted") and has_text:
                mismatches["text_extracted"] = "meta=False, but text.json exists"

            # vector/ES DB flags vs disk truth
            if flags.get("vector_database_done") and not has_text_seg_emb:
                mismatches["vector_database_done"] = "meta=True, but no embedding file (cannot be in vector DB)"
            if flags.get("elasticsearch_database_done") and not has_text_seg_emb and not has_text_seg:
                mismatches["elasticsearch_database_done"] = "meta=True, but no segmentation/embedding (cannot be in ES)"

            doc_info = {
                "fund_code": fund_code,
                "doc_name": doc_dir.name,
                "doc_dir": str(doc_dir),
                "meta_flags": flags,
                "text_meta_flags": text_meta_flags,
                "disk": {
                    "text_json": has_text,
                    "text_json_size": get_file_size(text_json),
                    "text_page_count": text_page_count,
                    "table_desc_json": has_table_desc,
                    "not_table_desc_json": has_not_table_desc,
                    "text_seg_json": has_text_seg,
                    "text_seg_size": get_file_size(text_seg),
                    "text_seg_emb_json": has_text_seg_emb,
                    "text_seg_emb_size": get_file_size(text_seg_emb),
                    "table_image_dir": has_table_img,
                    "table_image_count": count_dir_files(table_img_dir),
                    "temp_pdf_images_dir": has_temp_img,
                    "temp_pdf_images_count": count_dir_files(temp_img_dir),
                    "embeddings_dir": has_emb_dir,
                },
                "actual_stage": (stage_num, stage_name),
                "mismatches": mismatches,
                "file_name": meta.get("file_name", ""),
                "date": meta.get("date", ""),
            }
            all_docs[f"{fund_code}/{doc_dir.name}"] = doc_info

    return all_docs


def compute_statistics(all_docs):
    stats = {
        "total_docs": len(all_docs),
        "by_actual_stage": Counter(),
        "flags_true_in_meta": defaultdict(int),
        "artifacts_present": defaultdict(int),
        "inflated_flags": defaultdict(int),
        "deflated_flags": defaultdict(int),
        "docs_with_mismatches": 0,
        "docs_without_mismatches": 0,
        "by_mismatch_type": Counter(),
    }
    for key, doc in all_docs.items():
        stage = doc["actual_stage"][1]
        stats["by_actual_stage"][stage] += 1
        for flag, val in doc["meta_flags"].items():
            if val:
                stats["flags_true_in_meta"][flag] += 1
        for art_key, val in doc["disk"].items():
            if val and not art_key.endswith("_size") and not art_key.endswith("_count"):
                stats["artifacts_present"][art_key] += 1
        if doc["mismatches"]:
            stats["docs_with_mismatches"] += 1
            for flag, desc in doc["mismatches"].items():
                stats["by_mismatch_type"][f"{flag}: {desc}"] += 1
                if "meta=True" in desc:
                    stats["inflated_flags"][flag] += 1
                elif "meta=False" in desc:
                    stats["deflated_flags"][flag] += 1
        else:
            stats["docs_without_mismatches"] += 1
    return stats


def categorize_for_repair(all_docs):
    """Group docs by the next pipeline step that needs to run, based on disk truth."""
    cats = {
        "A_no_text": [],                 # No text.json → needs step1-2
        "B_need_table_detect": [],       # Has text, no table_image → needs step3
        "C_need_table_describe": [],     # Has table_image, no table_describe.json → needs step4.1
        "D_need_non_table_describe": [], # Has table_describe, no not_table_describe → needs step4.2
        "E_need_merge": [],              # Has table_describe, no text_seg → needs step5
        "F_need_segmentation": [],       # Has merged text, no text_seg → needs step6
        "G_need_embedding": [],          # Has text_seg, no text_seg_emb → needs step7
        "H_need_es_ingest": [],          # Has text_seg_emb, elasticsearch_database_done=False
        "I_need_vector_ingest": [],      # Has text_seg_emb, vector_database_done=False
        "J_healthy": [],                 # All flags consistent, all artifacts present
        "K_flag_only": [],               # Flags out of sync, but artifacts OK → just fix flags
    }

    for key, doc in all_docs.items():
        d = doc["disk"]
        f = doc["meta_flags"]

        if not d["text_json"]:
            cats["A_no_text"].append(key)
        elif d["text_seg_emb_json"]:
            # Has full chain through embedding
            # Check which DB flags are missing
            missing_es = not f.get("elasticsearch_database_done")
            missing_vec = not f.get("vector_database_done")
            if missing_es and missing_vec:
                cats["H_need_es_ingest"].append(key)
            elif missing_vec:
                cats["I_need_vector_ingest"].append(key)
            elif missing_es:
                cats["H_need_es_ingest"].append(key)
            elif doc["mismatches"]:
                cats["K_flag_only"].append(key)
            else:
                cats["J_healthy"].append(key)
        elif d["text_seg_json"]:
            # Has segmentation, needs embedding
            cats["G_need_embedding"].append(key)
        elif d["table_desc_json"] or d["not_table_desc_json"]:
            # Has descriptions, needs merge + segmentation + embedding
            cats["E_need_merge"].append(key)
        elif d["table_image_dir"]:
            cats["C_need_table_describe"].append(key)
        elif d["text_json"]:
            # Has text only, check if it needs table detection
            # These docs might be pure-text (no tables to detect)
            cats["B_need_table_detect"].append(key)
        else:
            cats["A_no_text"].append(key)

    return cats


def print_report(all_docs, stats, cats):
    print("=" * 80)
    print("PIPELINE DIAGNOSTIC REPORT v2")
    print("=" * 80)
    print(f"\nTotal documents scanned: {stats['total_docs']}\n")

    print("--- ACTUAL PIPELINE STAGE (by disk artifacts) ---")
    for stage, count in sorted(stats["by_actual_stage"].items()):
        bar = "█" * min(count // 10, 60)
        print(f"  {stage:50s}: {count:6d} {bar}")

    print("\n--- META.JSON FLAGS (True counts) ---")
    for flag in ["text_extracted", "table_detection_scan_done", "table_describe_done",
                  "not_table_describe_done", "merge_done", "text_segmentation",
                  "embedding_done", "vector_database_done", "elasticsearch_database_done"]:
        count = stats["flags_true_in_meta"].get(flag, 0)
        print(f"  {flag:35s}: {count:6d}")

    print("\n--- DISK ARTIFACTS (present counts) ---")
    for art in ["text_json", "table_image_dir", "table_desc_json", "not_table_desc_json",
                "text_seg_json", "text_seg_emb_json"]:
        count = stats["artifacts_present"].get(art, 0)
        print(f"  {art:35s}: {count:6d}")

    print("\n--- INFLATED FLAGS (meta=True but artifact missing) ---")
    for flag, count in sorted(stats["inflated_flags"].items()):
        print(f"  {flag:35s}: {count:6d}")

    print("\n--- DEFLATED FLAGS (meta=False but artifact present) ---")
    for flag, count in sorted(stats["deflated_flags"].items()):
        print(f"  {flag:35s}: {count:6d}")

    print(f"\n--- OVERALL ---")
    print(f"  Docs WITH mismatches:    {stats['docs_with_mismatches']:6d}")
    print(f"  Docs WITHOUT mismatches: {stats['docs_without_mismatches']:6d}")

    print("\n--- DETAILED MISMATCH TYPES (top 30, condensed) ---")
    summary = defaultdict(list)
    for mismatch, count in stats["by_mismatch_type"].most_common(50):
        # Group by flag
        flag = mismatch.split(":")[0]
        summary[flag].append((mismatch, count))
    for flag in sorted(summary.keys()):
        items = summary[flag]
        total = sum(c for _, c in items)
        print(f"  {flag}: {total} total")
        for mismatch, count in items[:3]:
            print(f"      {mismatch:90s}: {count:6d}")

    print("\n" + "=" * 80)
    print("REPAIR CATEGORIES (disk-based)")
    print("=" * 80)
    cat_order = ["A_no_text", "B_need_table_detect", "C_need_table_describe",
                 "D_need_non_table_describe", "E_need_merge", "F_need_segmentation",
                 "G_need_embedding", "H_need_es_ingest", "I_need_vector_ingest",
                 "J_healthy", "K_flag_only"]
    cat_labels = {
        "A_no_text": "No text.json → need step1-2",
        "B_need_table_detect": "Has text, no table_image → need step3",
        "C_need_table_describe": "Has table_image, no table_desc → need step4.1",
        "D_need_non_table_describe": "Has table_desc, no not_table_desc → need step4.2",
        "E_need_merge": "Has descriptions, no text_seg → need step5 merge",
        "F_need_segmentation": "Has merged text, no text_seg → need step6",
        "G_need_embedding": "Has text_seg, no embeddings → need step7",
        "H_need_es_ingest": "Has embeddings, ES flag=False → need step8.1",
        "I_need_vector_ingest": "Has embeddings, Vec flag=False → need step8.2",
        "J_healthy": "All consistent and complete",
        "K_flag_only": "Artifacts OK, only flags need fixing",
    }
    print()
    for cat in cat_order:
        docs = cats.get(cat, [])
        if docs:
            label = cat_labels.get(cat, cat)
            print(f"  {cat:30s} | {label:60s} | count={len(docs):6d}")
    print()

    # Special analysis: For E_need_merge docs, check if they have table_describe
    e_docs = cats.get("E_need_merge", [])
    if e_docs:
        has_td = sum(1 for k in e_docs if all_docs[k]["disk"]["table_desc_json"])
        has_ntd = sum(1 for k in e_docs if all_docs[k]["disk"]["not_table_desc_json"])
        has_both = sum(1 for k in e_docs if all_docs[k]["disk"]["table_desc_json"] and all_docs[k]["disk"]["not_table_desc_json"])
        print(f"  [Detail] 'E_need_merge' docs ({len(e_docs)}):")
        print(f"      Have table_describe.json:  {has_td}")
        print(f"      Have not_table_describe.json: {has_ntd}")
        print(f"      Have both: {has_both}")
        print()

    # Special analysis: cross-reference the 695 docs the user mentioned
    # 695 docs = 543 flagged ready + 152 flagged needing embedding
    h_docs = cats.get("I_need_vector_ingest", [])  # Vec flag false but embeddings exist
    g_docs = cats.get("G_need_embedding", [])  # Need embedding
    print(f"\n  Vec-ingest pending (embeddings exist, vec flag=False): {len(h_docs)}")
    print(f"  Need embedding (has text_seg, no embeddings):     {len(g_docs)}")
    print(f"  Combined (I+G):                                     {len(h_docs) + len(g_docs)}")


def save_reports(all_docs, stats, cats):
    """Save detailed JSON reports for further analysis."""
    # Save stats
    report = {
        "statistics": {
            "total_docs": stats["total_docs"],
            "by_actual_stage": dict(stats["by_actual_stage"]),
            "flags_true_in_meta": dict(stats["flags_true_in_meta"]),
            "artifacts_present": dict(stats["artifacts_present"]),
            "inflated_flags": dict(stats["inflated_flags"]),
            "deflated_flags": dict(stats["deflated_flags"]),
            "docs_with_mismatches": stats["docs_with_mismatches"],
            "docs_without_mismatches": stats["docs_without_mismatches"],
        },
        "categories": {cat: len(docs) for cat, docs in cats.items()},
    }
    report_path = OUTPUT_DIR / "diagnostic_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\nDetailed stats saved to: {report_path}")

    # Save per-category doc lists
    for cat, docs in cats.items():
        if docs:
            cat_path = OUTPUT_DIR / f"repair_{cat}.json"
            with open(cat_path, "w") as f:
                json.dump(docs, f, ensure_ascii=False, indent=2)
    print(f"Per-category doc lists saved to: {OUTPUT_DIR}/repair_*.json")


if __name__ == "__main__":
    print("Scanning all documents on disk (v2, with real artifact knowledge)...")
    all_docs = scan_all_documents()
    print(f"Scanned {len(all_docs)} documents.\n")

    stats = compute_statistics(all_docs)
    cats = categorize_for_repair(all_docs)
    print_report(all_docs, stats, cats)
    save_reports(all_docs, stats, cats)
