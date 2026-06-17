#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 将 mysql 表 text_segmentation_embedding 中信息导入 elasticsearch 里


import os
import time
import json
import logging
import threading
import base64
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed
from elasticsearch import Elasticsearch, helpers

from db_config import get_elasticsearch_config
import file_paths_config
from common_utils import safe_json_dump, safe_json_load

MULTIFILE_OUTPUT_DIR = file_paths_config.OUTPUT_DIR
MANIFEST_FILE = os.path.join(MULTIFILE_OUTPUT_DIR, "processed_files_local.json")
json_lock = threading.Lock()

INDEX_NAME = "reits_announcements"
# 获取脚本所在目录，确保日志文件生成在log目录下
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, "es_database.log")

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
file_handler.setLevel(logging.WARNING)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 可调最大并发线程数
MAX_WORKERS = 5

# 初始化 ES
es_config = get_elasticsearch_config()
scheme = es_config.get('scheme', 'http')  # 默认使用 http

es_hosts = [f"{scheme}://{es_config['host']}:{es_config['port']}"]
es_username = (es_config.get('username') or '').strip()
es_password = es_config.get('password')

es_kwargs = {
    "verify_certs": False,
}

if es_username and es_password:
    token = base64.b64encode(f"{es_username}:{es_password}".encode("utf-8")).decode("ascii")
    es_kwargs["headers"] = {"Authorization": f"Basic {token}"}

es = Elasticsearch(es_hosts, **es_kwargs)

def _safe_read_json(path):
    try:
        if os.path.exists(path):
            return safe_json_load(path)
    except Exception:
        return None
    return None


def _get_pdf_folder_dir(file_name: str, fund_code: str) -> str:
    pdf_folder_name = os.path.splitext(file_name)[0]
    return os.path.join(MULTIFILE_OUTPUT_DIR, fund_code, pdf_folder_name)


def _infer_status_from_files(pdf_folder_dir: str):
    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path) or {}
    text_meta = text_json.get("metadata", {}) or {}

    merged = {}
    merged.update(text_meta)
    merged.update(meta)
    return merged


def get_pending_files_from_local():
    manifest = _safe_read_json(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {}) or {}
    grouped_files = {}

    for file_name, base_info in files_map.items():
        if not isinstance(base_info, dict):
            continue

        fund_code = (base_info or {}).get("fund_code") or ""
        if not fund_code:
            continue

        pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
        status = _infer_status_from_files(pdf_folder_dir)

        if status.get("doc_type_1") == "无关":
            continue
        if status.get("embedding_done") is not True:
            continue
        if status.get("elasticsearch_database_done") is True:
            continue

        row = {
            "file_name": file_name,
            "file_path": (base_info or {}).get("file_path", ""),
            "date": (base_info or {}).get("date") or status.get("date") or "",
            "fund_code": fund_code,
            "short_name": (base_info or {}).get("short_name") or status.get("short_name") or "",
            "announcement_title": (base_info or {}).get("announcement_title") or status.get("announcement_title") or "",
            "doc_type_1": status.get("doc_type_1") or "",
            "doc_type_2": status.get("doc_type_2") or "",
            "announcement_link": status.get("announcement_link") or "",
            "embedding_done": True,
            "elasticsearch_database_done": False,
        }
        grouped_files.setdefault(fund_code, []).append(row)

    for fc in grouped_files:
        grouped_files[fc].sort(key=lambda x: x.get("file_name", ""))

    return grouped_files


def update_local_es_done(file_info):
    file_name = file_info.get("file_name", "")
    fund_code = file_info.get("fund_code", "")
    if not file_name or not fund_code:
        return False

    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    meta.update(file_info)
    meta["elasticsearch_database_done"] = True
    safe_json_dump(meta, meta_path)

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path)
    if isinstance(text_json, dict):
        text_meta = text_json.get("metadata", {}) or {}
        text_meta["elasticsearch_database_done"] = True
        text_json["metadata"] = text_meta
        safe_json_dump(text_json, text_path)

    # [BATCH-FIX] manifest write removed — rebuilt by rebuild_manifest.py after step completes
    return True


def get_pending_files_from_db():
    return get_pending_files_from_local()



def _load_chunks_for_es(pdf_folder_dir: str, source_file: str):
    preferred = os.path.join(pdf_folder_dir, "text_segmentation_embedding.json")
    fallback = os.path.join(pdf_folder_dir, "text_segmentation.json")
    path = preferred if os.path.exists(preferred) else fallback
    if not os.path.exists(path):
        return None, f"找不到切分文件: {preferred} 或 {fallback}"

    try:
        chunks = safe_json_load(path)
    except Exception as e:
        return None, f"读取切分文件失败: {e}"

    if not isinstance(chunks, list) or not chunks:
        return None, "切分文件为空或格式不正确"

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
            "fund_code": meta.get("fund_code", ""),
            "short_name": meta.get("short_name", ""),
            "announcement_title": meta.get("announcement_title", ""),
            "doc_type_1": meta.get("doc_type_1", ""),
            "doc_type_2": meta.get("doc_type_2", ""),
            "announcement_link": meta.get("announcement_link", ""),
            "source_file": meta.get("source_file", "") or source_file,
            "page_num": meta.get("page_num", ""),
            "picture_path": meta.get("picture_path", ""),
            "char_count": int(meta.get("char_count", 0) or 0),
            "prev_chunks": json.dumps(meta.get("prev_chunks", []), ensure_ascii=False),
            "next_chunks": json.dumps(meta.get("next_chunks", []), ensure_ascii=False),
            "text": ck.get("text", "") or "",
        })

    if not docs:
        return None, "切分文件中没有可入库的chunk"

    docs.sort(key=lambda d: d.get("chunk_id", 0))
    return docs, None

