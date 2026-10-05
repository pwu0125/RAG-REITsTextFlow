#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
step8_2_ingest_vector_database.py — FAISS 向量数据库标志同步

替代原 Milvus 版 step8_2。

⚠️ 架构约定（2026-07-23）:
  本脚本不构建 FAISS 索引——那是 build_faiss_index.py 的职责。
  本脚本只做一件事：找到 manifest 中 embedding_done=True 但 vector_database_done=False
  的文件，验证其嵌入数据有效，然后更新 meta.json + manifest 标志。

  为什么不做增量追加？
  - IndexFlatIP 虽支持 add()，但无法检测重复——同一文件跑两次会造出双份向量
  - 全量重建由 build_faiss_index.py 负责，6 秒完成 113 万向量，远快于增量去重判断
  - 符合"全量重建 > 增量更新"的运维确定性原则

使用:
  python step8_2_ingest_vector_database.py
  BATCH_CODES="180101,180102" python step8_2_ingest_vector_database.py
  python step8_2_ingest_vector_database.py --verify  # 逐文件验证嵌入维度
"""

import os, sys, time, json, logging, threading, argparse
from typing import List
import numpy as np

import file_paths_config
from common_utils import (
    safe_json_dump, safe_json_load,
    filter_files_by_batch, get_doc_from_cli, filter_files_by_doc,
    get_codes_from_env_or_config,
)

# ── 配置 ─────────────────────────────────────────────────
MULTIFILE_OUTPUT_DIR = file_paths_config.OUTPUT_DIR
MANIFEST_FILE = os.path.join(MULTIFILE_OUTPUT_DIR, "processed_files_local.json")
json_lock = threading.Lock()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, "faiss_database.log")

EXPECTED_DIM = 768  # bge-base-zh-v1.5

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
fh = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(ch)


def _strip_pdf_suffix(filename: str) -> str:
    """manifest 存 pdf 文件名，磁盘目录无 .pdf 后缀"""
    return filename.replace('.pdf', '') if filename.endswith('.pdf') else filename


def load_pending_files(batch_codes=None):
    """加载 manifest 中 embedding_done=True 但 vector_database_done=False 的文件"""
    manifest = safe_json_load(MANIFEST_FILE) or {}
    files_map = manifest.get('files', {})

    if batch_codes:
        files_map = filter_files_by_batch(files_map, batch_codes)

    doc_filter = get_doc_from_cli()
    if doc_filter:
        files_map = filter_files_by_doc(files_map, doc_filter)

    pending = {}
    if files_map:
        for key, info in files_map.items():
            if not isinstance(info, dict):
                continue
            if info.get('embedding_done') is not True:
                continue
            if info.get('vector_database_done') is True:
                continue
            pending[key] = info

    logger.info(f"待同步标志: {len(pending)} (总数: {len(files_map)})")
    return pending


def verify_embedding_file(file_info, check_dim=True):
    """
    验证嵌入文件存在且维度正确。
    返回 (valid: bool, total_chunks: int, bad_dim: int, error: str|None)
    """
    fund_code = str(file_info.get('fund_code', ''))
    doc_name = _strip_pdf_suffix(file_info.get('file_name', ''))

    doc_dir = os.path.join(MULTIFILE_OUTPUT_DIR, fund_code, doc_name)
    emb_path = os.path.join(doc_dir, 'text_segmentation_embedding.json')

    if not os.path.exists(emb_path):
        return False, 0, 0, f"文件不存在: {emb_path}"

    try:
        with open(emb_path) as f:
            chunks = json.load(f)
    except Exception as e:
        return False, 0, 0, f"JSON 解析失败: {e}"

    total = len(chunks)
    bad_dim = 0

    if check_dim:
        for ci, chunk in enumerate(chunks):
            emb = chunk.get('embedding')
            if not emb or not isinstance(emb, list):
                bad_dim += 1
                continue
            if len(emb) != EXPECTED_DIM:
                bad_dim += 1

    return True, total, bad_dim, None


def update_meta_flag(file_info):
    """更新 meta.json 的 vector_database_done=True，并同步 text.json.metadata。

    rebuild_manifest_v3.py 读进度时 text.json.metadata 优先（6/17 覆写后权威源），
    若只写 meta.json 会导致重建后 manifest 标志回退为 False。
    """
    fund_code = str(file_info.get('fund_code', ''))
    doc_name = _strip_pdf_suffix(file_info.get('file_name', ''))

    doc_dir = os.path.join(MULTIFILE_OUTPUT_DIR, fund_code, doc_name)
    meta_path = os.path.join(doc_dir, 'meta.json')
    if not os.path.exists(meta_path):
        return False

    with json_lock:
        with open(meta_path) as f:
            meta = json.load(f)
        meta['vector_database_done'] = True
        safe_json_dump(meta, meta_path)

        text_path = os.path.join(doc_dir, 'text.json')
        if os.path.exists(text_path):
            text = safe_json_load(text_path)
            if isinstance(text, dict):
                text_meta = text.get('metadata', {}) or {}
                text_meta['vector_database_done'] = True
                text['metadata'] = text_meta
                safe_json_dump(text, text_path)
    return True


def update_manifest_flag(file_key):
    """更新 manifest 中的 vector_database_done=True"""
    manifest = safe_json_load(MANIFEST_FILE)
    if not manifest or 'files' not in manifest or file_key not in manifest['files']:
        return False

    with json_lock:
        manifest = safe_json_load(MANIFEST_FILE)
        if manifest and 'files' in manifest and file_key in manifest['files']:
            manifest['files'][file_key]['vector_database_done'] = True
            safe_json_dump(manifest, MANIFEST_FILE)
    return True


def main():
    parser = argparse.ArgumentParser(description='FAISS 向量数据库标志同步')
    parser.add_argument('--batch-codes', type=str, default='',
                        help='REIT 代码列表，逗号分隔 (如: 508022,508078)')
    parser.add_argument('--doc', type=str, default='',
                        help='单文档文件名过滤（子串匹配，与 --batch-codes 二选一）')
    parser.add_argument('--verify', action='store_true',
                        help='逐文件验证嵌入数据有效性（维度+存在性），无此标志只检查文件存在')
    args = parser.parse_args()

    batch_codes = get_codes_from_env_or_config()
    if args.batch_codes:
        batch_codes = [c.strip() for c in args.batch_codes.split(',') if c.strip()]

    if batch_codes:
        logger.info(f"BATCH_CODES: {batch_codes}")

    # ── 加载 pending ──
    pending = load_pending_files(batch_codes if batch_codes else None)

    if not pending:
        logger.info("无待同步文件")
        return

    # ── 逐文件处理 ──
    success_count = 0
    fail_count = 0
    skip_count = 0
    total_chunks = 0
    total_bad_dim = 0

    start_time = time.time()

    for key, info in pending.items():
        fund_code = str(info.get('fund_code', ''))
        doc_name = info.get('file_name', '')

        # 验证
        valid, chunks, bad_dim, error = verify_embedding_file(info, check_dim=args.verify)
        total_chunks += chunks
        total_bad_dim += bad_dim

        if not valid:
            fail_count += 1
            logger.error(f"  ❌ {fund_code}/{doc_name[:50]}... {error}")
            continue

        if args.verify and bad_dim > 0:
            logger.warning(f"  ⚠️  {fund_code}/{doc_name[:50]}... {bad_dim}/{chunks} 维度异常(≠{EXPECTED_DIM})")
            if bad_dim == chunks:
                skip_count += 1
                logger.warning(f"      全部维度异常，跳过")
                continue

        # 更新标志
        meta_ok = update_meta_flag(info)
        manifest_ok = update_manifest_flag(key)

        if meta_ok and manifest_ok:
            success_count += 1
            status = f"{doc_name[:40]}"
            logger.info(f"  ✅ {fund_code}/{status}... {chunks} chunks ✓")
        else:
            fail_count += 1
            logger.error(f"  ❌ {fund_code}/{doc_name[:40]}... 标志更新失败 (meta={meta_ok}, manifest={manifest_ok})")

    # ── GATE 结果 ──
    elapsed = time.time() - start_time
    logger.info(f"\n{'='*55}")
    logger.info(f"标志同步完成: ✅ {success_count} | ❌ {fail_count} | ⏭️ {skip_count}")
    logger.info(f"验证 chunks: {total_chunks:,} | 维度异常: {total_bad_dim}")
    logger.info(f"耗时: {elapsed:.1f}s")
    logger.info(f"{'='*55}\n")


if __name__ == '__main__':
    main()
