#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sync_text_metadata.py — 交叉验证标志同步脚本

【问题背景 (BUG)】
step5_merge_table_into_text.py 中的 _infer_status_from_files() 函数存在一个
数据源覆盖顺序的 bug：
    1. 首先读取 meta.json → 获取正确的源标志 (source of truth)
    2. 然后读取 text.json['metadata'] → 用其覆盖 meta.json 的值

如果 text.json 中残留了旧的/过时的标志值（例如 `table_describe_done: false`，
而 meta.json 中已是 `true`），则 meta.json 的正确值会被 text.json 的过时值覆盖。
这直接导致 step5 的 `get_pending_files_from_local()` 把本应参与 merge 的文档错误跳过。

【解决方案】
本脚本在 step5 合并逻辑运行之前执行，以 meta.json 为唯一权威数据源，
将 text.json['metadata'] 中的关键标志同步为 meta.json 中的值。

【检查的标志】
- table_describe_done    (表格描述是否完成)
- not_table_describe_done (非表格描述是否完成)
- merge_done             (合并是否完成)
- text_extracted         (文本提取是否完成)

【用法】
    python sync_text_metadata.py B1a          # 同步 B1a 批次
    python sync_text_metadata.py B1b          # 同步 B1b 批次
    python sync_text_metadata.py B1a B1b B1c   # 同步多个批次

【退出码】
    0 — 所有文档标志已一致，无需修复
    1 — 发现并修复了不一致的标志（警告性退出，供调用方感知）
