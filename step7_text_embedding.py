#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 将 mysql 数据库中表 text_segmentation_embedding 的 text 转为向量保存至 “embedding” 字段
"""
step7_text_embedding.py (多线程版本, 记录失败原因)

1) 使用 ThreadPoolExecutor 并发处理文件
2) 对每个文件:
   - 获取 db 连接 => 开启事务 => 查询 text_segmentation_embedding => 生成 embedding => 更新 => 提交
   - 若失败 => rollback => 记录错误原因
3) 在 future.result() 成功后, 更新数据库 processed_files 表的 embedding_done 状态
4) 对于失败文件, 在日志 embedding.log 中记录其文件名及失败原因
5) 对于空文本块, 默认不视为错误(只要 API 不报错即可). 如果全部 chunk 都为空, 仍会送空字符串到 API (不会报错时即不是错误).
   若想特别处理可在 embedding 前检测.
"""

import os
import re
import json
import time
import logging
import threading
from typing import List
from concurrent.futures import ThreadPoolExecutor, as_completed

# ------------------ import local configs ------------------
import file_paths_config
from model_config import MODEL_CONFIG
from openai import OpenAI  # 封装 zhipu API
from common_utils import safe_json_dump, safe_json_load

MULTIFILE_OUTPUT_DIR = file_paths_config.OUTPUT_DIR
MANIFEST_FILE = os.path.join(MULTIFILE_OUTPUT_DIR, "processed_files_local.json")
json_lock = threading.Lock()

# 日志文件 embedding.log, WARNING 及以上级别
# 获取脚本所在目录，确保日志文件生成在log目录下
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, "embedding.log")
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
file_handler.setLevel(logging.WARNING)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# 并发线程数，可按需修改
MAX_WORKERS = 5

def _get_env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


def _get_env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default

# embedding 相关配置
EMBED_BATCH_SIZE = _get_env_int("EMBED_BATCH_SIZE", 10)
EMBED_RETRY_MAX = _get_env_int("EMBED_RETRY_MAX", 5)
EMBED_RETRY_BASE_SECS = _get_env_float("EMBED_RETRY_BASE_SECS", 1.5)
MAX_CHARS = 3000
EMBEDDING_DIM = 2048

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
        if status.get("text_segmentation") is not True:
            continue
        if status.get("embedding_done") is True:
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
            "text_segmentation": True,
            "embedding_done": False,
        }
        grouped_files.setdefault(fund_code, []).append(row)

    for fc in grouped_files:
        grouped_files[fc].sort(key=lambda x: x.get("file_name", ""))

    return grouped_files


def update_local_embedding_done(file_info):
    file_name = file_info.get("file_name", "")
    fund_code = file_info.get("fund_code", "")
    if not file_name or not fund_code:
        return False

    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    meta.update(file_info)
    meta["embedding_done"] = True
    safe_json_dump(meta, meta_path)

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path)
    if isinstance(text_json, dict):
        text_meta = text_json.get("metadata", {}) or {}
        text_meta["embedding_done"] = True
        text_json["metadata"] = text_meta
        safe_json_dump(text_json, text_path)

    manifest = _safe_read_json(MANIFEST_FILE) or {"files": {}}
    if "files" not in manifest or not isinstance(manifest["files"], dict):
        manifest["files"] = {}
    entry = manifest["files"].get(file_name, {}) or {}
    entry.update(file_info)
    entry["embedding_done"] = True
    manifest["files"][file_name] = entry
    safe_json_dump(manifest, MANIFEST_FILE)
    return True


def get_pending_files_from_db():
    return get_pending_files_from_local()

class Config:
    embedding_provider = "ali"
    # 2026-09-26 修正: 单纯切 v4→v3/v2 都不是正解——索引权威是 build_faiss_index 的本地 bge-base-zh-v1.5
    # (768维). step7 的 API 嵌入(1024维 v4)产物会被索引维度过滤静默跳过(508606/508610 事件).
    # 正解 = step7 直接走本地 bge(与索引同源), 见 LocalBgeEmbeddings; 环境变量可强制回退 API:
    #   STEP7_EMBEDDING=api  恢复旧行为(不推荐)
    embedding_model = "text-embedding-v4"
    use_local_bge = os.environ.get("STEP7_EMBEDDING", "local") != "api"

