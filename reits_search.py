#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
reits_search.py — FAISS+ES 联合搜索接口

两种搜索模式：
  - vector: FAISS 向量相似搜索 → 返回 Top-K 相似文本片段
  - hybrid: FAISS 搜 Top-50 → ES 按 fund_code/date 过滤 → 最终 Top-K

用法:
  python reits_search.py "基础设施资产支持专项计划 投资策略"
  python reits_search.py --fund 180101 "出租率变化趋势"
  python reits_search.py --fund 180101 --date 2025 "出租率"

作为模块:
  from reits_search import ReitsSearcher
  s = ReitsSearcher()
  results = s.search("蛇口产园出租率", top_k=5, fund_code="180101")
"""

import os, sys, json, argparse, gc
import numpy as np

# 🔧 FAISS + sentence-transformers 共存在同一进程时 OpenMP 库冲突
# FAISS 和 PyTorch 各自链接了不同版本的 libomp.dylib
os.environ.setdefault('KMP_DUPLICATE_LIB_OK', 'TRUE')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

FAISS_INDEX_PATH = os.path.join(SCRIPT_DIR, "..", "..", "5_分析结果", "faiss_data", "reits_faiss.index")
META_PATH = os.path.join(SCRIPT_DIR, "..", "..", "5_分析结果", "faiss_data", "reits_faiss_meta.json")

# Lazy imports (to avoid crashes when not in conda env)
_faiss = None
_ST = None


def _get_faiss():
    global _faiss
    if _faiss is None:
        import faiss
        _faiss = faiss
    return _faiss


def _get_st():
    global _ST
    if _ST is None:
        from sentence_transformers import SentenceTransformer
        _ST = SentenceTransformer
    return _ST


class ReitsSearcher:
    """REITs FAISS 向量搜索器"""

    def __init__(self, index_path=None, meta_path=None, model_name=None):
        self.index_path = index_path or FAISS_INDEX_PATH
        self.meta_path = meta_path or META_PATH
        self.model_name = model_name or "BAAI/bge-base-zh-v1.5"

        self._index = None
        self._meta = None
        self._model = None

    @property
    def index(self):
        if self._index is None:
            faiss = _get_faiss()
            if not os.path.exists(self.index_path):
                raise FileNotFoundError(f"FAISS 索引不存在: {self.index_path}")
            self._index = faiss.read_index(self.index_path)
            print(f"📂 加载索引: {self._index.ntotal:,} 向量 × {self._index.d}维")
        return self._index

    @property
    def meta(self):
        if self._meta is None:
            with open(self.meta_path) as f:
                self._meta = json.load(f)
        return self._meta

    @property
    def model(self):
        if self._model is None:
            ST = _get_st()
            self._model = ST(self.model_name, device="cpu")  # CPU to avoid MPS crashes
            print(f"🧠 模型已加载: {self.model_name}")
        return self._model

    def encode(self, texts):
        """编码查询文本（通过子进程隔离 FAISS 与 PyTorch 的 OpenMP 冲突）"""
        import subprocess, tempfile
        
        # 将文本写入临时文件
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(texts, f)
            tmp_in = f.name
        
        tmp_out = tmp_in + '.vec.npy'
        
        try:
            result = subprocess.run(
                [sys.executable, '-c', f'''
import json, numpy as np
from sentence_transformers import SentenceTransformer

with open("{tmp_in}") as f:
    texts = json.load(f)
m = SentenceTransformer("{self.model_name}", device="cpu")
vecs = m.encode(texts, normalize_embeddings=True).astype(np.float32)
np.save("{tmp_out}", vecs)
'''],
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                raise RuntimeError(f"编码子进程失败: {result.stderr}")
            
            return np.load(tmp_out)
        finally:
            for p in [tmp_in, tmp_out]:
                if os.path.exists(p):
                    os.remove(p)

    def _unload_model(self):
        """释放模型内存"""
        if self._model is not None:
            del self._model
            self._model = None
            gc.collect()

    def search(self, query, top_k=10, fund_code=None, date=None):
        """
        向量搜索（顺序加载模式：先编码→后搜索，避免 FAISS+PyTorch 共存时 OOM）

        Args:
            query: 查询文本
            top_k: 返回结果数
            fund_code: 可选，按基金代码过滤
            date: 可选，按日期过滤（前缀匹配，如 "2025" 过滤 2025 年）

        Returns:
            list of dict: [{'rank', 'score', 'fund_code', 'date', 'title', 'text', 'global_id'}, ...]
        """
        # 编码查询（仅加载模型，不加载 FAISS）
        query_vec = self.encode([query])

        # 释放模型，加载 FAISS 索引搜索
        self._unload_model()
        idx = self.index  # lazy-load FAISS

        search_k = max(top_k * 5, 100) if (fund_code or date) else top_k
        distances, indices = idx.search(query_vec, min(search_k, idx.ntotal))

        # 收集结果
        id_map = self.meta.get('id_map', [])
        results = []
        for rank, (doc_id, dist) in enumerate(zip(indices[0], distances[0])):
            if doc_id < 0 or doc_id >= len(id_map):
                continue
            info = id_map[doc_id]

            # 可选过滤
            if fund_code and info.get('fund_code', '') != fund_code:
                continue
            if date and not str(info.get('date', '')).startswith(date):
                continue

            results.append({
                'rank': len(results) + 1,
                'score': float(dist),
                'fund_code': info.get('fund_code', ''),
                'date': info.get('date', ''),
                'title': info.get('title', ''),
                'text': info.get('text', ''),
                'global_id': info.get('global_id', ''),
            })

            if len(results) >= top_k:
                break

        return results

    def search_with_es_filter(self, query, top_k=10, fund_code=None, date_from=None, date_to=None):
        """
        混合搜索：FAISS 向量搜 Top-100 → 用 ES 元数据精确过滤 → 返回 Top-K

        需要 ES 运行在 localhost:9200。
        """
        try:
            import requests
        except ImportError:
            return self.search(query, top_k, fund_code, date_from)

        # FAISS 粗筛
        query_vec = self.encode([query])
        distances, indices = self.index.search(query_vec, min(200, self.index.ntotal))

        id_map = self.meta.get('id_map', [])
        candidate_ids = set()
        for doc_id in indices[0]:
            if doc_id < 0 or doc_id >= len(id_map):
                continue
            info = id_map[doc_id]
            if fund_code and info.get('fund_code') != fund_code:
                continue
            candidate_ids.add(info.get('global_id', ''))

        # ES 精确过滤
        if not candidate_ids:
            return []

        es_query = {
            "query": {
                "bool": {
                    "must": [
                        {"ids": {"values": list(candidate_ids)[:100]}}
                    ],
                    "filter": []
                }
            },
            "size": top_k,
            "_source": ["text", "fund_code", "date", "announcement_title", "source_file"]
        }

        if fund_code:
            es_query["query"]["bool"]["filter"].append({"term": {"fund_code.keyword": fund_code}})
        if date_from:
            es_query["query"]["bool"]["filter"].append({"range": {"date": {"gte": date_from}}})
        if date_to:
            es_query["query"]["bool"]["filter"].append({"range": {"date": {"lte": date_to}}})

        r = requests.post('http://localhost:9200/reits_announcements/_search', json=es_query, timeout=10)
        results = []
        for hit in r.json().get('hits', {}).get('hits', []):
            src = hit['_source']
            results.append({
                'rank': len(results) + 1,
                'score': hit.get('_score', 0),
                'fund_code': src.get('fund_code', ''),
                'date': src.get('date', ''),
                'title': src.get('announcement_title', ''),
                'text': src.get('text', '')[:300],
                'source': src.get('source_file', ''),
            })

        return results


def main():
    parser = argparse.ArgumentParser(description='REITs FAISS 向量搜索')
    parser.add_argument('query', nargs='?', help='搜索查询文本')
    parser.add_argument('--fund', '-f', help='基金代码过滤')
    parser.add_argument('--date', '-d', help='日期过滤（前缀，如 2025）')
    parser.add_argument('--top', '-k', type=int, default=5, help='返回结果数')
    parser.add_argument('--hybrid', action='store_true', help='使用 ES 混合搜索')
    parser.add_argument('--interactive', '-i', action='store_true', help='交互模式')
    args = parser.parse_args()

    searcher = ReitsSearcher()

    if args.interactive:
        print("REITs 向量搜索 (输入 'q' 退出)\n")
        while True:
            try:
                q = input("🔎 查询: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() == 'q':
                break
            results = searcher.search(q, top_k=args.top, fund_code=args.fund, date=args.date)
            _print_results(results)
        return

    if not args.query:
        parser.print_help()
        return

    if args.hybrid:
        results = searcher.search_with_es_filter(args.query, top_k=args.top, fund_code=args.fund, date_from=args.date)
    else:
        results = searcher.search(args.query, top_k=args.top, fund_code=args.fund, date=args.date)

    _print_results(results)


def _print_results(results):
    if not results:
        print("  无结果")
        return
    print(f"\n{'='*70}")
    for r in results:
        print(f"  #{r['rank']}  [{r.get('fund_code','')}] {r.get('date','')}  (score: {r.get('score',0):.4f})")
        print(f"   {r.get('title','')}")
        text = r.get('text', '')
        if text:
            print(f"   {text[:150]}...")
        print()


if __name__ == '__main__':
    main()
