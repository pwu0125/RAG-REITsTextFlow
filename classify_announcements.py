#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公告收录分类器 v2 —— 纯规则，无 LLM。

数据源: 同目录 classify_policy.json（机器可读关键词表，唯一配置来源）。
优先级: 全局硬排除 > core_full(含 exclude) > core_lite > non_core > 默认 non_core。
讨论触发: 与分层正交，命中 discuss_triggers 即标记。

用法:
  python classify_announcements.py                      # 从 stdin 读标题（每行一条）
  python classify_announcements.py < titles.txt         # 同上
  python classify_announcements.py titles.txt           # 读文件（每行一条）
  python classify_announcements.py --file titles.txt    # 显式文件

输入行格式: 纯标题，或 "元数据|标题" / "元数据\t标题"（末字段视为标题，其余字段原样回显）。
输出: 每行  label<TAB>命中=关键词<TAB>讨论触发<TAB>元数据<TAB>标题；结尾输出分布统计。
"""
import json
import os
import sys
from collections import Counter

POLICY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classify_policy.json")
DEFAULT_HIT = "未命中->默认非核心"


def load_policy(path=POLICY_FILE):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _hits(keywords, title):
    return [k for k in keywords if k in title]


def classify(title, policy):
    """返回 (label, matched_keywords)。"""
    hard = policy.get("hard_non_core", [])
    cf = policy["core_full"]
    cl = policy["core_lite"]
    nc = policy["non_core"]

    # 1) 全局硬排除：命中即 non_core（与 rebuild EXCLUDE 一致；压过 core 层优先级）
    for kw in hard:
        if kw in title:
            return ("non_core", [kw + "(全局排除)"])

    # 2) core_full：命中 keywords 且未命中 exclude
    if not _hits(cf.get("exclude", []), title):
        m = _hits(cf["keywords"], title)
        if m:
            return ("core_full", m)

    # 3) core_lite
    m = _hits(cl["keywords"], title)
    if m:
        return ("core_lite", m)

    # 4) non_core
    m = _hits(nc["keywords"], title)
    if m:
        return ("non_core", m)

    # 5) 未命中 → 默认 non_core
    return ("non_core", [DEFAULT_HIT])


def main():
    args = sys.argv[1:]
    lines = []
    if args and args[0] in ("-f", "--file"):
        with open(args[1], "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    elif args:
        with open(args[0], "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        lines = sys.stdin.read().splitlines()

    policy = load_policy()
    triggers = policy["discuss_triggers"]

    results = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.replace("\t", "|").split("|")]
        title = parts[-1]
        meta = parts[:-1]
        label, kws = classify(title, policy)
        discuss = [t for t in triggers if t in title]
        results.append((meta, title, label, kws, discuss))

    for meta, title, label, kws, discuss in results:
        d = ("讨论:" + "/".join(discuss)) if discuss else "-"
        print("%s\t命中=%s\t%s\t%s\t%s" % (label, ",".join(kws), d, "|".join(meta), title))

    print("\n=== 分布统计 ===")
    for k, v in sorted(Counter(r[2] for r in results).items()):
        print("%s: %d" % (k, v))
    print("总计: %d" % len(results))

    defaulted = [r for r in results if r[3] == [DEFAULT_HIT]]
    if defaulted:
        print("\n未命中默认 non_core (%d):" % len(defaulted))
        for meta, title, *_ in defaulted:
            print("  - %s %s" % ("|".join(meta), title))

    flagged = [r for r in results if r[4]]
    if flagged:
        print("\n讨论触发 (%d):" % len(flagged))
        for meta, title, label, _, discuss in flagged:
            print("  [%s] %s %s -> %s" % (label, "|".join(meta), title, "/".join(discuss)))


if __name__ == "__main__":
    main()
