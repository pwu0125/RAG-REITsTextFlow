#!/usr/bin/env python3
"""ES 批量入库 — 适配 {fund_code}/{doc_type}/{pdf_folder}/ 层级结构。
   从 processed_files_local.json 读取 pending 文件，逐个入库并更新状态。"""
import os, json, time, base64, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from elasticsearch import Elasticsearch, helpers

BASE = "/Users/pyemini/REITs/REITs_Text_data_pipeline/RAG-REITsTextFlow/announcement_document_processing_local"
JSON_PATH = os.path.join(BASE, "processed_files_local.json")
INDEX = "reits_announcements"
MAX_WORKERS = 5
json_lock = threading.Lock()

# ----- ES 连接 -----
env = {}
with open("/Users/pyemini/REITs/REITs_Text_data_pipeline/RAG-REITsTextFlow/.env") as f:
    for line in f:
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            env[k] = v.strip('"').strip("'")

token = base64.b64encode(f"elastic:{env['ES_PASSWORD']}".encode()).decode()
es = Elasticsearch([f"http://127.0.0.1:9200"],
                   headers={"Authorization": f"Basic {token}"},
                   verify_certs=False)

# ----- Path helpers -----
def find_pdf_dir(file_name, fund_code):
    """在 {fund_code}/{doc_type}/{pdf_folder}/ 下找到目录"""
    folder = os.path.splitext(file_name)[0]
    code_dir = os.path.join(BASE, fund_code)
    if not os.path.isdir(code_dir):
        return None
    for dtype in os.listdir(code_dir):
        candidate = os.path.join(code_dir, dtype, folder)
        if os.path.isdir(candidate):
            return candidate
    return None

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return None

def save_json(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ----- ES ops -----
def ingest_one(info):
    fname = info["file_name"]
    code = info["fund_code"]
    pdf_dir = find_pdf_dir(fname, code)
    if not pdf_dir:
        return (False, f"目录不存在: {code}/.../{os.path.splitext(fname)[0]}")

    emb_path = os.path.join(pdf_dir, "text_segmentation_embedding.json")
    fallback = os.path.join(pdf_dir, "text_segmentation.json")
    path = emb_path if os.path.exists(emb_path) else fallback
    if not os.path.exists(path):
        return (False, f"无切分文件: {emb_path}")

    chunks = load_json(path)
    if not isinstance(chunks, list) or not chunks:
        return (False, "切分文件为空")

    docs = []
    for ck in chunks:
        if not isinstance(ck, dict):
            continue
        meta = ck.get("metadata", {}) or {}
        docs.append({
            "id": int(ck.get("chunk_id", 0) or 0),
            "global_id": ck.get("global_id") or "",
            "chunk_id": int(ck.get("chunk_id", 0) or 0),
            "file_path": meta.get("file_path", ""),
            "date": meta.get("date", ""),
            "fund_code": meta.get("fund_code", code),
            "short_name": meta.get("short_name", ""),
            "announcement_title": meta.get("announcement_title", ""),
            "doc_type_1": meta.get("doc_type_1", ""),
            "doc_type_2": meta.get("doc_type_2", ""),
            "announcement_link": meta.get("announcement_link", ""),
            "source_file": meta.get("source_file", "") or fname,
            "page_num": meta.get("page_num", ""),
            "picture_path": meta.get("picture_path", ""),
            "char_count": int(meta.get("char_count", 0) or 0),
            "prev_chunks": json.dumps(meta.get("prev_chunks", []), ensure_ascii=False),
            "next_chunks": json.dumps(meta.get("next_chunks", []), ensure_ascii=False),
            "text": ck.get("text", "") or "",
        })

    if not docs:
        return (False, "无可入库chunk")

    docs.sort(key=lambda d: d.get("chunk_id", 0))

    # Delete old
    try:
        r = es.delete_by_query(index=INDEX, body={"query": {"term": {"source_file.keyword": fname}}})
        print(f"  DEL {fname[:50]}: {r['deleted']} old docs")
    except Exception as e:
        print(f"  WARN delete: {e}")

    # Bulk insert
    actions = [{"_index": INDEX, "_id": d["global_id"] or d["id"], "_source": d} for d in docs]
    result = helpers.bulk(es, actions, raise_on_error=False, raise_on_exception=False)
    count = result[0] if result else 0
    return (True, count)

def update_status(info):
    """Update meta.json and processed_files_local.json"""
    fname = info["file_name"]
    code = info["fund_code"]
    pdf_dir = find_pdf_dir(fname, code)
    if not pdf_dir:
        return False

    # Update meta.json
    meta = load_json(os.path.join(pdf_dir, "meta.json")) or {}
    meta["elasticsearch_database_done"] = True
    save_json(meta, os.path.join(pdf_dir, "meta.json"))

    # Update text.json metadata
    text = load_json(os.path.join(pdf_dir, "text.json"))
    if isinstance(text, dict):
        m = text.get("metadata", {}) or {}
        m["elasticsearch_database_done"] = True
        text["metadata"] = m
        save_json(text, os.path.join(pdf_dir, "text.json"))

    return True

# ----- Main -----
def main():
    manifest = load_json(JSON_PATH) or {}
    files = manifest.get("files", {})

    pending = []
    for fname, info in files.items():
        if not isinstance(info, dict):
            continue
        if not info.get("embedding_done"):
            continue
        if info.get("elasticsearch_database_done"):
            continue
        pending.append(info)

    total = len(pending)
    print(f"Pending: {total} files")

    if total == 0:
        print("Nothing to do.")
        return

    success = 0
    total_docs = 0
    failed = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(ingest_one, p): p for p in pending}
        for fut in as_completed(futures):
            info = futures[fut]
            fname = info["file_name"]
            try:
                ok, res = fut.result()
            except Exception as e:
                ok, res = False, str(e)

            if ok:
                with json_lock:
                    update_status(info)
                    # Update JSON manifest
                    files[fname]["elasticsearch_database_done"] = True
                success += 1
                total_docs += res
                print(f"  ✅ {fname[:60]} +{res} docs")
            else:
                failed.append((fname, res))
                print(f"  ❌ {fname[:60]}: {res}")

    # Save updated manifest
    with json_lock:
        manifest["files"] = files
        manifest["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # Recalculate summary
        from collections import defaultdict
        by_code = defaultdict(lambda: {"total": 0, "es_done": 0})
        for fn, info in files.items():
            if not isinstance(info, dict): continue
            c = info.get("fund_code", "?")
            by_code[c]["total"] += 1
            if info.get("elasticsearch_database_done"):
                by_code[c]["es_done"] += 1
        fully = sum(1 for s in by_code.values() if s["es_done"] == s["total"])
        manifest["summary"]["es_fully_indexed"] = fully
        manifest["summary"]["es_not_indexed"] = len(by_code) - fully
        save_json(manifest, JSON_PATH)

    print(f"\nDone: {success}/{total} files, {total_docs} docs ingested")
    if failed:
        print(f"Failed: {len(failed)}")
        for fn, reason in failed[:5]:
            print(f"  {fn[:60]}: {reason}")

if __name__ == "__main__":
    main()
