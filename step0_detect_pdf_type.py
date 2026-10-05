#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
step0_detect_pdf_type.py — pdf-inspector 前置路由检测（step2 前置）

运行命令（必须用 pdfinspector-venv 的 python，该 venv 内装有 pdf-inspector）：
    /Users/pyemini/REITs/tools/pdfinspector-venv/bin/python step0_detect_pdf_type.py

功能：
- 扫描 PDF_DIR(_flat_pdfs) 下所有 .pdf；
- 跳过 meta.json 已有 pdf_type 字段的文件；
- 对每个文件调用 pdf_inspector.detect_pdf 分类，把结果写入
  OUTPUT_DIR/{fund_code}/{pdf_folder_name}/meta.json 的新增字段：
    pdf_type              str          'text_based' / 'scanned' / 'image_based' / 'mixed' / 'detect_error'
    pdf_confidence        float
    pages_needing_ocr     list[int]    1 索引物理页码
    ocr_reasons_by_page   dict         {页码: [reason, ...]}
    pdf_detect_page_count int
  检测异常时写 pdf_type='detect_error' + pdf_error。

约束：
- 只新增检测字段，不改 meta.json 其他字段；
- 不写 manifest（processed_files_local.json 仅 rebuild_manifest.py 可写）。

可选命令行参数：传一个或多个文件名子串，只处理文件名包含任一子串的文件；
不传参数则扫描全部。

注意：pdf_inspector.classify_pdf 返回的 PdfClassification 不含
ocr_reasons_by_page 且 pages_needing_ocr 为 0 索引；而 detect_pdf 返回的
PdfResult 含 ocr_reasons_by_page 且 pages_needing_ocr 为 1 索引物理页码，
与 step2 的 page_number 约定一致，故使用 detect_pdf。
"""

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from file_paths_config import PDF_DIR, OUTPUT_DIR
from common_utils import safe_json_load, safe_json_dump

from pdf_inspector import detect_pdf

FILENAME_RE = re.compile(r"^(\d{6})-(.+)-(\d{4}-\d{2}-\d{2})\.pdf$")


def parse_filename(file_name: str):
    """从文件名解析 fund_code / date / short_name / announcement_title。"""
    m = FILENAME_RE.match(file_name)
    if not m:
        return None
    fund_code, mid, date = m.groups()
    short_name, announcement_title = ("", mid)
    if "_" in mid:
        short_name, announcement_title = mid.split("_", 1)
    return {
        "fund_code": fund_code,
        "date": date,
        "short_name": short_name,
        "announcement_title": announcement_title,
    }


def build_minimal_meta(file_name: str, pdf_path: str) -> dict:
    """meta.json 缺失时构造的骨架（仅文件信息，不含状态标志）。"""
    parsed = parse_filename(file_name) or {}
    return {
        "file_name": file_name,
        "file_path": pdf_path,
        "date": parsed.get("date", ""),
        "fund_code": parsed.get("fund_code", ""),
        "short_name": parsed.get("short_name", ""),
        "announcement_title": parsed.get("announcement_title", ""),
        "doc_type_1": "",
        "doc_type_2": "",
        "announcement_link": "",
    }


def detect_single_pdf(pdf_path: str):
    """调用 pdf_inspector，返回可写入 meta.json 的检测字段 dict。

    抛出的任何异常由调用方捕获并转成 detect_error 字段。
    """
    result = detect_pdf(os.path.realpath(pdf_path))

    ocr_reasons = {}
    for item in getattr(result, "ocr_reasons_by_page", None) or []:
        page = getattr(item, "page", None)
        reasons = getattr(item, "reasons", None) or []
        if isinstance(page, int):
            ocr_reasons[str(page)] = list(reasons)

    return {
        "pdf_type": result.pdf_type,
        "pdf_confidence": round(float(result.confidence), 4),
        "pages_needing_ocr": list(result.pages_needing_ocr),
        "ocr_reasons_by_page": ocr_reasons,
        "pdf_detect_page_count": int(result.page_count),
    }


def main():
    filters = [
        part.strip()
        for s in sys.argv[1:]
        if not s.startswith("-")
        for part in s.split(",")
        if part.strip()
    ]
    start = time.time()

    if not os.path.isdir(PDF_DIR):
        print(f"[错误] PDF_DIR 不存在: {PDF_DIR}")
        sys.exit(1)

    all_files = sorted(
        f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf")
    )
    if filters:
        targets = [f for f in all_files if any(sub in f for sub in filters)]
        print(f"[过滤] 命令行子串 {filters} → 命中 {len(targets)}/{len(all_files)} 个文件")
    else:
        targets = all_files
        print(f"[扫描] 全量 {len(targets)} 个 PDF")

    type_counts = {}
    error_count = 0
    skipped_count = 0
    processed = 0

    for file_name in targets:
        pdf_path = os.path.join(PDF_DIR, file_name)
        parsed = parse_filename(file_name)
        if not parsed:
            print(f"[跳过] 文件名无法解析 fund_code: {file_name}")
            skipped_count += 1
            continue

        fund_code = parsed["fund_code"]
        pdf_folder_name = os.path.splitext(file_name)[0]
        meta_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "meta.json")

        # 已有 pdf_type 则跳过（增量）
        if os.path.exists(meta_path):
            try:
                existing = safe_json_load(meta_path)
                if isinstance(existing, dict) and "pdf_type" in existing:
                    print(f"[跳过] 已有 pdf_type={existing['pdf_type']}: {file_name}")
                    skipped_count += 1
                    continue
            except Exception as e:
                print(f"[警告] meta.json 读取失败({e})，将覆盖: {file_name}")

        try:
            new_fields = detect_single_pdf(pdf_path)
        except Exception as e:
            new_fields = {
                "pdf_type": "detect_error",
                "pdf_error": f"{type(e).__name__}: {e}",
            }
            error_count += 1
            print(f"[错误] 检测失败 {file_name}: {e}")

        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        if os.path.exists(meta_path):
            try:
                meta = safe_json_load(meta_path)
            except Exception:
                meta = build_minimal_meta(file_name, pdf_path)
        else:
            meta = build_minimal_meta(file_name, pdf_path)
        if not isinstance(meta, dict):
            meta = build_minimal_meta(file_name, pdf_path)

        meta.update(new_fields)
        safe_json_dump(meta, meta_path)

        ptype = new_fields.get("pdf_type", "detect_error")
        type_counts[ptype] = type_counts.get(ptype, 0) + 1
        processed += 1

        if ptype != "detect_error":
            ocr_n = len(new_fields.get("pages_needing_ocr") or [])
            print(
                f"[完成] {fund_code} | {ptype:<10} conf={new_fields.get('pdf_confidence')} "
                f"pages={new_fields.get('pdf_detect_page_count')} ocr={ocr_n} | {file_name}"
            )

    elapsed = time.time() - start
    print("=" * 70)
    print(f"[统计] 处理 {processed}，跳过 {skipped_count}，错误 {error_count}")
    print(f"[统计] 各 pdf_type 计数: {type_counts or '(无成功检测)'}")
    print(f"[统计] 耗时 {elapsed:.1f} 秒")


if __name__ == "__main__":
    main()
