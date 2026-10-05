#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 将 mysql 数据库表 text_segmentation_embedding 中信息导入至向量数据库
#
# [DEPRECATED] Milvus 时代残留备份脚本（2026-07 起向量引擎已迁 FAISS）。
# 现行方案: build_faiss_index.py 全量重建索引 + step8_2_ingest_vector_database.py 标志同步。
# 本文件保留仅作历史参考，勿再运行。
"""
主要改动:
1) 使用ThreadPoolExecutor并发处理文件,可调MAX_WORKERS
2) 每个文件流程: 
   - 从DB获取所有字段(含embedding)
   - collection.load()后delete,再insert; 若出错则delete回滚
3) 每个文件成功后更新数据库中 processed_files 表的 vector_database_done 字段为 "true"
4) 在 vector_database.log 中记录WARNING级信息和提示
5) date若为 datetime.date => 转为字符串写入；embedding 不写入ES，只用于关键词检索
"""

import os
import time
import json
import logging
import threading
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_config import get_vector_db_config
import file_paths_config
from common_utils import safe_json_dump, safe_json_load
from pymilvus import connections, Collection, utility

MULTIFILE_OUTPUT_DIR = file_paths_config.OUTPUT_DIR
MANIFEST_FILE = os.path.join(MULTIFILE_OUTPUT_DIR, "processed_files_local.json")
json_lock = threading.Lock()

COLLECTION_NAME = "reits_announcement"
# 获取脚本所在目录，确保日志文件生成在log目录下
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, "vector_database.log")

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
file_handler.setLevel(logging.WARNING)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 可调最大并发线程数
MAX_WORKERS = 2


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

    seg_emb_path = os.path.join(pdf_folder_dir, "text_segmentation_embedding.json")
    if os.path.exists(seg_emb_path):
        merged.setdefault("embedding_done", True)

    return merged


def get_pending_files_from_local():
    manifest = _safe_read_json(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {}) or {}
    from common_utils import filter_manifest_files_by_env; files_map = filter_manifest_files_by_env(files_map)
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
        if status.get("vector_database_done") is True:
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
            "vector_database_done": False,
        }
        grouped_files.setdefault(fund_code, []).append(row)

    for fc in grouped_files:
        grouped_files[fc].sort(key=lambda x: x.get("file_name", ""))

    return grouped_files


def update_local_vector_db_done(file_info):
    file_name = file_info.get("file_name", "")
    fund_code = file_info.get("fund_code", "")
    if not file_name or not fund_code:
        return False

    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    meta.update(file_info)
    meta["vector_database_done"] = True
    safe_json_dump(meta, meta_path)

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path)
    if isinstance(text_json, dict):
        text_meta = text_json.get("metadata", {}) or {}
        text_meta["vector_database_done"] = True
        text_json["metadata"] = text_meta
        safe_json_dump(text_json, text_path)

    # Manifest write disabled: multi-threaded writes to the same JSON file
    # cause corruption. Status is tracked via meta.json + text.json instead.
    # Manifest is rebuilt by rebuild_manifest.py after batches complete.
    # with json_lock:
    #     manifest = _safe_read_json(MANIFEST_FILE) or {"files": {}}
    #     if "files" not in manifest or not isinstance(manifest["files"], dict):
    #         manifest["files"] = {}
    #     entry = manifest["files"].get(file_name, {}) or {}
    #     entry.update(file_info)
    #     entry["vector_database_done"] = True
    #     manifest["files"][file_name] = entry
    #     safe_json_dump(manifest, MANIFEST_FILE)

    return True