"""

import os
import sys
import json
import logging
from collections import defaultdict

from common_utils import safe_json_load, safe_json_dump
from file_paths_config import OUTPUT_DIR

# ———————————————————— 配置 ————————————————————

# Python 解释器路径（用于子进程调用，此处仅声明，实际子进程调用在 step5 中）
PYTHON = "/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python"

# 清单文件路径
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

# 批次配置文件（与 step5 共用）
BATCH_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "BATCH_CONFIG.json"
)

# 需要同步的标志列表（meta.json 中的键名）
# 这些标志同时存在于 meta.json 和 text.json['metadata'] 中
SYNC_FLAGS = [
    "table_describe_done",
    "not_table_describe_done",
    "merge_done",
    "text_extracted",
]

# ======================== 核心逻辑 ========================

def load_batch_config():
    """加载 BATCH_CONFIG.json，返回 batch_name → codes 的映射"""
    if not os.path.exists(BATCH_CONFIG_PATH):
        print(f"❌ 批次配置文件不存在: {BATCH_CONFIG_PATH}")
        sys.exit(2)
    with open(BATCH_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_manifest():
    """加载清单文件，返回 files 字典"""
    manifest = safe_json_load(MANIFEST_FILE)
    if not manifest:
        print(f"❌ 清单文件为空或不存在: {MANIFEST_FILE}")
        sys.exit(2)
    files_map = manifest.get("files", {})
    if not files_map:
        print(f"❌ 清单文件中没有 files 数据")
        sys.exit(2)
    return files_map


def get_batch_codes(batch_names):
    """
    根据批次名称获取该批次包含的 fund_code 列表。

    Args:
        batch_names: 批次名称列表，如 ["B1a"]

    Returns:
        list of fund_code strings, 如 ["180101", "180103", ...]
    """
    batch_config = load_batch_config()
    all_codes = set()
    for bn in batch_names:
        if bn not in batch_config:
            print(f"⚠️  警告: 批次 {bn} 不在 BATCH_CONFIG.json 中，跳过")
            continue
        codes = batch_config[bn].get("codes", [])
        all_codes.update(codes)
    return list(all_codes)


def filter_docs_by_batch(files_map, batch_codes):
    """
    根据批次代码筛选文档。

    Args:
        files_map: manifest 中的 files 字典
        batch_codes: 该批次包含的 fund_code 列表

    Returns:
        [(file_name, fund_code, pdf_folder_dir), ...]
    """
    result = []
    for file_name, file_info in files_map.items():
        if not isinstance(file_info, dict):
            continue
        fund_code = file_info.get("fund_code", "")
        if not fund_code or fund_code not in batch_codes:
            continue
        # 构建 pdf 文件夹路径
        pdf_folder_name = os.path.splitext(file_name)[0]
        pdf_folder_dir = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name)
        result.append((file_name, fund_code, pdf_folder_dir))
    return result


def sync_one_doc(file_name, fund_code, pdf_folder_dir):
    """
    同步单个文档的 text.json['metadata'] 与 meta.json。

    业务规则：
        meta.json 是唯一权威数据源（source of truth）。
        text.json['metadata'] 中的标志必须与 meta.json 一致。
        如果发现不一致，以 meta.json 为准修正 text.json。

    Args:
        file_name: 原始 PDF 文件名
        fund_code: 基金代码
        pdf_folder_dir: 文档文件夹路径

    Returns:
        dict: {
            "status": "ok" | "fixed" | "skipped_no_meta" | "skipped_no_text" | "error",
            "fixed_flags": [被修复的标志列表],
            "detail": 详情字符串,
        }
    """
    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    text_path = os.path.join(pdf_folder_dir, "text.json")

    # ----- 读取 meta.json -----
    meta_raw = safe_json_load(meta_path)
    if meta_raw is None:
        return {
            "status": "skipped_no_meta",
            "fixed_flags": [],
            "detail": f"meta.json 不存在或无法读取",
        }

    # ----- 读取 text.json -----
    text_raw = safe_json_load(text_path)
    if text_raw is None:
        return {
            "status": "skipped_no_text",
            "fixed_flags": [],
            "detail": f"text.json 不存在或无法读取",
        }

    if not isinstance(text_raw, dict):
        return {
            "status": "skipped_no_text",
            "fixed_flags": [],
            "detail": f"text.json 内容不是有效字典",
        }

    # ----- 确保 metadata 子字典存在 -----
    if "metadata" not in text_raw or not isinstance(text_raw.get("metadata"), dict):
        text_raw["metadata"] = {}

    text_meta = text_raw["metadata"]
    fixed_flags = []

    # ----- 逐标志对比并修复 -----
    for flag in SYNC_FLAGS:
        meta_val = meta_raw.get(flag)
        text_val = text_meta.get(flag)

        # 如果 meta.json 中没有该标志，跳过
        if meta_val is None:
            continue

        # 如果值完全一致（包括类型和内容），跳过
        if meta_val == text_val:
            continue

        # 不一致 → 以 meta.json 为准修复 text.json
        old_text_val = text_val
        text_meta[flag] = meta_val
        fixed_flags.append(
            f"{flag}: text.json={old_text_val} → meta.json={meta_val}"
        )

    # ----- 如果有修复，写回 text.json -----
    if fixed_flags:
        text_raw["metadata"] = text_meta
        safe_json_dump(text_raw, text_path)

    if fixed_flags:
        return {
            "status": "fixed",
            "fixed_flags": fixed_flags,
            "detail": f"修复 {len(fixed_flags)} 个不一致标志",
        }
    else:
        return {
            "status": "ok",
            "fixed_flags": [],
            "detail": "标志一致，无需修复",
        }


def sync_batch(batch_names):
    """
    同步指定批次的所有文档。

    Args:
        batch_names: 批次名称列表

    Returns:
        (total_checked, total_fixed, total_skipped, has_fixes)
    """
    # 1. 获取批次代码
    batch_codes = get_batch_codes(batch_names)
    if not batch_codes:
        print("❌ 没有有效的批次代码，退出")
        sys.exit(2)

    print(f"📋 批次 {batch_names} 包含 {len(batch_codes)} 个基金代码: {batch_codes}")

    # 2. 加载清单并筛选文档
    files_map = load_manifest()
    docs = filter_docs_by_batch(files_map, batch_codes)
    if not docs:
        print("❌ 没有找到属于该批次的文档")
        sys.exit(2)

    print(f"📄 该批次共 {len(docs)} 个文档，开始逐一检查标志一致性...\n")

    # 3. 逐文档同步
    stats = defaultdict(int)  # ok, fixed, skipped_no_meta, skipped_no_text, error
    fixed_details = []  # 记录修复详情

    for idx, (file_name, fund_code, pdf_folder_dir) in enumerate(docs, 1):
        # 检查文件夹是否存在
        if not os.path.isdir(pdf_folder_dir):
            stats["skipped_no_folder"] += 1
            continue

        result = sync_one_doc(file_name, fund_code, pdf_folder_dir)
        status = result["status"]
        stats[status] += 1

        if status == "fixed":
            for fix_info in result["fixed_flags"]:
                fixed_details.append(f"  [{file_name}] {fix_info}")
            print(f"  [{idx}/{len(docs)}] 🔧 修复: {file_name}")
            for fix_info in result["fixed_flags"]:
                print(f"           {fix_info}")
        elif status == "ok":
            # 无修复，静默（仅计数）
            if idx % 50 == 0 or idx == len(docs):
                print(f"  [{idx}/{len(docs)}] ✅ 已检查 {stats['ok']} 篇一致...")
        else:
            print(f"  [{idx}/{len(docs)}] ⚠️  跳过: {file_name} — {result['detail']}")

    # 4. 输出汇总
    total_checked = stats["ok"] + stats["fixed"]
    total_fixed = stats["fixed"]
    total_skipped = stats["skipped_no_meta"] + stats["skipped_no_text"] + stats.get("skipped_no_folder", 0)
    has_fixes = total_fixed > 0

    print(f"\n{'='*60}")
    print(f"  同步完成汇总")
    print(f"{'='*60}")
    print(f"  批次:         {batch_names}")
    print(f"  总文档数:     {len(docs)}")
    print(f"  已检查一致:   {stats['ok']}")
    print(f"  已修复:       {total_fixed}")
    print(f"  跳过(无文件): {total_skipped}")
    print(f"  错误:         {stats['error']}")
    print(f"{'='*60}")

    if has_fixes:
        print(f"\n  修复详情:")
        for detail in fixed_details:
            print(detail)
        print(f"\n  ⚠️  共修复 {total_fixed} 个文档的标志不一致。")
        print(f"  meta.json 作为权威数据源，text.json['metadata'] 已同步。")
    else:
        print(f"\n  ✅ 所有文档的 text.json 标志与 meta.json 一致，无需修复。")

    return total_checked, total_fixed, total_skipped, has_fixes


# ======================== 入口 ========================

def main():
    if len(sys.argv) < 2:
        print("用法: python sync_text_metadata.py <batch_name> [batch_name ...]")
        print("示例: python sync_text_metadata.py B1a")
        print("      python sync_text_metadata.py B1a B1b B1c")
        sys.exit(2)

    batch_names = sys.argv[1:]
    _, _, _, has_fixes = sync_batch(batch_names)

    # 退出码: 0=全部一致, 1=有修复（警告性退出）
    sys.exit(1 if has_fixes else 0)


if __name__ == "__main__":
    main()
