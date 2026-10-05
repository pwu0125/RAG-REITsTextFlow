#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收录数据大表生成器 — 零 LLM，纯统计，供每日管道调用。

数据源:
  1. processed_files_local.json  (RAG 收录台账, rebuild_manifest_v3 唯一事实)
  2. reits_master_universe.csv   (全市场唯一数据源: 代码/名称/上市日)
  3. ES reits_announcements      (已入库权威判定: source_file 聚合)

入库判定: 台账条目 file_name ∈ ES source_file 集合 = 已入库。
  台账有但 ES 无 → 登记残留, 不计入大表, 打印 WARNING。
  ES 不可用 → fail-closed: 退出码 1 (管道报警), 不写大表。

输出: ~/REITs/2_公告数据/收录数据大表_公告入库统计.csv
  行 = 已上市基金 ∪ 台账中有收录的基金, 按 fund_code 排序
  列 = 代码 | 名称 | 上市交易首日 | 各公告类型入库数量... | 入库合计

分类映射(标题关键词, 优先级自上而下, 与实测全量零未命中对齐):
  扩募说明书更新 > 扩募说明书 > 招募说明书更新 > 招募说明书首发版
  > 年度财报 > 中期财报 > 季度财报 > 运营公告 > 分红公告
  > 重大事件公告 > 基金合同 > 上市交易公告 > 其他公告

退出码: 0 = 成功(即使有 WARNING); 1 = 数据源缺失/异常(供管道报警)
"""
import csv
import json
import os
import sys
import urllib.request
from collections import Counter, defaultdict

MANIFEST = "/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/announcement_document_processing_local/processed_files_local.json"
MASTER = "/Users/pyemini/REITs/2_公告数据/reits_master_universe.csv"
OUT_CSV = "/Users/pyemini/REITs/2_公告数据/收录数据大表_公告入库统计.csv"
ES_INDEX = "reits_announcements"
ES_URL = "http://localhost:9200"


def fetch_es_source_files():
    """ES 聚合查询: 返回 distinct source_file 集合。失败抛异常。"""
    q = json.dumps({
        "size": 0,
        "query": {"match_all": {}},
        "aggs": {"sources": {"terms": {"field": "source_file", "size": 5000}}},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{ES_URL}/{ES_INDEX}/_search",
        data=q,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    buckets = d.get("aggregations", {}).get("sources", {}).get("buckets", [])
    return {b["key"] for b in buckets}

COLUMNS = [
    "招募说明书首发版", "招募说明书更新", "扩募说明书", "扩募说明书更新",
    "年度财报", "中期财报", "季度财报", "运营公告", "分红公告",
    "重大事件公告", "基金合同", "上市交易公告", "招募书附件", "其他公告",
]

UPDATE_HINTS = ("更新", "修改", "修订", "更正")


def classify(title: str) -> str:
    t = title or ""
    if "附件" in t:
        return "招募书附件"
    if "扩募" in t:
        return "扩募说明书更新" if any(k in t for k in UPDATE_HINTS) else "扩募说明书"
    if "招募说明书" in t:
        return "招募说明书更新" if any(k in t for k in UPDATE_HINTS) else "招募说明书首发版"
    if "年度报告" in t:
        return "年度财报"
    if "中期报告" in t:
        return "中期财报"
    if "季度报告" in t:
        return "季度财报"
    if any(k in t for k in ("运营情况", "运营数据", "经营情况")):
        return "运营公告"
    if any(k in t for k in ("收益分配", "分红")):
        return "分红公告"
    if any(k in t for k in ("重大事项", "收购", "出售", "持有人大会", "清算", "终止", "重大资产")):
        return "重大事件公告"
    if "基金合同" in t:
        return "基金合同"
    if "上市交易" in t:
        return "上市交易公告"
    return "其他公告"


def main() -> int:
    warnings = []

    # ---- 0. ES 已入库判定（fail-closed: 不可用则退出 1 报警，不写大表）----
    try:
        es_sources = fetch_es_source_files()
    except Exception as ex:
        print(f"ERROR: ES 查询失败（fail-closed，大表未刷新）: {ex}", file=sys.stderr)
        return 1
    if not es_sources:
        print("ERROR: ES source_file 集合为空（fail-closed，大表未刷新）", file=sys.stderr)
        return 1

    # ---- 1. 读收录台账 ----
    if not os.path.exists(MANIFEST):
        print(f"ERROR: manifest 不存在: {MANIFEST}", file=sys.stderr)
        return 1
    with open(MANIFEST, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    files = manifest.get("files", {})
    if not files:
        print("ERROR: manifest files 为空", file=sys.stderr)
        return 1

    per_fund = defaultdict(Counter)
    total_ingested = 0
    residual = []
    for key, entry in files.items():
        code = entry.get("fund_code", "")
        title = entry.get("announcement_title") or entry.get("file_name", "")
        if not code:
            warnings.append(f"台账条目缺 fund_code: {entry.get('file_name', '?')}")
            continue
        if key not in es_sources:
            residual.append((code, title))
            continue
        cat = classify(title)
        if cat == "其他公告":
            warnings.append(f"未命中分类: [{code}] {title}")
        per_fund[code][cat] += 1
        total_ingested += 1

    if residual:
        warnings.append(f"台账有但 ES 无 chunks 的登记残留 {len(residual)} 条（不计入大表）")
        for code, title in residual[:5]:
            warnings.append(f"  残留: [{code}] {title[:70]}")

    # ---- 2. 读全市场唯一数据源 ----
    if not os.path.exists(MASTER):
        print(f"ERROR: master universe 不存在: {MASTER}", file=sys.stderr)
        return 1
    master = {}
    with open(MASTER, "r", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            master[row["fund_code"]] = row

    # ---- 3. 行集合 = 已上市 ∪ 台账有收录 ----
    listed = {c for c, r in master.items() if r.get("list_status") == "已上市"}
    ingested = set(per_fund.keys())
    orphan = ingested - set(master.keys())
    if orphan:
        warnings.append(f"台账存在但 master universe 缺失的代码(名称将留空): {sorted(orphan)}")
    row_codes = sorted(listed | ingested)

    # ---- 4. 写 CSV (UTF-8 BOM, Excel 兼容) ----
    header = ["基金代码", "基金名称", "上市交易首日"] + COLUMNS + ["入库合计"]
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for code in row_codes:
            info = master.get(code, {})
            counts = per_fund.get(code, Counter())
            row = [
                code,
                info.get("fund_name", ""),
                info.get("list_date", ""),
            ] + [counts.get(c, 0) for c in COLUMNS] + [sum(counts.values())]
            w.writerow(row)

    # ---- 5. 输出摘要(进管道日志) ----
    col_totals = Counter()
    for c in per_fund.values():
        col_totals.update(c)
    print(f"收录大表已刷新: {len(row_codes)} 行 (已上市 {len(listed)} + 仅收录未上市 {len(ingested - listed)})")
    print(f"  入库总数: {total_ingested} (台账 {len(files)} 篇)")
    detail = "  ".join(f"{c}={col_totals[c]}" for c in COLUMNS if col_totals[c])
    print(f"  分类分布: {detail}")
    for wmsg in warnings:
        print(f"  WARNING: {wmsg}")
    print(f"  输出: {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
