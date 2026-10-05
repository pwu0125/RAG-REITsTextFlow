#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
rebuild_manifest_v3.py — 文件驱动进度检测 (File-Driven Progress Detection)

与 v1/v2 的关键区别:
  ❌ v1/v2: 读 meta.json 中的 boolean flag 判断进度 → 一次覆写全部归零
  ✅ v3:     检查磁盘上的实际产物文件 → 数据在则进度在，不受 meta.json 覆写影响

双结构扫描:
  2层: {fund_code}/{doc_name}/
  3层: {fund_code}/{doc_type}/{doc_name}/
  自动合并，2层优先（新管道标准结构）

进度判断规则 (按优先级):
  step1 (PDF渲染):       temp_pdf_images/ 目录存在且有内容
  step2 (文本提取):       text.json 存在且 > 500 bytes
  step3_1 (向量检测):    meta.json flag (唯一需要 flag 的步骤，无磁盘产物)
  step3_2 (扫描检测):    table_image/ 目录存在且有内容
  step4_1_1 (表格描述):  table_describe.json 存在且 > 100 bytes
  step4_2_1 (非表格描述): not_table_describe.json 存在且 > 100 bytes
  step5 (合并):          merge_done flag OR (text.json > 1KB 且含"表格描述")
  step6 (分段):          text_segmentation flag OR text_segmentation 目录存在
  step7 (向量嵌入):      embedding_done flag
  step8_1 (ES入库):      elasticsearch_database_done flag
  step8_2 (向量入库):    vector_database_done flag

核心原则:
  - 文件存在性 > flag 布尔值
  - 已有 True 永不降级为 False（不可变累积）
  - 同时扫描 2+3 层，合并去重
