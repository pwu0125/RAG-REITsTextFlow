#!/usr/bin/env python3
"""
gen_lockfile.py — 精确生成增量入库 lockfile (v2: fast manifest scan)

用法:
  python gen_lockfile.py --from 2026-06-01 --keywords "招募说明书" --output batch.json
  python gen_lockfile.py --from 2026-06-01 --keywords "扩募" "解除限售" --output batch.json --dry-run
"""
import json, os, sys, argparse
from datetime import datetime

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "announcement_document_processing_local")
MANIFEST = os.path.join(BASE, "processed_files_local.json")

def load_manifest():
    with open(MANIFEST) as f:
        return json.load(f)

def main():
    parser = argparse.ArgumentParser(description="Generate incremental lockfile")
    parser.add_argument("--from", dest="from_date", default="")
    parser.add_argument("--keywords", nargs="*", default=[])
    parser.add_argument("--codes", nargs="*", default=[])
    parser.add_argument("--exclude", nargs="*", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    m = load_manifest()
    files = m.get("files", {})

    targets = []
    for key, entry in files.items():
        name = entry.get("file_name", "")
        date = entry.get("date", "")
        code = entry.get("fund_code", "")

        if args.from_date and date < args.from_date:
            continue
        if args.codes and code not in args.codes:
            continue
        if args.keywords and not any(kw in name for kw in args.keywords):
            continue
        if args.exclude and any(kw in name for kw in args.exclude):
            continue

        # Verify doc exists on disk — use EXACT match via manifest file_name lookup
        # The manifest key format: "file_name.pdf" — strip .pdf to match dir name
        manifest_key_no_ext = key
        if manifest_key_no_ext.endswith(".pdf"):
            manifest_key_no_ext = manifest_key_no_ext[:-4]
        
        doc_dir = os.path.join(BASE, code, manifest_key_no_ext)
        if os.path.isdir(doc_dir):
            doc_id = f"{code}/{manifest_key_no_ext}"
            mp = os.path.join(doc_dir, "meta.json")
            flags = {}
            if os.path.exists(mp):
                with open(mp) as f:
                    meta = json.load(f)
                flags = {
                    "text": meta.get("text_extracted", False),
                    "embed": meta.get("embedding_done", False),
                    "es": meta.get("elasticsearch_database_done", False),
                    "mv": meta.get("vector_database_done", False),
                }
            targets.append({
                "doc_id": doc_id,
                "fund_code": code,
                "file_name": name,
                "date": date,
                "flags": flags,
            })

    lockfile = {
        "generated_at": datetime.now().isoformat(),
        "filters": vars(args),
        "count": len(targets),
        "docs": targets,
    }

    if args.dry_run:
        for t in targets:
            f = t["flags"]
            status = []
            if f.get("text"): status.append("TXT")
            if f.get("embed"): status.append("EMB")
            if f.get("es"): status.append("ES")
            if f.get("mv"): status.append("MV")
            print(f"  [{','.join(status) if status else 'NEW':12s}] {t['fund_code']}: {t['file_name'][:60]}")
        print(f"\n  Total: {len(targets)} docs")
        return

    with open(args.output, "w") as f:
        json.dump(lockfile, f, ensure_ascii=False, indent=2)
    print(f"{len(targets)} docs → {args.output}")

if __name__ == "__main__":
    main()
