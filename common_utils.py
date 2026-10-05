#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
common_utils.py - 通用工具模块

提供统一的JSON序列化功能，解决datetime对象序列化问题。
v2.1: 新增抗踩坑防护函数（2026-07-22）
  - verify_pdf_dir_populated()  → 防止链入错误目录（坑#1）
  - normalize_manifest_keys()   → 防止key格式不匹配（坑#2）
  - get_codes_from_env_or_config() → 统一BATCH_CODES/BATCH_CONFIG（坑#3）
"""

import json
import datetime
import os
import sys
from typing import Any

class SafeJSONEncoder(json.JSONEncoder):
    """安全的JSON编码器，处理各种Python对象序列化"""
    
    def default(self, obj: Any) -> Any:
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.strftime('%Y-%m-%d')
        elif isinstance(obj, datetime.time):
            return obj.strftime('%H:%M:%S')
        elif hasattr(obj, '__dict__'):
            # 处理自定义对象
            return obj.__dict__
        return super().default(obj)

def safe_json_dump(data: Any, file_path: str, **kwargs) -> None:
    """安全的JSON文件写入"""
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, cls=SafeJSONEncoder, ensure_ascii=False, indent=2, **kwargs)

def safe_json_dumps(data: Any, **kwargs) -> str:
    """安全的JSON字符串序列化"""
    return json.dumps(data, cls=SafeJSONEncoder, ensure_ascii=False, **kwargs)

def safe_json_load(file_path: str) -> Any:
    """安全的JSON文件读取"""
    with open(file_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def safe_json_loads(json_str: str) -> Any:
    """安全的JSON字符串反序列化"""
    return json.loads(json_str)


# ═══════════════════════════════════════════════════════════
# Batch-aware manifest filtering
# ═══════════════════════════════════════════════════════════

BATCH_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "BATCH_CONFIG.json")


def _safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _parse_batch_codes_str(codes_str: str):
    """解析逗号分隔的 code 字符串，支持 JSON 数组格式。

    修复（2026-08-25）: 裸数字如 `--batch-codes 180101` 会被 json.loads 解析成 int，
    导致下游 set(batch_codes) 抛 "'int' object is not iterable"。统一归一化为字符串列表。
    """
    if not codes_str or not codes_str.strip():
        return None
    codes_str = codes_str.strip()
    try:
        result = json.loads(codes_str)
    except Exception:
        return [c.strip() for c in codes_str.split(",") if c.strip()]
    if isinstance(result, list):
        return [str(c).strip() for c in result if str(c).strip()]
    if isinstance(result, (int, str)):
        return [str(result).strip()]
    return None


def get_batch_codes_from_env():
    """优先从 CLI --batch-codes 参数读取；fallback 到 BATCH_CODES 环境变量。

    用法：
      python step4_1_1.py --batch-codes 508066,508028,180601
      python step4_1_1.py --batch-codes '["508066","508028","180601"]'
    """
    # 1) CLI arg: --batch-codes
    try:
        if "--batch-codes" in sys.argv:
            idx = sys.argv.index("--batch-codes")
            if idx + 1 < len(sys.argv):
                result = _parse_batch_codes_str(sys.argv[idx + 1])
                if result:
                    return result
    except Exception:
        pass

    # 2) Fallback: BATCH_CODES 环境变量
    return _parse_batch_codes_str(os.environ.get("BATCH_CODES", ""))


def filter_files_by_batch(files_map: dict, batch_codes: list) -> dict:
    """只保留属于 batch_codes 的文档。"""
    code_set = set(batch_codes)
    return {
        k: v for k, v in files_map.items()
        if (v or {}).get("fund_code", "") in code_set
    }


def get_doc_from_cli():
    """从 CLI --doc 参数读取目标文档名（支持文件名或子串匹配）。"""
    try:
        if "--doc" in sys.argv:
            idx = sys.argv.index("--doc")
            if idx + 1 < len(sys.argv):
                return sys.argv[idx + 1].strip()
    except Exception:
        pass
    return None


def filter_files_by_doc(files_map: dict, doc_pattern: str) -> dict:
    """只保留文件名包含 doc_pattern 的文档。"""
    return {
        k: v for k, v in files_map.items()
        if doc_pattern in k
    }


def filter_manifest_files_by_env(files_map: dict) -> dict:
    """优先级：--doc > --batch-codes > BATCH_CODES 环境变量 > 全量。
    
    用法：
      python step4_1_1.py --doc "508066-华泰紫金...-2022-05-06.pdf"
      python step4_1_1.py --doc "2022-05-06"   # 子串匹配
      python step4_1_1.py --batch-codes 508066,508028
    """
    # 1) --doc 单文档（最高优先级）
    doc = get_doc_from_cli()
    if doc:
        return filter_files_by_doc(files_map, doc)
    
    # 2) --batch-codes / env
    codes = get_batch_codes_from_env()
    if codes is None:
        return files_map
    return filter_files_by_batch(files_map, codes)


def is_manifest_filtered() -> bool:
    """Return True if --doc or --batch-codes filtering is active (CLI or env)."""
    if get_doc_from_cli():
        return True
    if get_batch_codes_from_env():
        return True
    return False


# ═══════════════════════════════════════════════════════════
# 抗踩坑防护函数（2026-07-22）
# ═══════════════════════════════════════════════════════════

def verify_pdf_dir_populated(pdf_dir: str = None, min_files: int = 1) -> bool:
    """【坑#1防护】验证PDF_DIR存在且包含至少min_files个PDF/symlink。

    调用位置：各步骤process_pdfs/main，处理文件之前调用。
    如失败，打印明确的错误信息（包含目录路径），并返回False。

    用法：
      from file_paths_config import PDF_DIR
      if not verify_pdf_dir_populated(PDF_DIR, min_files=10):
          print("💡 提示：PDF文件应在 _flat_pdfs/ 目录，确认已链接。"
                "如需从 REITs_notice/ 链接，使用 link_pdfs_to_flat.py")
    """
    if pdf_dir is None:
        from file_paths_config import PDF_DIR as _pdf_dir
        pdf_dir = _pdf_dir

    if not os.path.exists(pdf_dir):
        print(f"❌ PDF_DIR 不存在: {pdf_dir}")
        print(f"   请检查 file_paths_config.py 中 PDF_DIR 配置")
        return False

    pdf_count = sum(
        1 for f in os.listdir(pdf_dir)
        if f.lower().endswith('.pdf')
    )
    symlink_count = sum(
        1 for f in os.listdir(pdf_dir)
        if os.path.islink(os.path.join(pdf_dir, f)) and f.lower().endswith('.pdf')
    )

    print(f"📂 PDF_DIR: {pdf_dir}")
    print(f"   PDF文件: {pdf_count} | Symlink: {symlink_count}")

    if pdf_count < min_files:
        print(f"❌ PDF文件数量 ({pdf_count}) 少于最低要求 ({min_files})")
        print(f"   请确认PDF已正确链接到此目录")
        return False

    return True


def normalize_manifest_keys(files_map: dict) -> tuple:
    """【坑#2防护】验证并修正manifest key格式。

    Step2/Step3等步骤用PDF文件名（如 "code-xxx-2024-01-01.pdf"）比对白名单。
    但手动构造的manifest key可能是 "code/doc_name" 格式，导致白名单漏过。

    返回: (corrected_map, fix_count, error_samples)
    - corrected_map: 修正后的files映射
    - fix_count: 修正了多少条
    - error_samples: 无法修正的条目列表

    检测规则：
    1. key含 "/" → 尝试提取 {code}/{doc_name} → new_key = doc_name + ".pdf"
    2. key无 ".pdf" → 追加
    3. key不匹配 ^\\d{6}-.+$ 模式 → 记录但不确定如何修正
    """
    import re
    corrected = {}
    fix_count = 0
    error_samples = []

    for key, value in files_map.items():
        new_key = key

        # Rule 1: Fix code/doc_name → doc_name.pdf
        if '/' in key:
            parts = key.split('/', 1)
            if len(parts) == 2:
                new_key = parts[1] + '.pdf'
                fix_count += 1

        # Rule 2: Missing .pdf extension
        elif not key.endswith('.pdf'):
            new_key = key + '.pdf'
            fix_count += 1

        # Rule 3: Validate PDF filename pattern
        if not re.match(r'^\d{6}-.+\.pdf$', new_key):
            error_samples.append((key, new_key, 'pattern_mismatch'))

        corrected[new_key] = value

    return corrected, fix_count, error_samples


def get_codes_from_env_or_config() -> list:
    """【坑#3防护】统一从BATCH_CODES或BATCH_CONFIG获取批次代码列表。

    优先级：
    1. BATCH_CODES 环境变量
    2. BATCH_CONFIG.json 中合并所有批次的代码
    3. 返回空列表

    用法（在 GATE1 等需要批次代码的地方）:
      codes = get_codes_from_env_or_config()
      if codes:
          # Process specific codes
          pass
    """
    # 1) Try BATCH_CODES env var
    batch_env = get_batch_codes_from_env()
    if batch_env:
        return batch_env

    # 2) Fallback to merging all BATCH_CONFIG codes
    config = _safe_read_json(BATCH_CONFIG_PATH)
    if config:
        all_codes = set()
        for batch_name, entry in config.items():
            if isinstance(entry, dict) and 'codes' in entry:
                all_codes.update(entry['codes'])
            elif isinstance(entry, list):
                all_codes.update(entry)
        if all_codes:
            return sorted(all_codes)

    return []