class LocalBgeEmbeddings:
    """本地 bge-base-zh-v1.5 (768维) — 与 build_faiss_index.py 索引同源.
    2026-09-26 新增: 修复 step7 走 ali v4(1024维) 产物被索引维度过滤静默跳过的问题."""

    _model = None

    def __init__(self):
        from sentence_transformers import SentenceTransformer
        if LocalBgeEmbeddings._model is None:
            LocalBgeEmbeddings._model = SentenceTransformer(
                "BAAI/bge-base-zh-v1.5", device="mps")
        self.model = LocalBgeEmbeddings._model

    def embed_documents(self, texts):
        vecs = self.model.encode(
            texts, batch_size=64, normalize_embeddings=True,
            show_progress_bar=False)
        return [v.tolist() for v in vecs]


class OpenAIEmbeddings:
    """
    使用 Zhipu API 生成文本向量
    """
    def __init__(self):
        try:
            self.model_config = MODEL_CONFIG[Config.embedding_provider][Config.embedding_model]
        except KeyError:
            raise ValueError(f"未找到配置：{Config.embedding_provider}/{Config.embedding_model}")
        self.client = OpenAI(
            api_key=self.model_config["api_key"],
            base_url=self.model_config["base_url"]
        )
        self.model_name = self.model_config["model"]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        all_embeddings = []
        batch_size = EMBED_BATCH_SIZE
        if Config.embedding_provider == "ali" and batch_size > 10:
            batch_size = 10
        if batch_size <= 0:
            batch_size = 10

        def _embed_once(batch_texts: List[str]) -> List[List[float]]:
            response = self.client.embeddings.create(
                model=self.model_name,
                input=batch_texts,
                encoding_format="float"
            )
            return [item.embedding for item in response.data]

        def _embed_with_retry(batch_texts: List[str]) -> List[List[float]]:
            for attempt in range(1, EMBED_RETRY_MAX + 1):
                try:
                    return _embed_once(batch_texts)
                except Exception as e:
                    if attempt >= EMBED_RETRY_MAX:
                        raise
                    wait = EMBED_RETRY_BASE_SECS * (2 ** (attempt - 1))
                    logger.warning(f"向量生成失败，准备重试({attempt}/{EMBED_RETRY_MAX})，等待 {wait}s：{e}")
                    time.sleep(wait)
            raise RuntimeError("向量生成重试失败")

        def _embed_resilient(batch_texts: List[str]) -> List[List[float]]:
            try:
                return _embed_with_retry(batch_texts)
            except Exception:
                if len(batch_texts) <= 1:
                    raise
                mid = len(batch_texts) // 2
                left = _embed_resilient(batch_texts[:mid])
                right = _embed_resilient(batch_texts[mid:])
                return left + right

        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i:i+batch_size]
            batch_texts = [t[:MAX_CHARS] for t in batch_texts]
            print(f"正在生成向量 batch {i // batch_size + 1}, 文本数量: {len(batch_texts)}")

            embeddings = _embed_resilient(batch_texts)
            all_embeddings.extend(embeddings)

        return all_embeddings

def load_json_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json_file(data, path):
    safe_json_dump(data, path)

def load_json_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json_file(data, path):
    safe_json_dump(data, path)


def _load_segmentation_chunks(pdf_folder_dir: str):
    seg_path = os.path.join(pdf_folder_dir, "text_segmentation.json")
    if not os.path.exists(seg_path):
        return None, f"切分文件不存在: {seg_path}"

    try:
        data = load_json_file(seg_path)
    except Exception as e:
        return None, f"读取切分文件失败: {e}"

    if not isinstance(data, list):
        return None, "切分文件格式不正确(应为list)"

    return data, None


def _save_segmentation_chunks(pdf_folder_dir: str, chunks):
    seg_path = os.path.join(pdf_folder_dir, "text_segmentation.json")
    save_json_file(chunks, seg_path)


def _write_embedding_snapshot(pdf_folder_dir: str, chunks):
    out_path = os.path.join(pdf_folder_dir, "text_segmentation_embedding.json")
    save_json_file(chunks, out_path)

