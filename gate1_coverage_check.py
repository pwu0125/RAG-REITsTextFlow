#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GATE1 · 信息提取覆盖率检查（独立脚本）

用法:
    python gate1_coverage_check.py <batch_name>
    python gate1_coverage_check.py B1a

检查逻辑:
    对批次内每篇核心文档，验证以下三项全部满足:
      a) meta.json 中 table_describe_done=true  OR  table_describe.json 存在且有内容
      b) meta.json 中 not_table_describe_done=true  OR  not_table_describe.json 存在且有内容
      c) text.json 存在且 > 1KB

    覆盖率 = 通过数 / 总文档数
    - >= 99% → exit 0 (PASS)
    - <  99% → exit 1 (FAIL)

批次码来源:
    1. BATCH_CONFIG.json（优先，格式简洁）
    2. RAG_BATCH_TRACKER.json（回退，标准 tracker）
"""

import json
import os
import sys
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


# ═══════════════════════════════════════════════════════════
# 工具函数
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


def check_single_doc(code: str, dirname: str) -> tuple:
    """
    检查单篇文档的三项条件。
    返回: (passed: bool, missing: list[str])
    
    检查项:
      a) table_describe_done=true  OR  table_describe.json 存在且有内容
      b) not_table_describe_done=true  OR  not_table_describe.json 存在且有内容
      c) text.json 存在且 > 1KB
    """
    doc_dir = os.path.join(DATA_DIR, code, dirname)
    missing = []

    # ── a) table_describe 检查 ──
    meta = _safe_read_json(os.path.join(doc_dir, "meta.json")) or {}
    table_flag = meta.get("table_describe_done", False)

    table_json_path = os.path.join(doc_dir, "table_describe.json")
    table_json = _safe_read_json(table_json_path)
    table_file_ok = bool(table_json)

    if not table_flag and not table_file_ok:
        missing.append("table_describe")

    # ── b) not_table_describe 检查 ──
    not_table_flag = meta.get("not_table_describe_done", False)

    not_table_json_path = os.path.join(doc_dir, "not_table_describe.json")
    not_table_json = _safe_read_json(not_table_json_path)
    not_table_file_ok = bool(not_table_json)

    if not not_table_flag and not not_table_file_ok:
        missing.append("not_table_describe")

    # ── c) text.json 存在且 > 1KB ──
    text_path = os.path.join(doc_dir, "text.json")
    if not os.path.exists(text_path):
        missing.append("text.json(缺失)")
    elif os.path.getsize(text_path) <= 1024:
        missing.append("text.json(≤1KB)")

    passed = len(missing) == 0
    return passed, missing


# ═══════════════════════════════════════════════════════════
# 主检查逻辑
# ═══════════════════════════════════════════════════════════

def run_gate1(batch_name: str) -> bool:
    """
    执行 GATE1 覆盖率检查。
    返回: True (通过) / False (失败)
    """
    print(f"\n{'='*60}")
    print(f"  GATE1 · 信息提取覆盖率检查")
    print(f"  批次: {batch_name}")
    print(f"{'='*60}")

    # 1) 获取批次码
    batch_codes = get_batch_codes(batch_name)
    print(f"  REITs: {batch_codes}")

    # 2) 筛选批次文档
    batch_docs = get_batch_docs(batch_codes)
    total = len(batch_docs)

    if total == 0:
        print(f"\n  ⚠️  批次 {batch_name} 中没有找到文档（manifest 可能未更新）")
        _log_gate("GATE1_coverage", False, {
            "batch": batch_name,
            "error": "no_docs_in_batch",
            "batch_codes": batch_codes,
        })
        return False

    # 3) 逐篇检查
    results = []
    missing_details = []

    for fn, code, dirname in batch_docs:
        passed, missing = check_single_doc(code, dirname)
        results.append(passed)

        if not passed:
            missing_details.append(
                f"  {code} | {fn[:80]}... 缺少: {', '.join(missing)}"
            )

    # 4) 统计并输出
    passed_count = sum(results)
    coverage_pct = round(passed_count / total * 100, 2) if total > 0 else 0.0

    print(f"\n  📊 检查项:")
    print(f"     a) table_describe_done=true OR table_describe.json 有内容")
    print(f"     b) not_table_describe_done=true OR not_table_describe.json 有内容")
    print(f"     c) text.json 存在且 > 1KB")
    print(f"\n  📊 覆盖率: {passed_count}/{total} ({coverage_pct}%)")

    if missing_details:
        print(f"\n  ⚠️  未通过 {len(missing_details)} 篇:")
        for detail in missing_details[:20]:
            print(detail)
        if len(missing_details) > 20:
            print(f"  ... 还有 {len(missing_details) - 20} 篇")

    gate_passed = coverage_pct >= 99.0
    status = "✅ GATE1 PASSED" if gate_passed else "❌ GATE1 FAILED"
    print(f"\n  {status}: {passed_count}/{total} ({coverage_pct}%)  [阈值: ≥99%]")

    if not gate_passed:
        print(f"  📌 图片已保留，修复后可重新运行 GATE1 检查。")

    # 5) 写日志
    _log_gate("GATE1_coverage", gate_passed, {
        "batch": batch_name,
        "batch_codes": batch_codes,
        "passed": passed_count,
        "total": total,
        "coverage_pct": coverage_pct,
        "missing_count": len(missing_details),
        "check_items": "table_describe_done|not_table_describe_done|text.json>1KB",
    })

    return gate_passed


def main():
    if len(sys.argv) < 2:
        print("用法: python gate1_coverage_check.py <批次名>")
        print("示例: python gate1_coverage_check.py B1a")
        sys.exit(2)

    batch_name = sys.argv[1]

    try:
        passed = run_gate1(batch_name)
    except Exception as e:
        print(f"\n❌ GATE1 检查异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