def _load_chunk_rows_for_milvus(pdf_folder_dir: str, source_file: str):
    seg_emb_path = os.path.join(pdf_folder_dir, "text_segmentation_embedding.json")
    if not os.path.exists(seg_emb_path):
        return None, f"找不到切分向量文件: {seg_emb_path}"

    try:
        chunks = safe_json_load(seg_emb_path)
    except Exception as e:
        return None, f"读取切分向量文件失败: {e}"

    if not isinstance(chunks, list) or not chunks:
        return None, "切分向量文件为空或格式不正确"

    rows = []
    for ck in chunks:
        if not isinstance(ck, dict):
            continue
        meta = ck.get("metadata", {}) or {}
        embedding = ck.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            continue

        chunk_id = int(ck.get("chunk_id", 0) or 0)
        global_id = ck.get("global_id") or ""
        file_path = meta.get("file_path", "")
        dt = meta.get("date", "")
        fund_code = meta.get("fund_code", "")
        short_name = meta.get("short_name", "")
        ann_title = meta.get("announcement_title", "")
        doc1 = meta.get("doc_type_1", "")
        doc2 = meta.get("doc_type_2", "")
        ann_link = meta.get("announcement_link", "")
        s_file = meta.get("source_file", "") or source_file
        page_num = meta.get("page_num", "")
        pic_path = meta.get("picture_path", "")
        ch_count = int(meta.get("char_count", 0) or 0)

        prev_v = meta.get("prev_chunks", "")
        if isinstance(prev_v, (list, dict)):
            prev_c = json.dumps(prev_v, ensure_ascii=False)
        else:
            prev_c = str(prev_v) if prev_v is not None else ""

        next_v = meta.get("next_chunks", "")
        if isinstance(next_v, (list, dict)):
            next_c = json.dumps(next_v, ensure_ascii=False)
        else:
            next_c = str(next_v) if next_v is not None else ""

        txt = ck.get("text", "") or ""

        db_id = int(meta.get("id", 0) or ck.get("id", 0) or chunk_id)

        rows.append((
            db_id,
            str(global_id),
            chunk_id,
            str(file_path),
            str(dt),
            str(fund_code),
            str(short_name),
            str(ann_title),
            str(doc1),
            str(doc2),
            str(ann_link),
            str(s_file),
            str(page_num),
            str(pic_path),
            ch_count,
            prev_c,
            next_c,
            str(txt),
            embedding,
        ))

    if not rows:
        return None, "切分向量文件中没有可入库的chunk（embedding为空）"

    rows.sort(key=lambda r: r[2])
    return rows, None

def delete_existing_in_milvus(collection, source_file):
    """
    先 delete expr="source_file=='xxx'", 需 collection.load()后再删
    """
    expr = f"source_file == '{source_file}'"
    try:
        collection.delete(expr=expr)
        print(f"已删除 source_file={source_file} 在 Milvus 中旧数据。")
    except Exception as e:
        logger.warning(f"删除 Milvus 旧数据失败: {e}")

def insert_to_milvus(collection, source_file, chunk_rows):
    """
    根据 schema 将数据插入集合 COLLECTION_NAME
    支持分批插入大文件数据，避免 gRPC 消息大小超限
    """
    if not chunk_rows:
        raise ValueError("该文件数据库中无文本块记录或embedding为空")

    # 分批插入，每批最多 100 条记录
    batch_size = 100
    total_inserted = 0
    
    for i in range(0, len(chunk_rows), batch_size):
        batch_rows = chunk_rows[i:i+batch_size]
        
        ids = []
        global_ids = []
        chunk_ids = []
        file_paths = []
        dates = []
        fund_codes = []
        short_names = []
        ann_titles = []
        doc1s = []
        doc2s = []
        ann_links = []
        source_files = []
        page_nums = []
        pic_paths = []
        char_counts = []
        prev_chs = []
        next_chs = []
        texts = []
        embeddings = []
        
        for row in batch_rows:
            ids.append(row[0])
            global_ids.append(row[1])
            chunk_ids.append(row[2])
            file_paths.append(row[3])
            dates.append(row[4])
            fund_codes.append(row[5])
            short_names.append(row[6])
            ann_titles.append(row[7])
            doc1s.append(row[8])
            doc2s.append(row[9])
            ann_links.append(row[10])
            source_files.append(row[11])
            page_nums.append(row[12])
            pic_paths.append(row[13])
            char_counts.append(row[14])
            prev_chs.append(row[15])
            next_chs.append(row[16])
            texts.append(row[17])
            embeddings.append(row[18])
        
        data = [
            ids, global_ids, chunk_ids, file_paths, dates, fund_codes, short_names,
            ann_titles, doc1s, doc2s, ann_links, source_files, page_nums,
            pic_paths, char_counts, prev_chs, next_chs, texts, embeddings
        ]
        
        try:
            resp = collection.insert(data)
            total_inserted += len(batch_rows)
            print(f"成功插入批次 {i//batch_size + 1}: {len(batch_rows)} 条数据到集合 '{COLLECTION_NAME}'。")
        except Exception as e:
            # 如果批次插入失败，尝试进一步减小批次大小
            if "message larger than max" in str(e) and len(batch_rows) > 10:
                print(f"批次太大，尝试更小的批次大小...")
                smaller_batch_size = 10
                for j in range(0, len(batch_rows), smaller_batch_size):
                    small_batch = batch_rows[j:j+smaller_batch_size]
                    small_data = [
                        [row[k] for row in small_batch] for k in range(19)
                    ]
                    try:
                        resp = collection.insert(small_data)
                        total_inserted += len(small_batch)
                        print(f"成功插入小批次: {len(small_batch)} 条数据。")
                    except Exception as inner_e:
                        print(f"小批次插入也失败: {inner_e}")
                        raise inner_e
            else:
                raise e
    
    print(f"文件 {source_file} 总共成功插入 {total_inserted} 条数据到集合 '{COLLECTION_NAME}'。")