def process_file_embedding(pdf_info):
    file_name = pdf_info.get("file_name", "")
    fund_code = pdf_info.get("fund_code", "")
    if not file_name:
        return (False, "文件信息中缺少 file_name")
    if not fund_code:
        return (False, "文件信息中缺少 fund_code")

    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    if not os.path.exists(pdf_folder_dir):
        return (False, f"PDF文件夹不存在: {pdf_folder_dir}")

    chunks, err = _load_segmentation_chunks(pdf_folder_dir)
    if err:
        return (False, err)

    texts = []
    for ck in chunks:
        if isinstance(ck, dict):
            texts.append(str(ck.get("text", "")) if ck.get("text") is not None else "")
        else:
            texts.append("")

    try:
        if Config.use_local_bge:
            embedder = LocalBgeEmbeddings()
        else:
            embedder = OpenAIEmbeddings()
        embeddings = embedder.embed_documents(texts)
    except Exception as e:
        return (False, str(e))

    if len(embeddings) != len(chunks):
        return (False, "chunks数量与embedding结果数量不匹配")

    for ck, vec in zip(chunks, embeddings):
        if isinstance(ck, dict):
            ck["embedding"] = vec

    try:
        _save_segmentation_chunks(pdf_folder_dir, chunks)
        _write_embedding_snapshot(pdf_folder_dir, chunks)
    except Exception as e:
        return (False, f"写入本地embedding结果失败: {e}")

    return (True, None)

def main():
    processed_files = get_pending_files_from_db()
    if not processed_files:
        print("没有找到需要处理的文件。")
        logger.warning("没有找到需要处理的文件。")
        return

    # 遍历数据库查询结果，构建待处理列表
    files_to_process = []
    for fund_code, pdf_list in processed_files.items():
        for pdf_info in pdf_list:
            files_to_process.append(pdf_info)

    total_count = len(files_to_process)
    if total_count == 0:
        print("没有找到需要处理的文件。")
        logger.warning("没有找到需要处理的文件。")
        return

    print(f"本次需要处理 {total_count} 个文件的向量化...")
    success_count = 0
    failed_details = []  # 存 (file_name, reason)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_file = {}
        for pdf_info in files_to_process:
            fut = executor.submit(process_file_embedding, pdf_info)
            future_to_file[fut] = pdf_info

        for fut in as_completed(future_to_file):
            pdf_info = future_to_file[fut]
            file_name = pdf_info.get("file_name", "")
            try:
                # ═══ Fix 2: timeout guard — 300s per file, prevents silent hang ═══
                success, error_reason = fut.result(timeout=300)
            except Exception as e:
                success = False
                error_reason = str(e)
                logger.warning(f"文件 {file_name} embedding出现未知异常: {error_reason}")

            if success:
                with json_lock:
                    ok = update_local_embedding_done(pdf_info)
                if ok:
                    success_count += 1
                    print(f"已完成 {file_name} 的向量化处理。")
                else:
                    print(f"文件 {file_name} 更新本地状态 embedding_done 失败。")
                    logger.warning(f"文件 {file_name} 更新本地状态 embedding_done 失败。")
                    failed_details.append((file_name, "更新本地状态失败"))
            else:
                if not error_reason:
                    error_reason = "Unknown failure"
                print(f"{file_name} 处理向量化失败，原因: {error_reason}")
                failed_details.append((file_name, error_reason))

    remain = total_count - success_count
    if success_count == total_count:
        print("embedding全部完成！")
        logger.warning("embedding全部完成！")
    else:
        if success_count == 0:
            print("所有文件向量化均失败或跳过。")
            logger.warning("所有文件向量化均失败或跳过。")
        else:
            print(f"有 {remain} 个文件未完成向量化。")
            logger.warning(f"有 {remain} 个文件未完成向量化。")

    if failed_details:
        for (fname, reason) in failed_details:
            logger.warning(f"文件 {fname} 向量化处理失败，原因: {reason}")
        failed_files_only = [f for (f, _) in failed_details]
        logger.warning(f"下列文件向量化处理失败: {failed_files_only}")

if __name__ == "__main__":
    main()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()