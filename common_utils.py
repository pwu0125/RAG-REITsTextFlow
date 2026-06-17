#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
common_utils.py - 通用工具模块

提供统一的JSON序列化功能，解决datetime对象序列化问题
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
    """解析逗号分隔的 code 字符串，支持 JSON 数组格式。"""
    if not codes_str or not codes_str.strip():
        return None
    codes_str = codes_str.strip()
    try:
        return json.loads(codes_str)
    except Exception:
        return [c.strip() for c in codes_str.split(",") if c.strip()]


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