def delete_existing_from_es(source_file):
    """
    先删除 ES 中旧数据：source_file 字段匹配
    """
    try:
        query_body = {
            "query": {
                "term": {
                    "source_file.keyword": source_file
                }
            }
        }
        res = es.delete_by_query(index=INDEX_NAME, body=query_body)
        print(f"已删除 source_file={source_file} 在ES中旧数据, 删除数量: {res['deleted']}.")
    except Exception as e:
        logger.warning(f"删除ES旧数据失败: {e}")

def bulk_insert_es(source_file, docs):
    """
    批量插入 docs（list of dict）到 ES
    """
    if not docs:
        raise ValueError("无文档可插入ES")
    actions = []
    for doc in docs:
        _id = doc["global_id"] if "global_id" in doc and doc["global_id"] else doc["id"]
        action = {
            "_index": INDEX_NAME,
            "_id": _id,
            "_source": doc
        }
        actions.append(action)
    resp = helpers.bulk(es, actions, raise_on_error=False, raise_on_exception=False)
    return resp

def ingest_single_file(pdf_info):
    source_file = pdf_info.get("file_name", "")
    fund_code = pdf_info.get("fund_code", "")
    if not source_file:
        return (False, "缺少 file_name")
    if not fund_code:
        return (False, "缺少 fund_code")

    pdf_folder_dir = _get_pdf_folder_dir(source_file, fund_code)
    if not os.path.exists(pdf_folder_dir):
        return (False, f"PDF文件夹不存在: {pdf_folder_dir}")

    docs, err = _load_chunks_for_es(pdf_folder_dir, source_file)
    if err:
        return (False, err)

    delete_existing_from_es(source_file)
    try:
        bulk_insert_es(source_file, docs)
    except Exception as e:
        delete_existing_from_es(source_file)
        return (False, f"插入ES出错: {e}")
    return (True, None)

def main():
    processed_files = get_pending_files_from_local()
    
    if not processed_files:
        print("没有找到需要处理的文件。")
        logger.warning("没有找到需要处理的文件。")
        return

    # 收集需要处理的文件
    files_to_process = []
    for fund_code, pdf_list in processed_files.items():
        for pdf_info in pdf_list:
            files_to_process.append(pdf_info)

    total_count = len(files_to_process)
    if total_count == 0:
        print("没有找到需要处理的文件。")
        logger.warning("没有找到需要处理的文件。")
        return

    print(f"本次需要处理 {total_count} 个文件的ES数据入库(多线程={MAX_WORKERS})...")
    success_count = 0
    failed_details = []  # 存 (file_name, reason)

    # 多线程
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {}
        for pdf_info in files_to_process:
            future = executor.submit(ingest_single_file, pdf_info)
            future_to_file[future] = pdf_info

        for fut in as_completed(future_to_file):
            pdf_info = future_to_file[fut]
            file_name = pdf_info.get("file_name", "")
            try:
                success, err_reason = fut.result()
            except Exception as e:
                success = False
                err_reason = str(e)
            if success:
                with json_lock:
                    ok = update_local_es_done(pdf_info)
                if not ok:
                    failed_details.append((file_name, "更新本地状态失败"))
                    print(f"文件 {file_name} ES入库成功，但更新本地状态失败。")
                    continue

                success_count += 1
                print(f"文件 {file_name} ES数据入库成功，已更新本地状态。")
            else:
                if not err_reason:
                    err_reason = "unknown reason"
                print(f"{file_name} 导入ES失败, 原因: {err_reason}")
                failed_details.append((file_name, err_reason))
            time.sleep(1)

    remain = total_count - success_count
    if success_count == total_count:
        print("es数据库入库全部完成！")
        logger.warning("es数据库入库全部完成！")
    else:
        if success_count == 0:
            print("所有文件ES入库均失败或跳过。")
            logger.warning("所有文件ES入库均失败或跳过。")
        else:
            print(f"有 {remain} 个文件未完成ES入库。")
            logger.warning(f"有 {remain} 个文件未完成ES入库。")

    if failed_details:
        for fname, reason in failed_details:
            logger.warning(f"文件 {fname} ES入库失败, 原因: {reason}")
        failed_files_only = [f for (f, _) in failed_details]
        logger.warning(f"下列文件ES入库处理失败: {failed_files_only}")

if __name__ == "__main__":
    main()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()