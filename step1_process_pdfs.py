#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 找到本轮需要处理的pdf（本地模式，不依赖MySQL）
"""
step1_process_pdfs.py - 本地台账版本（不依赖 MySQL）

目标：
1) 适配两种文件名格式：
   - 旧格式：2023-05-19_180101.SH_简称_标题.pdf
   - 新格式：180101-标题或简称_标题-2023-05-19.pdf
2) 不使用 MySQL：改为写本地“台账文件” processed_files_local.json
3) 为每个 PDF 建立输出目录结构（供后续 step2/step3 使用）：
   OUTPUT_DIR/<fund_code>/<pdf_basename>/

"""

import os
import re
import logging
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from file_paths_config import PDF_DIR, OUTPUT_DIR
from common_utils import safe_json_dump, safe_json_load

# ========== 日志配置 ===========
# 创建日志目录，使用相对路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(CURRENT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, "process_pdfs.log")
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
file_handler.setLevel(logging.WARNING)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

DEFAULT_STATUS = {
    "text_extracted": False,
    "table_detection_vector_done": False,
    "table_detection_scan_done": False,
    "table_describe_done": False,
    "not_table_describe_done": False,
    "merge_done": False,
    "text_segmentation": False,
    "embedding_done": False,
    "vector_database_done": False,
    "elasticsearch_database_done": False,
}

def parse_pdf_filename(file_name: str):
    base = os.path.basename(file_name)
    stem, ext = os.path.splitext(base)
    if ext.lower() != ".pdf":
        return None

    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{6}\.\w{2})_(.*?)_(.+)$", stem)
    if m:
        date, fund_code, short_name, announcement_title = m.groups()
        return {
            "date": date,
            "fund_code": fund_code,
            "short_name": short_name,
            "announcement_title": announcement_title,
        }

    m = re.match(r"^(\d{6})-(.*)-(\d{4}-\d{2}-\d{2})$", stem)
    if m:
        fund_code, middle, date = m.groups()
        if "_" in middle:
            short_name, announcement_title = middle.split("_", 1)
        else:
            short_name, announcement_title = "", middle
        return {
            "date": date,
            "fund_code": fund_code,
            "short_name": short_name,
            "announcement_title": announcement_title,
        }

    return None

def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        try:
            data = safe_json_load(MANIFEST_FILE)
            if isinstance(data, dict) and "files" in data and isinstance(data["files"], dict):
                from common_utils import filter_manifest_files_by_env
                data["files"] = filter_manifest_files_by_env(data["files"])
                return data
        except Exception:
            pass
    return {
        "generated_at": "",
        "source_dir": PDF_DIR,
        "output_dir": OUTPUT_DIR,
        "files": {}
    }


def save_manifest(manifest):
    manifest["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_json_dump(manifest, MANIFEST_FILE)
# 单个 PDF 文件多线程处理
def process_pdf(file):
    parsed = parse_pdf_filename(file)
    if not parsed:
        return None

    file_path = os.path.join(PDF_DIR, file)
    if not os.path.exists(file_path):
        return None
    # 解析符号链接获取真实路径
    file_path = os.path.realpath(file_path)

    fund_code = parsed["fund_code"]
    short_name = parsed["short_name"]
    announcement_title = parsed["announcement_title"]
    date = parsed["date"]

    pdf_folder_name = os.path.splitext(file)[0]
    pdf_folder_dir = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    entry = {
        "file_name": file,
        "file_path": file_path,
        "date": date,
        "fund_code": fund_code,
        "short_name": short_name,
        "announcement_title": announcement_title,
        "doc_type_1": "",
        "doc_type_2": "",
        "announcement_link": "",
    }
    entry.update(DEFAULT_STATUS)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    # 修复（2026-08-15）：manifest 脱节时本文件可能已被处理过，meta.json 已含
    # text_extracted=True/merge_done=True 等状态标志。以 existing 为基底、entry 只补
    # existing 缺失的键，保留既有状态标志与 step0 检测字段，避免被全新 entry 冲回 False。
    if os.path.exists(meta_path):
        try:
            existing = safe_json_load(meta_path)
            if isinstance(existing, dict):
                for k, v in entry.items():
                    if k not in existing:
                        existing[k] = v
                entry = existing
        except Exception:
            pass
    safe_json_dump(entry, meta_path)

    print(f"解析文件: {file} => 日期:{date},基金代码:{fund_code},基金简称:{short_name},公告:{announcement_title}")
    return (fund_code, entry)

def process_pdfs():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    manifest = load_manifest()
    manifest_files = set(manifest.get("files", {}).keys())

    if not os.path.exists(PDF_DIR):
        print(f"PDF目录不存在: {PDF_DIR}")
        logger.warning(f"PDF目录不存在: {PDF_DIR}")
        return

    from common_utils import is_manifest_filtered
    if is_manifest_filtered():
        # Whitelist mode: 只处理过滤后的 manifest 中的文档
        files_to_process = [f for f in manifest_files if f.lower().endswith(".pdf")]
        total_pdf_files = len(manifest_files)
        skipped_bad_name = 0
    else:
        # Default mode: 处理 raw/ 中尚未在 manifest 的文档
        pdf_files = os.listdir(PDF_DIR)
        total_pdf_files = len(pdf_files)
        files_to_process = []
        skipped_bad_name = 0
        for file in pdf_files:
            if not file.lower().endswith(".pdf"):
                continue
            if file in manifest_files:
                continue
            if not parse_pdf_filename(file):
                skipped_bad_name += 1
                continue
            files_to_process.append(file)

    pending_count = len(files_to_process)
    results = []
    errors = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        future_map = {executor.submit(process_pdf, f): f for f in files_to_process}
        for future in as_completed(future_map):
            f = future_map[future]
            try:
                res = future.result()
                if res:
                    results.append(res)
                else:
                    errors.append((f, "返回None"))
            except Exception as e:
                errors.append((f, str(e)))

    # 写入本地台账
    for _, entry in results:
        manifest["files"][entry["file_name"]] = entry
    try:
        save_manifest(manifest)
    except Exception as e:
        errors.append((MANIFEST_FILE, f"写入台账失败: {e}"))

    inserted_count = len(results)
    
    fund_groups = {}
    for _, entry in results:
        fc = entry.get("fund_code", "")
        fund_groups[fc] = fund_groups.get(fc, 0) + 1
    group_count = len(fund_groups)

    # 终端输出
    print("\n===========================================")
    print(f"总PDF文件数: {total_pdf_files}")
    print(f"待处理文件数量: {pending_count}")
    print(f"文件名不符合规则(跳过): {skipped_bad_name}")
    print(f"本次处理基金组数: {group_count}, 本次登记文件数: {inserted_count}, 失败: {len(errors)}")
    if errors:
        print("失败文件:")
        for (fname, reason) in errors:
            print(f"  - {fname}: {reason}")
    print("===========================================\n")

    # 写日志
    logger.warning(f"总PDF文件数: {total_pdf_files}")
    logger.warning(f"待处理文件数量: {pending_count}")
    logger.warning(f"文件名不符合规则(跳过): {skipped_bad_name}")
    logger.warning(f"本次处理基金组数: {group_count}, 本次登记文件数: {inserted_count}, 失败: {len(errors)}")
    if errors:
        logger.warning(f"本次处理失败共 {len(errors)} 个:")
        for (fname, reason) in errors:
            logger.warning(f"  文件: {fname}, 原因: {reason}")

if __name__ == "__main__":
    process_pdfs()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()