"""

import os
import json
import logging
import re
from collections import Counter, defaultdict

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(OUTPUT_DIR, "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(DATA_DIR, "processed_files_local.json")

# FAISS 向量库（Milvus 已退役 2026-07，向量引擎迁至 build_faiss_index.py 全量重建）
FAISS_DATA_DIR = os.path.abspath(os.path.join(OUTPUT_DIR, "..", "..", "5_分析结果", "faiss_data"))
FAISS_INDEX_PATH = os.path.join(FAISS_DATA_DIR, "reits_faiss.index")
FAISS_META_PATH = os.path.join(FAISS_DATA_DIR, "reits_faiss_meta.json")

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

# 2026-08-15 扩展: 用户决策 — 分红/收益分配类公告纳入 RAG 核心文档，并补录历史。
# 标题含「分红」即命中（如「分红款」罕见可接受）；「分红提示性公告」仍会被排除词拦截。
# 2026-10-04 修复(陷阱88): is_core_doc 与 classify_policy.json v2.1 脱钩——旧关键词表
#   不含「运营数据/扩募」等 core_lite 词，导致周六 4 条 core_lite 公告(508008/508069
#   运营数据、508055 扩募×2)建了目录却进不了 manifest，pipeline 永远看不见。
#   现改为: 命中旧关键词 或 classify() 判为 core_full/core_lite → 均算核心文档。
CORE_KEYWORDS = ["年度报告", "中期报告", "季度报告", "招募说明书", "分红", "收益分配", "评估报告"]
EXCLUDE_KEYWORDS = ["审计报告", "提示性"]

# 惰性加载分类器（与增量管道同源，规则唯一真相 = classify_policy.json v2.1）
_classifier = None

def _get_classifier():
    global _classifier
    if _classifier is None:
        try:
            import classify_announcements as C
            policy = C.load_policy()
            _classifier = lambda title: C.classify(title, policy)
        except Exception:
            _classifier = lambda title: (None, [])
    return _classifier

FLAGS = [
    "text_extracted",
    "table_detection_vector_done",
    "table_detection_scan_done",
    "table_describe_done",
    "not_table_describe_done",
    "merge_done",
    "text_segmentation",
    "embedding_done",
    "vector_database_done",
    "elasticsearch_database_done",
]


def is_core_doc(title: str) -> bool:
    """判断是否为核心文档类型（2026-10-04 起与 classify_policy v2.1 对齐）"""
    if not title:
        return False
    for kw in EXCLUDE_KEYWORDS:
        if kw in title:
            return False
    for kw in CORE_KEYWORDS:
        if kw in title:
            return True
    # 分类器兜底：core_full / core_lite 均算核心（含运营数据、扩募等增量词）
    label, _ = _get_classifier()(title)
    return label in ("core_full", "core_lite")


def check_file(path: str, min_size: int = 0) -> bool:
    """检查文件是否存在且大于最小值"""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > min_size
    except OSError:
        return False


def check_dir_has_content(path: str) -> bool:
    """检查目录是否存在且有内容（非空）"""
    try:
        if not os.path.isdir(path):
            return False
        return len(os.listdir(path)) > 0
    except OSError:
        return False


def detect_progress_from_files(doc_dir: str, meta: dict) -> dict:
    """
    文件驱动 + text.json.metadata 双源进度检测。
    返回 dict: {flag_name: bool}

    优先级: text.json.metadata > 磁盘文件存在性 > meta.json flag
    核心原则: 文件存在 → True；text.json.metadata 为权威源（6/17 meta覆写后唯一幸存）；已有 True 不降级。
    """
    # ═══ Phase A: 读取 text.json.metadata (权威源 — 未被覆写) ═══
    text_path = os.path.join(doc_dir, "text.json")
    text_metadata = {}
    if check_file(text_path, 100):
        try:
            with open(text_path, "r", encoding="utf-8") as f:
                text_json = json.load(f)
            if isinstance(text_json, dict):
                text_metadata = text_json.get("metadata", {}) or {}
        except Exception:
            pass

    # ═══ Phase B: 以 text.json.metadata 为权威源初始化 ═══
    # text.json.metadata 中存有管道写入的完整 step 标志 (merge_done, text_segmentation, embedding_done, etc.)
    # 这些标志在 6/17 meta.json 覆写事件中幸存
    progress = {}
    for flag in FLAGS:
        # 优先从 text.json.metadata 读取
        if flag in text_metadata:
            progress[flag] = bool(text_metadata[flag])
        # 其次从 meta.json 读取
        elif flag in meta:
            progress[flag] = bool(meta[flag])
        else:
            progress[flag] = False

    # ═══ Phase C: 文件驱动检测 (步骤1-4) ═══
    # 这些步骤的磁盘产物不会被覆写，可以直接从文件检测

    # --- Step 1: PDF渲染 ---
    if check_dir_has_content(os.path.join(doc_dir, "temp_pdf_images")):
        progress["text_extracted"] = True

    # --- Step 2: 文本提取 ---
    if check_file(text_path, 500):
        progress["text_extracted"] = True

    # --- Step 3_2: 表格扫描检测 ---
    if check_dir_has_content(os.path.join(doc_dir, "table_image")):
        progress["table_detection_scan_done"] = True
        progress["table_detection_vector_done"] = True

    # --- Step 4_1_1: LLM表格描述 ---
    td_path = os.path.join(doc_dir, "table_describe.json")
    if check_file(td_path, 100):
        progress["table_describe_done"] = True
        progress["text_extracted"] = True

    # --- Step 4_2_1: 非表格图片描述 ---
    ntd_path = os.path.join(doc_dir, "not_table_describe.json")
    if check_file(ntd_path, 100):
        progress["not_table_describe_done"] = True

    # --- Step 5: merge — text.json 中检测 merge_done 内容证据 ---
    # merge 后 text.json 会包含 table descriptions
    if check_file(text_path, 1000):
        try:
            with open(text_path, "r", encoding="utf-8") as f:
                text_content = f.read(10000)  # 读前10KB检测merge产物
            if "表格描述" in text_content or "table_description" in text_content.lower():
                if not progress.get("merge_done"):
                    progress["merge_done"] = True
        except Exception:
            pass

    # --- Step 6: 文本分段 — 检查 text_segmentation_embedding.json 存在性 ---
    emb_path = os.path.join(doc_dir, "text_segmentation_embedding.json")
    if check_file(emb_path, 1000):
        progress["text_segmentation"] = True
        progress["embedding_done"] = True
        progress["text_extracted"] = True

    # --- Step 7: 向量嵌入 — 同上，text_segmentation_embedding.json 即 embedding 产物 ---
    # (与 Step 6 共享同一个文件检测，text_segmentation_embedding.json 包含 chunks + embeddings)

    # 如果 merge_done=True，确保前置步骤也标记完成
    if progress.get("merge_done"):
        progress["table_describe_done"] = True
        progress["text_extracted"] = True

    return progress


def find_all_meta_files():
    """
    扫描 2层 + 3层 结构，找到所有 meta.json 文件。
    返回: {unique_key: (doc_dir, meta_dict)}
    unique_key = f"{fund_code}/{doc_name}"
    2层优先于3层 (新管道标准结构)。
    """
    found = {}  # unique_key -> (doc_dir, meta)

    for root, _, filenames in os.walk(DATA_DIR):
        if "meta.json" not in filenames:
            continue

        # 跳过非文档目录
        rel = os.path.relpath(root, DATA_DIR)
        parts = rel.split(os.sep)
        if len(parts) < 2:
            continue  # 太浅，跳过
        if parts[0] in ("manifest_backups", "log", "output", "page_images", "debug_output"):
            continue

        meta_path = os.path.join(root, "meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        title = meta.get("announcement_title", "")
        if not is_core_doc(title):
            continue

        # 确定 fund_code 和 doc_name
        if len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit():
            # 2层: fund_code/doc_name
            fund_code = parts[0]
            doc_name = parts[1]
        elif len(parts) == 3 and len(parts[0]) == 6 and parts[0].isdigit():
            # 3层: fund_code/doc_type/doc_name
            fund_code = parts[0]
            doc_name = parts[2]
        else:
            continue

        key = f"{fund_code}/{doc_name}"

        # 2层优先：如果 key 已存在且当前是2层，替换；如果是3层且已有2层，跳过
        layer = 2 if len(parts) == 2 else 3
        if key in found:
            existing_layer = found[key][2] if len(found[key]) > 2 else None
            if existing_layer == 2:
                continue  # 已有2层，跳过3层
            elif layer == 2:
                found[key] = (root, meta, 2)  # 2层覆盖3层
                continue

        found[key] = (root, meta, layer)

    return found


def rebuild():
    """主重建函数"""
    all_metas = find_all_meta_files()
    logging.info("扫描到 %d 个核心文档 (2层+3层合并去重)", len(all_metas))

    files_entry = {}
    skipped = 0

    for key, (doc_dir, meta, layer) in sorted(all_metas.items()):
        fund_code, doc_name = key.split("/", 1)

        # 文件驱动进度检测
        progress = detect_progress_from_files(doc_dir, meta)

        # 构建文件条目
        file_name = doc_name + ".pdf"
        entry = {
            "file_name": file_name,
            "file_path": meta.get("file_path", ""),
            "date": meta.get("date", ""),
            "fund_code": meta.get("fund_code", fund_code),
            "short_name": meta.get("short_name", ""),
            "announcement_title": meta.get("announcement_title", ""),
            "doc_type_1": meta.get("doc_type_1", ""),
            "doc_type_2": meta.get("doc_type_2", ""),
            "announcement_link": meta.get("announcement_link", ""),
            "_layer": layer,
        }
        for flag in FLAGS:
            entry[flag] = progress.get(flag, False)

        files_entry[file_name] = entry

    # 构建 summary
    codes = set(e.get("fund_code", "") for e in files_entry.values())
    step_counts = Counter()
    for e in files_entry.values():
        for flag in FLAGS:
            if e.get(flag):
                step_counts[flag] += 1

    import datetime
    manifest = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v3 (file-driven)",
        "source_dir": "",
        "output_dir": DATA_DIR,
        "directory_structure": "auto-detected (2-layer + 3-layer merge)",
        "summary": {
            "total": len(files_entry),
            "reits": len(codes),
            **{flag: step_counts.get(flag, 0) for flag in FLAGS},
        },
        "files": files_entry,
    }

    # 备份旧 manifest
    backup_dir = os.path.join(DATA_DIR, "manifest_backups")
    os.makedirs(backup_dir, exist_ok=True)
    if os.path.exists(MANIFEST_FILE):
        backup_name = f"processed_files_local.{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        os.rename(MANIFEST_FILE, os.path.join(backup_dir, backup_name))
        logging.info("旧 manifest 已备份: %s", backup_name)

    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 输出统计
    logging.info("=" * 60)
    logging.info("📊 Manifest v3 重建完成")
    logging.info("  核心文档: %d (覆盖 %d REITs)", len(files_entry), len(codes))
    logging.info("  2层: %d / 3层: %d",
                 sum(1 for e in files_entry.values() if e.get("_layer") == 2),
                 sum(1 for e in files_entry.values() if e.get("_layer") == 3))
    logging.info("-" * 60)
    for flag in FLAGS:
        cnt = step_counts.get(flag, 0)
        pct = cnt / len(files_entry) * 100 if files_entry else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        logging.info("  %-35s %4d/%d (%5.1f%%) %s", flag, cnt, len(files_entry), pct, bar)

    # 交叉校验
    _cross_validate(files_entry)


def _faiss_n_vectors() -> int:
    """读取 faiss meta 头部 n_vectors，不加载全量 id_map。"""
    with open(FAISS_META_PATH, "r", encoding="utf-8") as f:
        head = f.read(4096)
    m = re.search(r'"n_vectors"\s*:\s*(\d+)', head)
    return int(m.group(1)) if m else 0


def _faiss_stream_codes() -> set:
    """流式扫描 meta id_map 提取唯一 fund_code（约 5s / 1.2GB）。"""
    codes = set()
    with open(FAISS_META_PATH, "r", encoding="utf-8") as f:
        for line in f:
            m = re.search(r'^\s*"code":\s*"(\d{6})"', line)
            if m:
                codes.add(m.group(1))
    return codes


def _cross_validate(files_entry: dict):
    """双向交叉校验: manifest ↔ ES ↔ FAISS（Milvus 已退役）"""
    import urllib.request

    es_false_positive = []
    mv_false_positive = []

    # ES 校验
    try:
        req = urllib.request.Request(
            'http://localhost:9200/reits_announcements/_search',
            data=json.dumps({
                'size': 0,
                'aggs': {'by_fund': {'terms': {'field': 'fund_code', 'size': 200}}}
            }).encode(),
            headers={'Content-Type': 'application/json'})
        es_data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        es_codes = set(b['key'] for b in es_data['aggregations']['by_fund']['buckets'])
        es_manifest_codes = set()
        for e in files_entry.values():
            if e.get('elasticsearch_database_done'):
                es_manifest_codes.add(e.get('fund_code', ''))
        es_fp = es_manifest_codes - es_codes
        es_fn = es_codes - es_manifest_codes
        if es_fp:
            logging.warning("  🔴 ES假阳性: %d REITs — %s", len(es_fp), ", ".join(sorted(es_fp)[:10]))
        if es_fn:
            logging.info("  🟡 ES假阴性: %d REITs (ES有但manifest未标记)", len(es_fn))
        if not es_fp and not es_fn:
            logging.info("  ✅ ES 交叉校验通过")
    except Exception as e:
        logging.warning("  ⚠️ ES 校验跳过: %s", e)

    # FAISS 校验（Milvus 已退役 → FAISS 口径；不可用静默跳过，不阻断）
    try:
        if not os.path.exists(FAISS_INDEX_PATH):
            raise FileNotFoundError(FAISS_INDEX_PATH)
        n_vec = _faiss_n_vectors()
        if n_vec <= 0:
            raise ValueError(f"FAISS meta n_vectors={n_vec} 异常")
        faiss_codes = _faiss_stream_codes()
        mv_manifest_codes = set()
        for e in files_entry.values():
            if e.get('vector_database_done'):
                mv_manifest_codes.add(e.get('fund_code', ''))
        faiss_fp = mv_manifest_codes - faiss_codes
        faiss_fn = faiss_codes - mv_manifest_codes
        if faiss_fp:
            logging.warning("  🔴 FAISS假阳性: %d REITs — %s", len(faiss_fp), ", ".join(sorted(faiss_fp)[:10]))
        if faiss_fn:
            logging.info("  🟡 FAISS假阴性: %d REITs (FAISS有但manifest未标记)", len(faiss_fn))
        if not faiss_fp and not faiss_fn:
            logging.info("  ✅ FAISS 交叉校验通过: %d 向量, %d REITs", n_vec, len(faiss_codes))
    except Exception:
        pass  # FAISS 不可用 → 静默，不阻断


if __name__ == "__main__":
    rebuild()
