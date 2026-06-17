#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GATE2 · ES 入库准确率检查

用法:
    python gate2_accuracy_check.py <batch_name>

检查逻辑:
    1. 文档覆盖率 — 批次内每个文档在 ES 中是否有记录（按 source_file 聚合计数）
    2. 字段完整性 — 随机抽样 5% chunks 检查 text/fund_code/date 必须非空
    3. 准确率 — (通过检查的文档数 + 通过的完好 chunk 数) / (总文档 + 抽样 chunks) ≥ 99.5%
"""

import json
import os
import random
import sys
import time
from datetime import datetime

# ── 路径配置 ──────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "announcement_document_processing_local")
MANIFEST_FILE = os.path.join(DATA_DIR, "processed_files_local.json")
BATCH_CONFIG_FILE = os.path.join(SCRIPT_DIR, "BATCH_CONFIG.json")
TRACKER_FILE = os.path.join(
    os.path.dirname(SCRIPT_DIR), "RAG_BATCH_TRACKER.json"
)
GATE_LOG = os.path.join(SCRIPT_DIR, "log", "quality_gate.log")

ES_INDEX = "reits_announcements"
ES_HOST = os.environ.get("ES_HOST", "127.0.0.1")
ES_PORT = os.environ.get("ES_PORT", "9200")
ES_SCHEME = os.environ.get("ES_SCHEME", "http")
ES_USERNAME = os.environ.get("ES_USERNAME", "elastic")
ES_PASSWORD = os.environ.get("ES_PASSWORD", "")

SAMPLE_RATIO = 0.05      # 抽样比例
ACCURACY_THRESHOLD = 99.5  # 准确率阈值


# ═══════════════════════════════════════════════════════════
# 工具函数（保留原桩代码中的）
# ═══════════════════════════════════════════════════════════

def _safe_read_json(path: str):
    """安全读取 JSON 文件，不存在或解析失败返回 None"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    return None


