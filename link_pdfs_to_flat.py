#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
link_pdfs_to_flat.py — 将REITs公告PDF链接到 _flat_pdfs/ 的正确工具

【坑#1解决方案】防止手动链入错误目录。
  - 读取 announcements_manifest.csv，筛选目标公告
  - 在 _flat_pdfs/ 创建 symlink 指向 REITs_notice/{code}/ 下的真实PDF
  - 使用 file_paths_config.PDF_DIR，确保与管道一致

用法:
  # 按日期范围链入
  python link_pdfs_to_flat.py --after 2026-06-04

  # 按分类关键词链入
  python link_pdfs_to_flat.py --keywords 季度报告,运营数据,招募说明书

  # 按fund code链入
  python link_pdfs_to_flat.py --codes 508606,508607,508016

  # 干运行（只报告不操作）
  python link_pdfs_to_flat.py --after 2026-06-04 --dry-run
"""
import os, sys, csv, json, argparse
from datetime import datetime

# 自动定位项目根目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from file_paths_config import PDF_DIR
from common_utils import verify_pdf_dir_populated

MANIFEST_CSV = os.path.join(
    os.path.dirname(os.path.dirname(SCRIPT_DIR)), "REITs_announcements", "announcements_manifest.csv"
)

EXTRACT_KW = [
    '季度报告', '年度报告', '年报', '中报', '中期报告', '运营数据', '经营情况',
    '招募说明书', '基金合同', '托管协议', '产品资料', '扩募', '询价', '基金合同生效',
    # 常见公告文件名词（2026-08 补充）：默认不再漏掉 产品资料概要更新 等无'公告'字样标题
    '公告', '概要', '报告',
]


def main():
    parser = argparse.ArgumentParser(description="链接REITs公告PDF到管道PDF_DIR")
    parser.add_argument('--after', help='仅处理此日期后的公告 (YYYY-MM-DD)')
    parser.add_argument('--codes', help='基金代码,逗号分隔')
    parser.add_argument('--keywords', help='标题关键词,逗号分隔 (在默认提取类公告关键词之上增量追加)')
    parser.add_argument('--dry-run', action='store_true', help='仅报告，不操作')
    args = parser.parse_args()

    # 验证PDF_DIR
    print(f"📂 目标目录: {PDF_DIR}")
    os.makedirs(PDF_DIR, exist_ok=True)

    # 读取manifest
    if not os.path.exists(MANIFEST_CSV):
        print(f"❌ MANIFEST_CSV 不存在: {MANIFEST_CSV}")
        sys.exit(1)

    rows = []
    with open(MANIFEST_CSV, 'r') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    # 筛选条件
    date_filter = args.after
    code_filter = set(c.strip() for c in args.codes.split(',')) if args.codes else None
    # 关键词过滤：默认=EXTRACT_KW；显式 --keywords 为「增量」，与默认集合取并集，
    # 避免单传 '公告' 时把 产品资料概要更新 等无'公告'字样标题漏掉
    kw_filter = set(EXTRACT_KW)
    if args.keywords:
        kw_filter |= set(k.strip() for k in args.keywords.split(',') if k.strip())

    linked = 0
    skipped = 0
    broken = 0

    for r in rows:
        # Date filter
        if date_filter and r.get('date', '') < date_filter:
            continue

        # Title keyword filter（移除对'提示性公告'的硬编码排除：它并非去重/防误链逻辑，
        # 且会漏掉需要入库的提示性公告，如 180901 解除限售提示性公告，曾被迫手动补链）
        title = r.get('title', '') or r.get('display_title', '')
        if not any(kw in title for kw in kw_filter):
            continue

        # Code filter
        fund_code = r.get('fund_code', '')
        if code_filter and fund_code not in code_filter:
            continue

        # Source path
        local_path = r.get('local_path', '')
        if not local_path or not os.path.exists(local_path):
            broken += 1
            continue

        fname = os.path.basename(local_path)
        dst = os.path.join(PDF_DIR, fname)

        if os.path.exists(dst):
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [DRY] {fund_code} | {fname[:70]}...")
        else:
            os.symlink(os.path.abspath(local_path), dst)
            linked += 1

    print(f"\n✅ 完成: 链接 {linked} | 跳过(已存在) {skipped} | 源文件缺失 {broken}")
    if not args.dry_run:
        verify_pdf_dir_populated(PDF_DIR, min_files=1)


if __name__ == '__main__':
    main()
