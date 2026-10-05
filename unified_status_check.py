#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
unified_status_check.py — 统一状态检测器（单向不可逆推理链）

设计原则:
  🔒 永不降级 — True 永远不改回 False（除非用户明确要求重置）
  🔗 最强锚定 → 次级锚定 → 磁盘扫描
  📦 新老文档统一入口
  🛡️ 默认 dry-run，需 --apply 才写入

推理链（从最强到最弱）:
  Phase 1: FAISS (R0) — 有 chunk  → step1→8_2 全部完成
  Phase 2: ES (R1)    — 有 chunk  → step1→8_1 全部完成
  Phase 3: Disk Scan   — 文件存在 → 逐步骤标记
  Phase 4: 反向推断    — 下游完成 → 上游一定完成
  Phase 5: 新文档发现  — 扫描目录注册
  Phase 6: 应用写入    — meta.json + text.json.metadata + manifest

用法:
  python unified_status_check.py              # 诊断模式 (dry-run)
  python unified_status_check.py --apply       # 应用模式 (写入)
  python unified_status_check.py --doc 508066  # 单 code 诊断
"""

import datetime
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from typing import Optional

# ═══════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(DATA_DIR, "processed_files_local.json")

# FAISS 向量库（Milvus 已退役 2026-07，向量引擎迁至 build_faiss_index.py 全量重建）
FAISS_DATA_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", "5_分析结果", "faiss_data"))
FAISS_INDEX_PATH = os.path.join(FAISS_DATA_DIR, "reits_faiss.index")
FAISS_META_PATH = os.path.join(FAISS_DATA_DIR, "reits_faiss_meta.json")

# 核心文档关键词（用于过滤噪音文档）
CORE_KEYWORDS = ["年度报告", "中期报告", "季度报告", "招募说明书"]
EXCLUDE_KEYWORDS = ["审计报告", "提示性"]

# 全部追踪标志
ALL_FLAGS = [
    "text_extracted",
    "table_detection_vector_done",
    "table_detection_scan_done",
    "table_describe_done",
    "not_table_describe_done",
    "merge_done",
    "text_segmentation",
    "embedding_done",
    "elasticsearch_database_done",
    "vector_database_done",
]

# FAISS 锚定 → 全部上游完成
FAISS_UPSTREAM = ALL_FLAGS[:]  # 全部

# ES 锚定 → step1→8_1 完成（不含 vector_database_done）
ES_UPSTREAM = [f for f in ALL_FLAGS if f != "vector_database_done"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("unified_check")


# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════
def safe_read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def check_file(path: str, min_size: int = 0) -> bool:
    try:
        return os.path.isfile(path) and os.path.getsize(path) > min_size
    except OSError:
        return False


def check_dir_has_content(path: str) -> bool:
    try:
        return os.path.isdir(path) and len(os.listdir(path)) > 0
    except OSError:
        return False


def is_core_doc(title: str) -> bool:
    if not title:
        return False
    for kw in EXCLUDE_KEYWORDS:
        if kw in title:
            return False
    for kw in CORE_KEYWORDS:
        if kw in title:
            return True
    return False


# ═══════════════════════════════════════════════
# FAISS 工具（替代已退役的 Milvus 查询）
# ═══════════════════════════════════════════════
_FAISS_LOADED = None  # (doc_names, codes) 缓存，避免同次运行重复扫描 1.2GB meta


def _faiss_load() -> tuple:
    """流式扫描 faiss meta id_map（约 5s / 1.2GB，进程内缓存一次）。
    返回 (doc_names, fund_codes)；FAISS 不可用抛异常。
    """
    global _FAISS_LOADED
    if _FAISS_LOADED is None:
        if not os.path.exists(FAISS_INDEX_PATH):
            raise FileNotFoundError(f"FAISS 索引不存在: {FAISS_INDEX_PATH}")
        doc_names = set()
        codes = set()
        with open(FAISS_META_PATH, "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r'^\s*"doc_name":\s*"(.*)",?\s*$', line)
                if m:
                    doc_names.add(m.group(1))
                    continue
                m = re.search(r'^\s*"code":\s*"(\d{6})"', line)
                if m:
                    codes.add(m.group(1))
        if not codes:
            raise ValueError("FAISS meta id_map 为空")
        _FAISS_LOADED = (doc_names, codes)
    return _FAISS_LOADED


def _faiss_n_vectors() -> int:
    """读取 faiss meta 头部 n_vectors，不加载全量 id_map。"""
    with open(FAISS_META_PATH, "r", encoding="utf-8") as f:
        head = f.read(4096)
    m = re.search(r'"n_vectors"\s*:\s*(\d+)', head)
    return int(m.group(1)) if m else 0


# ═══════════════════════════════════════════════
# Phase 1: FAISS 锚定 (R0 — 最强证据)
# ═══════════════════════════════════════════════
def phase1_faiss_anchor(manifest_files: dict) -> dict:
    """
    逐 source_file 查 FAISS meta id_map（doc_name 匹配）。
    有 chunk → 所有 10 个 flag 强制 True。
    """
    logger.info("═" * 50)
    logger.info("Phase 1: FAISS 锚定 (R0)")

    faiss_status = {}  # source_file → bool

    try:
        doc_names, _ = _faiss_load()
        logger.info("  FAISS 就绪: %s 向量, %d 文档",
                    f"{_faiss_n_vectors():,}", len(doc_names))
    except Exception as e:
        logger.warning("  ⚠️  FAISS 不可用: %s", e)
        logger.warning("  Phase 1 跳过 — 所有文档标记为\"未验证\"")
        return {}

    total = len(manifest_files)
    anchored = 0
    errors = 0

    for i, (source_file, info) in enumerate(manifest_files.items()):
        if (i + 1) % 200 == 0:
            logger.info("  FAISS 进度: %d/%d (锚定: %d)", i + 1, total, anchored)

        try:
            doc_name = os.path.splitext(source_file)[0]
            if doc_name in doc_names:
                faiss_status[source_file] = True
                anchored += 1
            else:
                faiss_status[source_file] = False
        except Exception:
            errors += 1
            faiss_status[source_file] = False  # 匹配失败 → treat as unverified

    logger.info("  ✅ FAISS 锚定完成: %d/%d 有数据, %d 匹配失败",
                anchored, total, errors)
    return faiss_status


# ═══════════════════════════════════════════════
# Phase 2: ES 锚定 (R1)
# ═══════════════════════════════════════════════
def phase2_es_anchor(manifest_files: dict, faiss_anchored: dict) -> dict:
    """
    批量查询 ES（一次 aggregation）。
    有 chunk → step1→8_1 全部完成。
    已被 FAISS 锚定的文档跳过（FAISS > ES）。
    """
    logger.info("═" * 50)
    logger.info("Phase 2: ES 锚定 (R1)")
    
    es_status = {}  # source_file → bool
    
    try:
        # 聚合查询：按 source_file.keyword 分组
        query = {
            "size": 0,
            "aggs": {
                "by_source": {
                    "terms": {
                        "field": "source_file",
                        "size": 10000,
                    }
                }
            },
        }
        req = urllib.request.Request(
            "http://localhost:9200/reits_announcements/_search",
            data=json.dumps(query).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        buckets = resp["aggregations"]["by_source"]["buckets"]
        es_source_counts = {b["key"]: b["doc_count"] for b in buckets}
        logger.info("  ES 聚合查询完成: %d 个 source_file 有数据", len(es_source_counts))
    except Exception as e:
        logger.warning("  ⚠️  ES 不可用: %s", e)
        logger.warning("  Phase 2 跳过")
        return {}

    anchored = 0
    skipped_mv = 0
    es_only = 0
    
    for source_file in manifest_files:
        # FAISS 已锚定 → 跳过
        if faiss_anchored.get(source_file):
            es_status[source_file] = True  # 已由更强证据锚定
            skipped_mv += 1
            continue
        
        if source_file in es_source_counts:
            es_status[source_file] = True
            anchored += 1
            es_only += 1
        else:
            es_status[source_file] = False

    logger.info("  ✅ ES 锚定: %d (跳过FAISS已锚定: %d, ES新锚定: %d)",
                anchored + skipped_mv, skipped_mv, es_only)
    return es_status


# ═══════════════════════════════════════════════
# Phase 3: 磁盘扫描 (R2-R8)
# ═══════════════════════════════════════════════
def phase3_disk_scan(manifest_files: dict, faiss_anchored: dict, es_anchored: dict) -> dict:
    """
    从磁盘文件逐文档检测进度。
    已被 FAISS/ES 锚定的文档跳过（已全部 True）。
    """
    logger.info("═" * 50)
    logger.info("Phase 3: 磁盘扫描")
    
    disk_progress = {}  # source_file → {flag: bool}
    skipped = 0
    scanned = 0
    
    for source_file, info in manifest_files.items():
        fund_code = (info or {}).get("fund_code", source_file.split("-")[0])
        doc_name = os.path.splitext(source_file)[0]
        doc_dir = os.path.join(DATA_DIR, fund_code, doc_name)
        
        # FAISS/ES 已锚定 → 全部 True
        if faiss_anchored.get(source_file) or es_anchored.get(source_file):
            disk_progress[source_file] = {f: True for f in ALL_FLAGS}
            skipped += 1
            continue
        
        scanned += 1
        progress = {f: False for f in ALL_FLAGS}
        
        # text.json → text_extracted
        text_path = os.path.join(doc_dir, "text.json")
        if check_file(text_path, 500):
            progress["text_extracted"] = True
        
        # table_image/ → scan_done + vector_done
        if check_dir_has_content(os.path.join(doc_dir, "table_image")):
            progress["table_detection_scan_done"] = True
            progress["table_detection_vector_done"] = True
        
        # table_describe.json → table_describe_done
        td_path = os.path.join(doc_dir, "table_describe.json")
        if check_file(td_path, 100):
            progress["table_describe_done"] = True
            progress["text_extracted"] = True  # 前置必完成
        
        # not_table_describe.json → ntd_done
        ntd_path = os.path.join(doc_dir, "not_table_describe.json")
        if check_file(ntd_path, 100):
            progress["not_table_describe_done"] = True
        
        # text_segmentation_embedding.json → embedding_done + text_segmentation
        emb_path = os.path.join(doc_dir, "text_segmentation_embedding.json")
        if check_file(emb_path, 1000):
            progress["embedding_done"] = True
            progress["text_segmentation"] = True
            progress["text_extracted"] = True
        
        # text.json 含表格描述 → merge 推断
        if check_file(text_path, 1024):
            try:
                with open(text_path, "r", encoding="utf-8") as f:
                    head = f.read(8192)
                if "表格描述" in head or "table_description" in head.lower():
                    progress["merge_done"] = True
            except Exception:
                pass
        
        disk_progress[source_file] = progress

    logger.info("  ✅ 磁盘扫描: %d 扫描, %d 跳过(FAISS/ES锚定)", scanned, skipped)
    return disk_progress


# ═══════════════════════════════════════════════
# Phase 4: 反向推断 (下游完成 → 上游一定完成)
# ═══════════════════════════════════════════════
def phase4_backward_inference(disk_progress: dict) -> None:
    """
    基于已知完成步骤，反向推断上游。
    例如: embedding_done → merge_done → table_describe_done → ...
    """
    logger.info("═" * 50)
    logger.info("Phase 4: 反向推断")
    
    inferences = 0
    for source_file, progress in disk_progress.items():
        before = sum(progress.values())
        
        # vector_database_done → embedding_done, text_segmentation, merge_done ↑
        if progress.get("vector_database_done"):
            progress["embedding_done"] = True
            progress["text_segmentation"] = True
            progress["merge_done"] = True
        
        # elasticsearch_database_done → merge_done, text_segmentation, embedding_done ↑
        if progress.get("elasticsearch_database_done"):
            progress["merge_done"] = True
            progress["text_segmentation"] = True
            progress["embedding_done"] = True
        
        # embedding_done → merge_done ↑
        if progress.get("embedding_done"):
            progress["merge_done"] = True
            progress["text_segmentation"] = True
        
        # merge_done → table_describe_done, ntd_done ↑
        if progress.get("merge_done"):
            progress["table_describe_done"] = True
            progress["not_table_describe_done"] = True
            progress["text_extracted"] = True
        
        # table_describe_done → scan_done, vector_done, text_extracted ↑
        if progress.get("table_describe_done"):
            progress["table_detection_scan_done"] = True
            progress["table_detection_vector_done"] = True
            progress["text_extracted"] = True
        
        after = sum(progress.values())
        if after > before:
            inferences += 1
    
    logger.info("  ✅ 反向推断完成: %d 篇文档扩展了上游标志", inferences)


# ═══════════════════════════════════════════════
# Phase 5: 新文档发现
# ═══════════════════════════════════════════════
def phase5_discover_new_docs(manifest_files: dict) -> list[dict]:
    """
    扫描磁盘目录，发现 manifest 中不存在的文档。
    返回新增条目列表。
    """
    logger.info("═" * 50)
    logger.info("Phase 5: 新文档发现")
    
    existing_keys = set(manifest_files.keys())
    new_entries = []
    
    for root, dirs, _ in os.walk(DATA_DIR):
        # 跳过备份/日志目录
        rel = os.path.relpath(root, DATA_DIR)
        parts = rel.split(os.sep)
        if len(parts) < 2:
            continue
        if parts[0] in ("manifest_backups", "log", "output", "meta_backups", "page_images"):
            continue
        if len(parts[0]) != 6 or not parts[0].isdigit():
            continue
        
        fund_code = parts[0]
        
        for d in dirs:
            meta_path = os.path.join(root, d, "meta.json")
            if not os.path.exists(meta_path):
                continue
            
            meta = safe_read_json(meta_path)
            if not meta:
                continue
            
            title = meta.get("announcement_title", "")
            if not is_core_doc(title):
                continue
            
            file_name = d + ".pdf"
            if file_name in existing_keys:
                continue
            
            entry = {
                "file_name": file_name,
                "file_path": meta.get("file_path", ""),
                "date": meta.get("date", ""),
                "fund_code": meta.get("fund_code", fund_code),
                "short_name": meta.get("short_name", ""),
                "announcement_title": title,
                "doc_type_1": meta.get("doc_type_1", ""),
                "doc_type_2": meta.get("doc_type_2", ""),
                "announcement_link": meta.get("announcement_link", ""),
                "_newly_discovered": True,
            }
            for flag in ALL_FLAGS:
                entry[flag] = False  # 新文档初始全 False
            
            new_entries.append(entry)
            logger.info("  🆕 新文档 [%s] %s", fund_code, title[:60])
    
    logger.info("  ✅ 发现 %d 个新文档", len(new_entries))
    return new_entries


# ═══════════════════════════════════════════════
# Phase 6: 应用写入
# ═══════════════════════════════════════════════
def phase6_apply(
    manifest_files: dict,
    faiss_anchored: dict,
    es_anchored: dict,
    disk_progress: dict,
    new_entries: list[dict],
) -> dict:
    """
    将检测结果写入三个目标:
    1. meta.json — 只改 False→True（永不降级）
    2. text.json.metadata — 同步
    3. processed_files_local.json — 完整 manifest
    """
    logger.info("═" * 50)
    logger.info("Phase 6: 应用写入")
    
    stats = Counter()
    
    # ── 6a: 合并新文档到 manifest_files ──
    for entry in new_entries:
        manifest_files[entry["file_name"]] = entry
    
    # ── 6b: 写入 meta.json + text.json.metadata ──
    for source_file, info in manifest_files.items():
        fund_code = (info or {}).get("fund_code", source_file.split("-")[0])
        doc_name = os.path.splitext(source_file)[0]
        doc_dir = os.path.join(DATA_DIR, fund_code, doc_name)
        
        if not os.path.isdir(doc_dir):
            continue
        
        # 确定最终标志（离散 → disk → ES → FAISS 优先级递增）
        final = disk_progress.get(source_file, {f: False for f in ALL_FLAGS})

        # FAISS 锚定覆盖
        if faiss_anchored.get(source_file):
            for f in FAISS_UPSTREAM:
                final[f] = True
        # ES 锚定覆盖
        elif es_anchored.get(source_file):
            for f in ES_UPSTREAM:
                final[f] = True
        
        # ── 写 meta.json (只改 False→True) ──
        meta_path = os.path.join(doc_dir, "meta.json")
        meta = safe_read_json(meta_path) or {}
        meta_changed = False
        for flag in ALL_FLAGS:
            if final.get(flag) and not meta.get(flag):
                meta[flag] = True
                meta_changed = True
                stats[f"meta_{flag}"] += 1
        
        if meta_changed:
            safe_write_json(meta_path, meta)
            stats["meta_files_updated"] += 1
        
        # ── 写 text.json.metadata (同步) ──
        text_path = os.path.join(doc_dir, "text.json")
        if os.path.exists(text_path):
            try:
                text = json.load(open(text_path))
                if "metadata" not in text:
                    text["metadata"] = {}
                text_changed = False
                for flag in ALL_FLAGS:
                    if final.get(flag) and not text["metadata"].get(flag):
                        text["metadata"][flag] = True
                        text_changed = True
                if text_changed:
                    json.dump(text, open(text_path, "w"), ensure_ascii=False)
                    stats["text_metadata_updated"] += 1
            except Exception:
                pass
    
    # ── 6c: 写 manifest ──
    manifest_output = {source_file: {} for source_file in manifest_files}
    for source_file, info in manifest_files.items():
        entry = {
            "file_name": source_file,
            "file_path": info.get("file_path", ""),
            "date": info.get("date", ""),
            "fund_code": info.get("fund_code", ""),
            "short_name": info.get("short_name", ""),
            "announcement_title": info.get("announcement_title", ""),
            "doc_type_1": info.get("doc_type_1", ""),
            "doc_type_2": info.get("doc_type_2", ""),
            "announcement_link": info.get("announcement_link", ""),
        }
        # 合并最终标志
        final = disk_progress.get(source_file, {f: False for f in ALL_FLAGS})
        if faiss_anchored.get(source_file):
            for f in FAISS_UPSTREAM:
                final[f] = True
        elif es_anchored.get(source_file):
            for f in ES_UPSTREAM:
                final[f] = True
        for flag in ALL_FLAGS:
            entry[flag] = final.get(flag, False)
        
        manifest_output[source_file] = entry
    
    # 构建 summary
    codes = set(e.get("fund_code", "") for e in manifest_output.values())
    step_counts = Counter()
    for e in manifest_output.values():
        for flag in ALL_FLAGS:
            if e.get(flag):
                step_counts[flag] += 1
    
    manifest = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "unified (faiss→es→disk→inference, never downgrade)",
        "output_dir": DATA_DIR,
        "summary": {
            "total": len(manifest_output),
            "reits": len(codes),
            **{flag: step_counts.get(flag, 0) for flag in ALL_FLAGS},
        },
        "files": manifest_output,
    }
    
    # 备份旧 manifest
    backup_dir = os.path.join(DATA_DIR, "manifest_backups")
    os.makedirs(backup_dir, exist_ok=True)
    if os.path.exists(MANIFEST_FILE):
        backup_name = f"processed_files_local.{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        shutil.move(MANIFEST_FILE, os.path.join(backup_dir, backup_name))
    
    safe_write_json(MANIFEST_FILE, manifest)
    stats["manifest_docs"] = len(manifest_output)
    stats["manifest_reits"] = len(codes)
    
    logger.info("  ✅ meta.json: %d 文件更新", stats["meta_files_updated"])
    logger.info("  ✅ text.json.metadata: %d 文件更新", stats["text_metadata_updated"])
    logger.info("  ✅ manifest: %d 文档, %d REITs", stats["manifest_docs"], stats["manifest_reits"])
    
    return dict(stats)


# ═══════════════════════════════════════════════
# Phase 7: 交叉验证报告
# ═══════════════════════════════════════════════
def phase7_cross_validate(manifest_files: dict) -> dict:
    """
    manifest ↔ ES ↔ disk 三层交叉验证（只读，不修改）。
    """
    logger.info("═" * 50)
    logger.info("Phase 7: 交叉验证报告")
    
    report = {"es_false_positive": [], "es_false_negative": [], "faiss_false_positive": []}
    
    # ── ES 交叉校验 ──
    try:
        query = {
            "size": 0,
            "aggs": {"by_fund": {"terms": {"field": "fund_code", "size": 200}}},
        }
        req = urllib.request.Request(
            "http://localhost:9200/reits_announcements/_search",
            data=json.dumps(query).encode(),
            headers={"Content-Type": "application/json"},
        )
        es_data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        es_codes = {b["key"] for b in es_data["aggregations"]["by_fund"]["buckets"]}
        
        manifest_es_codes = set()
        for e in manifest_files.values():
            if e.get("elasticsearch_database_done"):
                manifest_es_codes.add(e.get("fund_code", ""))
        
        es_fp = manifest_es_codes - es_codes
        es_fn = es_codes - manifest_es_codes
        
        if es_fp:
            logger.warning("  🔴 ES 假阳性 (manifest=True, ES=无): %d codes", len(es_fp))
            report["es_false_positive"] = sorted(es_fp)[:10]
        if es_fn:
            logger.info("  🟡 ES 假阴性 (manifest=False, ES=有): %d codes", len(es_fn))
            report["es_false_negative"] = sorted(es_fn)[:10]
        if not es_fp and not es_fn:
            logger.info("  ✅ ES 交叉校验 100% 一致")
    except Exception as e:
        logger.warning("  ⚠️  ES 交叉校验跳过: %s", e)
    
    # ── FAISS 交叉校验（Milvus 已退役 → FAISS 口径）──
    try:
        _, faiss_codes = _faiss_load()
        n_vec = _faiss_n_vectors()

        faiss_manifest_codes = set()
        for e in manifest_files.values():
            code = e.get("fund_code", "")
            if e.get("vector_database_done"):
                faiss_manifest_codes.add(code)

        faiss_fp = faiss_manifest_codes - faiss_codes
        faiss_fn = faiss_codes - faiss_manifest_codes

        if faiss_fp:
            logger.warning("  🔴 FAISS 假阳性 (manifest=True, FAISS=无): %d codes", len(faiss_fp))
            report["faiss_false_positive"] = sorted(faiss_fp)[:10]
        if faiss_fn:
            logger.info("  🟡 FAISS 假阴性 (manifest=False, FAISS=有): %d codes", len(faiss_fn))
        if not faiss_fp and not faiss_fn:
            logger.info("  ✅ FAISS 交叉校验通过: %d 向量, %d/%d codes 一致",
                        n_vec, len(faiss_codes), len(faiss_manifest_codes))
    except Exception as e:
        logger.warning("  ⚠️  FAISS 交叉校验跳过: %s", e)

    return report


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════
def run(apply_mode: bool = False, filter_code: Optional[str] = None):
    start_time = time.time()
    
    logger.info("=" * 60)
    logger.info("🔍 unified_status_check — 统一状态检测器")
    logger.info("  模式: %s", "APPLY (写入)" if apply_mode else "DIAGNOSE (只读)")
    logger.info("  推理链: FAISS(R0) → ES(R1) → Disk(R2-R8)")
    logger.info("  原则: 永不降级 (True 永远不改回 False)")
    if filter_code:
        logger.info("  过滤: code=%s", filter_code)
    logger.info("=" * 60)
    
    # ── 读取现有 manifest ──
    manifest = safe_read_json(MANIFEST_FILE) or {"files": {}}
    all_files = manifest.get("files", {})
    
    if filter_code:
        all_files = {k: v for k, v in all_files.items() if v.get("fund_code") == filter_code}
        if not all_files:
            logger.error("  ❌ 未找到 code=%s 的文档", filter_code)
            return
    
    logger.info("\n📋 现有 manifest: %d 文档\n", len(all_files))
    
    # ── Phase 1: FAISS 锚定 ──
    faiss_status = phase1_faiss_anchor(all_files)

    # ── Phase 2: ES 锚定 ──
    es_status = phase2_es_anchor(all_files, faiss_status)

    # ── Phase 3: 磁盘扫描 ──
    disk_progress = phase3_disk_scan(all_files, faiss_status, es_status)
    
    # ── Phase 4: 反向推断 ──
    phase4_backward_inference(disk_progress)
    
    # ── Phase 5: 新文档发现 ──
    new_entries = []
    if apply_mode and not filter_code:
        new_entries = phase5_discover_new_docs(all_files)
    
    # ── Print summary ──
    logger.info("\n" + "=" * 60)
    logger.info("📊 检测结果汇总")
    logger.info("=" * 60)
    
    final_counts = Counter()
    total = len(all_files)
    for source_file, progress in disk_progress.items():
        # Apply anchors
        if faiss_status.get(source_file):
            for f in FAISS_UPSTREAM:
                progress[f] = True
        elif es_status.get(source_file):
            for f in ES_UPSTREAM:
                progress[f] = True
        for flag in ALL_FLAGS:
            if progress.get(flag):
                final_counts[flag] += 1

    logger.info("  文档总数: %d", total)
    logger.info("  FAISS 锚定: %d", sum(1 for v in faiss_status.values() if v))
    logger.info("  ES 锚定: %d", sum(1 for v in es_status.values() if v))
    logger.info("-" * 60)
    for flag in ALL_FLAGS:
        cnt = final_counts.get(flag, 0)
        pct = 100.0 * cnt / total if total else 0
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        logger.info("  %-35s %s %4d/%d (%5.1f%%)", flag, bar, cnt, total, pct)
    logger.info("-" * 60)
    logger.info("  新发现文档: %d", len(new_entries))
    
    # ── Phase 6: 应用写入 ──
    if apply_mode:
        logger.info("\n🖊️  写入模式...\n")
        stats = phase6_apply(all_files, faiss_status, es_status, disk_progress, new_entries)
        logger.info("\n  写入统计: %s", json.dumps(stats, ensure_ascii=False, indent=2))
    
    # ── Phase 7: 交叉验证 ──
    if not filter_code:
        phase7_cross_validate(all_files)
    
    elapsed = time.time() - start_time
    logger.info("\n⏱️  总耗时: %.1f 秒\n", elapsed)
    
    if not apply_mode:
        logger.info("💡 这是 DRY-RUN 模式。要写入，运行: python unified_status_check.py --apply")


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    filter_code = None
    for arg in sys.argv:
        if arg.startswith("--doc=") or arg.startswith("--code="):
            filter_code = arg.split("=", 1)[1]
    run(apply_mode=apply_mode, filter_code=filter_code)