def _log_gate(gate_name: str, passed: bool, details: dict):
    """追加质检日志"""
    os.makedirs(os.path.dirname(GATE_LOG), exist_ok=True)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "gate": gate_name,
        "passed": passed,
        "details": details,
    }
    with open(GATE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def get_batch_codes(batch_name: str) -> list:
    """
    获取批次对应的 fund_code 列表。
    优先从 BATCH_CONFIG.json 读取，回退到 RAG_BATCH_TRACKER.json。
    """
    # 1) 尝试 BATCH_CONFIG.json
    batch_config = _safe_read_json(BATCH_CONFIG_FILE)
    if batch_config and batch_name in batch_config:
        entry = batch_config[batch_name]
        if isinstance(entry, dict) and "codes" in entry:
            return entry["codes"]

    # 2) 回退到 RAG_BATCH_TRACKER.json
    tracker = _safe_read_json(TRACKER_FILE)
    if tracker:
        batch = tracker.get("batches", {}).get(batch_name)
        if batch and "reits" in batch:
            return [r["code"] for r in batch["reits"] if "code" in r]

    print(f"❌ 批次 {batch_name} 未找到（BATCH_CONFIG.json 和 RAG_BATCH_TRACKER.json 均无）")
    sys.exit(2)


def get_batch_docs(batch_codes: list) -> list:
    """
    从 manifest 中筛选属于批次的文档。
    返回: [(manifest_key, fund_code, dirname), ...]

    注意: manifest key 带有 .pdf 后缀，目录名不包含后缀。
    """
    manifest = _safe_read_json(MANIFEST_FILE)
    if not manifest:
        print("❌ 无法读取 manifest")
        return []

    files = manifest.get("files", {})
    batch_docs = []

    for fn, entry in files.items():
        if not isinstance(entry, dict):
            continue
        code = entry.get("fund_code", "")
        if code not in batch_codes:
            continue
        # manifest key: "180101-xxx-2021-05-20.pdf"
        # 目录名:      "180101-xxx-2021-05-20"  (去掉 .pdf)
        dirname = os.path.splitext(fn)[0]
        batch_docs.append((fn, code, dirname))

    return batch_docs


# ═══════════════════════════════════════════════════════════
# ES 连接
# ═══════════════════════════════════════════════════════════

def _get_es_client():
    """延迟加载 ES 客户端，避免 import 时连接失败"""
    import base64
    from elasticsearch import Elasticsearch

    es_kwargs = {"verify_certs": False}
    if ES_USERNAME and ES_PASSWORD:
        token = base64.b64encode(
            f"{ES_USERNAME}:{ES_PASSWORD}".encode("utf-8")
        ).decode("ascii")
        es_kwargs["headers"] = {"Authorization": f"Basic {token}"}

    es_url = f"{ES_SCHEME}://{ES_HOST}:{ES_PORT}"
    return Elasticsearch([es_url], **es_kwargs)


# ═══════════════════════════════════════════════════════════
# 检查 1: 文档覆盖率（按 source_file 聚合）
# ═══════════════════════════════════════════════════════════

def _check_document_coverage(es, batch_codes: list, expected_docs: set) -> dict:
    """
    查询 ES 中批次文档的 source_file 聚合，检查覆盖率。
    返回: {
        "es_total_chunks": int,
        "es_unique_files": int,
        "expected_docs": int,
        "missing_docs": list[str],
        "covered_docs": int,
        "coverage_pct": float,
    }
    """
    # 用 fund_code 过滤 + source_file 聚合
    query = {
        "size": 0,
        "track_total_hits": True,  # 获取精确总数，避免 ES 默认 10000 截断
        "query": {
            "terms": {"fund_code": batch_codes}
        },
        "aggs": {
            "by_source": {
                "terms": {
                    "field": "source_file",
                    "size": 10000,  # 批次文档数最多 ~140
                }
            }
        }
    }

    try:
        resp = es.search(index=ES_INDEX, body=query)
    except Exception as e:
        print(f"  ❌ ES 查询失败: {e}")
        return {
            "es_total_chunks": 0,
            "es_unique_files": 0,
            "expected_docs": len(expected_docs),
            "missing_docs": list(expected_docs),
            "covered_docs": 0,
            "coverage_pct": 0.0,
            "error": str(e),
        }

    total_hits = resp["hits"]["total"]["value"]
    agg = resp.get("aggregations", {}).get("by_source", {})
    buckets = agg.get("buckets", [])

    es_source_files = {b["key"] for b in buckets}

    # 对比 manifest 中的文档名（manifest key 带 .pdf，ES source_file 也带 .pdf）
    missing_docs = sorted(expected_docs - es_source_files)
    covered_docs = len(expected_docs) - len(missing_docs)
    coverage_pct = round(covered_docs / len(expected_docs) * 100, 2) if expected_docs else 0.0

    return {
        "es_total_chunks": total_hits,
        "es_unique_files": len(buckets),
        "expected_docs": len(expected_docs),
        "missing_docs": missing_docs,
        "covered_docs": covered_docs,
        "coverage_pct": coverage_pct,
        "source_file_buckets": buckets,  # 保留供抽样使用
    }


# ═══════════════════════════════════════════════════════════
# 检查 2: 字段完整性（随机抽样）
# ═══════════════════════════════════════════════════════════

def _sample_chunks_and_check(es, batch_codes: list, sample_size: int) -> dict:
    """
    从 ES 中随机抽样 chunks，检查 text/fund_code/date 是否非空。
    使用 random_score 做随机排序。

    返回: {
        "sampled": int,
        "passed": int,
        "failed": int,
        "failed_detail": list[dict],
    }
    """
    if sample_size <= 0:
        return {"sampled": 0, "passed": 0, "failed": 0, "failed_detail": []}

    # 使用 function_score + random_score 做随机抽样
    query = {
        "size": sample_size,
        "query": {
            "function_score": {
                "query": {
                    "terms": {"fund_code": batch_codes}
                },
                "random_score": {},
                "boost_mode": "replace",
            }
        },
        "_source": ["global_id", "text", "fund_code", "date", "source_file"],
    }

    try:
        resp = es.search(index=ES_INDEX, body=query)
    except Exception as e:
        print(f"  ⚠️ ES 随机抽样查询失败: {e}")
        return {"sampled": 0, "passed": 0, "failed": 0, "failed_detail": [{"error": str(e)}]}

    hits = resp["hits"]["hits"]
    passed = 0
    failed = 0
    failed_detail = []

    for hit in hits:
        src = hit["_source"]
        issues = []

        text = (src.get("text") or "").strip()
        fund_code = (src.get("fund_code") or "").strip()
        date = (src.get("date") or "").strip()

        if not text:
            issues.append("text 为空")
        if not fund_code:
            issues.append("fund_code 为空")
        if not date:
            issues.append("date 为空")

        if issues:
            failed += 1
            failed_detail.append({
                "global_id": src.get("global_id", ""),
                "source_file": src.get("source_file", "")[:80],
                "issues": issues,
            })
        else:
            passed += 1

    return {
        "sampled": len(hits),
        "passed": passed,
        "failed": failed,
        "failed_detail": failed_detail,
    }


# ═══════════════════════════════════════════════════════════
# 主检查逻辑
# ═══════════════════════════════════════════════════════════

def run_gate2(batch_name: str) -> bool:
    """
    执行 GATE2 准确率检查。

    检查项:
      1. 文档覆盖率 — 批次内每个文档在 ES 中是否有记录
      2. 字段完整性 — 随机抽样 5% chunks 检查 text/fund_code/date 非空
      3. 综合准确率 ≥ 99.5%

    返回: True (通过) / False (失败)
    """
    print(f"\n{'='*60}")
    print(f"  GATE2 · ES 入库准确率检查")
    print(f"  批次: {batch_name}")
    print(f"  索引: {ES_INDEX} @ {ES_HOST}:{ES_PORT}")
    print(f"{'='*60}")

    # 1) 获取批次码
    batch_codes = get_batch_codes(batch_name)
    print(f"  REITs: {batch_codes}")

    # 2) 获取批次文档列表（manifest）
    batch_docs = get_batch_docs(batch_codes)
    total_docs = len(batch_docs)
    expected_docs = {fn for fn, _, _ in batch_docs}

    if total_docs == 0:
        print(f"\n  ⚠️  批次 {batch_name} 中没有找到文档（manifest 可能未更新）")
        _log_gate("GATE2_accuracy", False, {
            "batch": batch_name,
            "error": "no_docs_in_batch",
            "batch_codes": batch_codes,
        })
        return False

    print(f"  manifest 文档数: {total_docs}")

    # 3) 连接 ES
    try:
        es = _get_es_client()
        if not es.ping():
            print(f"\n  ❌ 无法连接到 ES ({ES_HOST}:{ES_PORT})")
            _log_gate("GATE2_accuracy", False, {
                "batch": batch_name,
                "error": "es_unreachable",
                "es_host": f"{ES_HOST}:{ES_PORT}",
            })
            return False
    except Exception as e:
        print(f"\n  ❌ ES 连接失败: {e}")
        _log_gate("GATE2_accuracy", False, {
            "batch": batch_name,
            "error": "es_connection_error",
            "detail": str(e),
        })
        return False

    # 4) 文档覆盖率检查
    print(f"\n  ── 检查 1: 文档覆盖率 ──")
    cov = _check_document_coverage(es, batch_codes, expected_docs)
    print(f"  ES 批次 chunks 总数: {cov['es_total_chunks']}")
    print(f"  ES 中唯一 source_file 数: {cov['es_unique_files']}")
    print(f"  manifest 预期文档数: {cov['expected_docs']}")
    print(f"  已覆盖文档数: {cov['covered_docs']}")
    if cov["missing_docs"]:
        print(f"  ⚠️ 缺失 {len(cov['missing_docs'])} 篇文档:")
        for doc in cov["missing_docs"][:10]:
            print(f"     - {doc[:100]}")
        if len(cov["missing_docs"]) > 10:
            print(f"     ... 还有 {len(cov['missing_docs']) - 10} 篇")

    # 5) 字段完整性抽样检查
    es_total_chunks = cov["es_total_chunks"]
    sample_size = max(1, int(es_total_chunks * SAMPLE_RATIO))
    print(f"\n  ── 检查 2: 字段完整性（随机抽样 {sample_size}/{es_total_chunks} chunks, {SAMPLE_RATIO*100:.0f}%）──")

    sample = _sample_chunks_and_check(es, batch_codes, sample_size)
    print(f"  抽样: {sample['sampled']} chunks")
    print(f"  通过: {sample['passed']}")
    print(f"  失败: {sample['failed']}")
    if sample["failed_detail"]:
        print(f"  失败示例:")
        for fd in sample["failed_detail"][:5]:
            print(f"     - {fd['global_id'][:60]}: {', '.join(fd['issues'])}")
        if len(sample["failed_detail"]) > 5:
            print(f"     ... 还有 {len(sample['failed_detail']) - 5} 个")

    # 6) 计算综合准确率
    # 公式: (通过检查的文档数 + 通过的完好 chunk 数) / (总文档 + 抽样 chunks) ≥ 99.5%
    passed_items = cov["covered_docs"] + sample["passed"]
    total_items = total_docs + sample["sampled"]
    accuracy_pct = round(passed_items / total_items * 100, 2) if total_items > 0 else 0.0

    print(f"\n  ── 综合准确率 ──")
    print(f"  文档覆盖: {cov['covered_docs']}/{total_docs}")
    print(f"  字段完整: {sample['passed']}/{sample['sampled']}")
    print(f"  综合: ({cov['covered_docs']} + {sample['passed']}) / ({total_docs} + {sample['sampled']})")
    print(f"  准确率: {accuracy_pct}%  [阈值: ≥{ACCURACY_THRESHOLD}%]")

    # 7) 判定
    gate_passed = accuracy_pct >= ACCURACY_THRESHOLD
    status = "✅ GATE2 PASSED" if gate_passed else "❌ GATE2 FAILED"
    print(f"\n  {status}")

    # 8) 写日志
    _log_gate("GATE2_accuracy", gate_passed, {
        "batch": batch_name,
        "batch_codes": batch_codes,
        "es_index": ES_INDEX,
        "es_total_chunks": cov["es_total_chunks"],
        "es_unique_files": cov["es_unique_files"],
        "expected_docs": total_docs,
        "covered_docs": cov["covered_docs"],
        "missing_docs_count": len(cov["missing_docs"]),
        "missing_docs": cov["missing_docs"][:20],
        "sample_size": sample["sampled"],
        "sample_passed": sample["passed"],
        "sample_failed": sample["failed"],
        "accuracy_pct": accuracy_pct,
        "threshold": ACCURACY_THRESHOLD,
        "check_items": "doc_coverage|field_completeness(text,fund_code,date)",
    })

    return gate_passed


def main():
    if len(sys.argv) < 2:
        print("用法: python gate2_accuracy_check.py <批次名>")
        print("示例: python gate2_accuracy_check.py B1a")
        sys.exit(2)

    batch_name = sys.argv[1]

    try:
        passed = run_gate2(batch_name)
    except Exception as e:
        print(f"\n❌ GATE2 检查异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
