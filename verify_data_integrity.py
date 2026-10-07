#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
verify_data_integrity.py — 验证管道数据完整性

对比 ES/磁盘实际数据 vs meta.json 标志，报告不一致项。
默认只读模式（不修改）。使用 --fix 可自动修复标志位。

用法:
  python verify_data_integrity.py           # 只读模式：报告不一致
  python verify_data_integrity.py --fix     # 修复模式：对齐标志
  python verify_data_integrity.py --json    # JSON 输出（用于自动化）
"""

import os, sys, json, argparse
import requests

ES_URL = "http://localhost:9200"
ES_INDEX = "reits_announcements"
BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "announcement_document_processing_local")


def get_es_source_files():
    """从 ES 获取所有唯一源文件名"""
    all_files = set()
    after_key = None
    while True:
        body = {"size": 0, "aggs": {"files": {"composite": {"size": 10000, "sources": [{"sf": {"terms": {"field": "source_file"}}}]}}}}
        if after_key: body["aggs"]["files"]["composite"]["after"] = after_key
        try:
            r = requests.post(f'{ES_URL}/{ES_INDEX}/_search', json=body, timeout=15)
            resp = r.json()
            for b in resp['aggregations']['files']['buckets']:
                all_files.add(b['key']['sf'])
            after_key = resp['aggregations']['files'].get('after_key')
            if not after_key or len(resp['aggregations']['files']['buckets']) == 0:
                break
        except Exception as e:
            print(f"❌ ES 查询失败: {e}")
            return None
    return all_files


def check_integrity():
    """检查 meta.json 与 ES/磁盘的一致性"""
    es_files = get_es_source_files()
    if es_files is None:
        return None

    results = {
        'es_files': len(es_files),
        'disk_seg_files': 0,
        'es_flag_match': 0,
        'es_flag_missing': [],
        'seg_flag_match': 0,
        'seg_flag_missing': [],
        'errors': [],
    }

    for code in sorted(os.listdir(BASE)):
        if not code.isdigit(): continue
        code_dir = os.path.join(BASE, code)
        if not os.path.isdir(code_dir): continue
        for doc in os.listdir(code_dir):
            doc_dir = os.path.join(code_dir, doc)
            pdf_name = doc + '.pdf'
            seg_path = os.path.join(doc_dir, 'text_segmentation.json')
            meta_path = os.path.join(doc_dir, 'meta.json')

            if not os.path.exists(meta_path):
                continue

            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception as e:
                results['errors'].append(f"meta.json read error: {code}/{doc}: {e}")
                continue

            # Check ES flag
            if pdf_name in es_files:
                if meta.get('elasticsearch_database_done'):
                    results['es_flag_match'] += 1
                else:
                    # 2026-10-04 修复: 存完整文档目录名(用|分隔), 供 fix 精确匹配。
                    # 旧格式 doc[:60] + 前缀20字符匹配在同基金相似文件名下会改错文件。
                    results['es_flag_missing'].append(f"{code}|{doc}")

            # Check segmentation flag
            has_seg = os.path.exists(seg_path) and os.path.getsize(seg_path) > 100
            if has_seg:
                results['disk_seg_files'] += 1
                if meta.get('text_segmentation'):
                    results['seg_flag_match'] += 1
                else:
                    results['seg_flag_missing'].append(f"{code}/{doc[:60]}")

    return results


def print_report(results):
    """打印可读报告"""
    print("=" * 60)
    print("  📋 数据完整性验证报告")
    print("=" * 60)

    es_files = results['es_files']
    es_missing = len(results['es_flag_missing'])
    es_ok = results['es_flag_match']
    es_total = es_ok + es_missing

    seg_missing = len(results['seg_flag_missing'])
    seg_ok = results['seg_flag_match']
    seg_total = seg_ok + seg_missing

    pct_es = 100 * es_ok / es_total if es_total else 100
    pct_seg = 100 * seg_ok / seg_total if seg_total else 100

    print(f"\n  ES 标志一致性:  {es_ok}/{es_total} ({pct_es:.1f}%)")
    print(f"  分段标志一致性: {seg_ok}/{seg_total} ({pct_seg:.1f}%)")
    print(f"  ES 唯一文件:     {es_files}")
    print(f"  磁盘分段文件:   {results['disk_seg_files']}")

    if results['errors']:
        print(f"\n  ❌ 错误 ({len(results['errors'])}):")
        for e in results['errors'][:5]:
            print(f"    {e}")

    if es_missing > 0:
        print(f"\n  ⚠️ ES 标志缺失 ({es_missing}):")
        for f in results['es_flag_missing'][:10]:
            print(f"    {f}...")
        if es_missing > 10:
            print(f"    ... 还有 {es_missing - 10} 篇")

    if seg_missing > 0:
        print(f"\n  ⚠️ 分段标志缺失 ({seg_missing}):")
        for f in results['seg_flag_missing'][:10]:
            print(f"    {f}...")
        if seg_missing > 10:
            print(f"    ... 还有 {seg_missing - 10} 篇")

    if es_missing == 0 and seg_missing == 0 and not results['errors']:
        print(f"\n  ✅ 全部一致！meta.json 标志与 ES/磁盘 100% 对齐")
    else:
        print(f"\n  💡 运行 'python verify_data_integrity.py --fix' 可自动修复标志")


def _write_flag_dual(meta_path: str, flag: str, value: bool = True) -> bool:
    """双写标志: meta.json 与 text.json.metadata 同轮写入(陷阱90根治)。

    只写一侧是历史分歧的源头: verify --fix 曾只写 meta, 秒跳分支只碰一侧,
    消费者若用「后者覆盖」语义读, 旧 False 会否决新 True。
    现在两处原子化更新; text.json 不存在(尚未提取)则跳过 text 侧。
    返回是否实际写入。
    """
    with open(meta_path) as f:
        meta = json.load(f)
    meta[flag] = value
    with open(meta_path, 'w') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    text_path = os.path.join(os.path.dirname(meta_path), 'text.json')
    if os.path.exists(text_path):
        try:
            with open(text_path) as f:
                tj = json.load(f)
            tm = tj.get('metadata') or {}
            if tm.get(flag) is not value:
                tm[flag] = value
                tj['metadata'] = tm
                with open(text_path, 'w') as f:
                    json.dump(tj, f, ensure_ascii=False)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠️ text.json 不可写({meta_path}): {e}")
    return True


def fix_integrity(results):
    """修复不一致的标志(2026-10-04 双写加固)"""
    es_files = get_es_source_files()
    if es_files is None:
        print("❌ 无法连接 ES，取消修复")
        return

    fixed_es = 0
    fixed_seg = 0

    # Fix ES flags —— 精确路径匹配(条目格式 code|完整文档目录名) + 双写
    for entry in results['es_flag_missing']:
        code, doc_full = entry.split('|', 1)
        meta_path = os.path.join(BASE, code, doc_full, 'meta.json')
        if os.path.exists(meta_path):
            _write_flag_dual(meta_path, 'elasticsearch_database_done', True)
            fixed_es += 1

    # Fix segmentation flags —— 双写
    for entry in results['seg_flag_missing']:
        code, doc_full = entry.split('|', 1)
        meta_path = os.path.join(BASE, code, doc_full, 'meta.json')
        if os.path.exists(meta_path):
            _write_flag_dual(meta_path, 'text_segmentation', True)
            fixed_seg += 1

    print(f"\n✅ 修复完成: ES标志 {fixed_es} 篇, 分段标志 {fixed_seg} 篇 (均为 meta+text 双写)")


def main():
    parser = argparse.ArgumentParser(description='REITs 管道数据完整性验证')
    parser.add_argument('--fix', action='store_true', help='自动修复不一致的标志')
    parser.add_argument('--json', action='store_true', help='JSON 格式输出')
    args = parser.parse_args()

    if args.json:
        results = check_integrity()
        if results:
            print(json.dumps({
                'es_flag_ok': results['es_flag_match'],
                'es_flag_missing': len(results['es_flag_missing']),
                'seg_flag_ok': results['seg_flag_match'],
                'seg_flag_missing': len(results['seg_flag_missing']),
                'errors': len(results['errors']),
            }, ensure_ascii=False))
        return

    results = check_integrity()
    if results is None:
        sys.exit(1)

    print_report(results)

    # ---- 反向对账（2026-10-07 盲区修复）----
    # 原校验只查"ES有→标志对"，漏掉"标志True→ES实无"（16份回补事故的漏网点）。
    # 反向口径: core文档(manifest在册)标志True但ES无该source_file(带.pdf) → 列出。
    es_files = get_es_source_files() or set()
    rev_broken = []
    mf_path = os.path.join(BASE, 'processed_files_local.json')
    if os.path.exists(mf_path):
        try:
            mf = json.load(open(mf_path))
            for k, v in (mf.get('files') or {}).items():
                if not isinstance(v, dict):
                    continue
                if v.get('elasticsearch_database_done') and k not in es_files:
                    rev_broken.append(k)
        except Exception as e:
            print(f'  ⚠️ 反向对账 manifest 读取失败: {e}')
    print(f"\n  反向对账(标志True→ES实无): {len(rev_broken)} 份")
    if rev_broken:
        print("  ❌ 存在标志虚高文档（需回补或核销）:")
        for k in rev_broken[:10]:
            print(f"    {k[:70]}")
        if len(rev_broken) > 10:
            print(f"    ... 共 {len(rev_broken)} 份")

    if args.fix:
        if results['es_flag_missing'] or results['seg_flag_missing']:
            fix_integrity(results)
            # Re-check
            results2 = check_integrity()
            print_report(results2)
        else:
            print("\n  无需修复，标志已对齐。")


if __name__ == '__main__':
    main()