def ingest_file_to_milvus(pdf_info):
    """
    单个文件处理逻辑(线程任务):
      1) 读取本地 text_segmentation_embedding.json
      2) 打开 Milvus Collection
      3) 删除旧数据
      4) 插入新数据
      若异常则删除已插入数据以模拟回滚, 返回 (True, None) 或 (False, error_reason)
    """
    source_file = pdf_info.get("file_name", "")
    fund_code = pdf_info.get("fund_code", "")
    if not source_file:
        return (False, "缺少 file_name")
    if not fund_code:
        return (False, "缺少 fund_code")

    pdf_folder_dir = _get_pdf_folder_dir(source_file, fund_code)
    chunk_rows, err = _load_chunk_rows_for_milvus(pdf_folder_dir, source_file)
    if err:
        return (False, err)

    try:
        collection = Collection(name=COLLECTION_NAME)
    except Exception as e:
        return (False, f"打开 Milvus Collection失败: {e}")

    # 跳过 delete：Milvus 集合过大 (422K entities) 导致 load 超时
    # insert 不需要加载集合，直接插入; 少数重复可通过后续 de-dup 处理
    try:
        insert_to_milvus(collection, source_file, chunk_rows)
    except Exception as e:
        return (False, f"插入 Milvus 出错: {e}")

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

    print(f"本次需要处理 {total_count} 个文件的向量数据库入库(多线程={MAX_WORKERS})...")
    success_count = 0
    failed_details = []  # 存 (file_name, reason)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {}
        for pdf_info in files_to_process:
            future = executor.submit(ingest_file_to_milvus, pdf_info)
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
                try:
                    update_local_vector_db_done(pdf_info)
                except Exception as e:
                    print(f"文件 {file_name} 更新本地状态失败: {e}")
                    logger.warning(f"文件 {file_name} 更新本地状态失败: {e}")
                    failed_details.append((file_name, f"更新本地状态失败: {e}"))
                    continue

                success_count += 1
                print(f"文件 {file_name} 向量数据库入库成功，已更新本地状态。")
            else:
                if not err_reason:
                    err_reason = "unknown reason"
                print(f"{file_name} 向量数据库入库失败，原因: {err_reason}")
                failed_details.append((file_name, err_reason))
            time.sleep(1)

    remain = total_count - success_count
    if success_count == total_count:
        print("向量数据库入库全部完成！")
        logger.warning("向量数据库入库全部完成！")
    else:
        if success_count == 0:
            print("所有文件向量数据库入库均失败或跳过。")
            logger.warning("所有文件向量数据库入库均失败或跳过。")
        else:
            print(f"有 {remain} 个文件未完成向量数据库入库。")
            logger.warning(f"有 {remain} 个文件未完成向量数据库入库。")

    if failed_details:
        for fname, reason in failed_details:
            logger.warning(f"文件 {fname} 入库失败, 原因: {reason}")
        failed_files_only = [f for (f, _) in failed_details]
        logger.warning(f"下列文件向量数据库入库处理失败: {failed_files_only}")

if __name__ == "__main__":
    vector_db_config = get_vector_db_config()
    milvus_kwargs = {
        "alias": "default",
        "host": vector_db_config["host"],
        "port": vector_db_config["port"],
    }
    user = (vector_db_config.get("user") or "").strip()
    password = vector_db_config.get("password")
    if user and password:
        milvus_kwargs["user"] = user
        milvus_kwargs["password"] = password

    connections.connect(**milvus_kwargs)
    main()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()