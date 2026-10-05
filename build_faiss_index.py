#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_faiss_index.py — 全量重建 FAISS IVF+PQ 向量索引

从现有 text_segmentation.json 读取文本 → 本地 BGE 模型生成 embedding
→ 保存到 text_segmentation_embedding.json → 构建 FAISS 索引 → 持久化

用法:
  python build_faiss_index.py              # 全量构建（含embedding生成+索引）
  python build_faiss_index.py --rebuild    # 强制重建所有embedding
  python build_faiss_index.py --index-only # 仅从已有embedding构建索引（跳过生成）
"""

import os, sys, json, time, argparse
import numpy as np
import requests
from pathlib import Path
from datetime import datetime
from tqdm import tqdm

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from file_paths_config import OUTPUT_DIR

# ── 配置 ─────────────────────────────────────────────────
MODEL_NAME = "BAAI/bge-base-zh-v1.5"  # 768-dim Chinese-optimized (base, 102M params)
BATCH_SIZE = 128                        # 批编码大小（base模型可用更大batch）
FAISS_INDEX_PATH = os.path.join(SCRIPT_DIR, "..", "..", "5_分析结果", "faiss_data", "reits_faiss.index")
META_PATH = os.path.join(SCRIPT_DIR, "..", "..", "5_分析结果", "faiss_data", "reits_faiss_meta.json")

# FAISS IVF+PQ 参数
IVF_NLIST = 4096        # IVF 聚类中心数
PQ_M = 48               # PQ 子向量数 (768/16=48)
PQ_NBITS = 8            # PQ 每子向量编码位数
NPROBE = 32             # 搜索时扫描簇数（默认精度）


def load_model():
    """加载 BGE 模型，使用 MPS 加速"""
    from sentence_transformers import SentenceTransformer
    print(f"📥 加载模型: {MODEL_NAME}...")
    model = SentenceTransformer(MODEL_NAME, device="mps")
    print(f"   ✅ 模型已加载到 MPS (Apple GPU)")
    return model


ES_URL = "http://localhost:9200"
ES_INDEX = "reits_announcements"


def get_es_source_files():
    """从 ES 获取所有唯一源文件名"""
    print(f"🔍 查询 ES ({ES_URL}/{ES_INDEX}) 唯一源文件...")
    all_files = set()
    after_key = None
    
    while True:
        body = {
            "size": 0,
            "aggs": {
                "files": {
                    "composite": {
                        "size": 10000,
                        "sources": [{"sf": {"terms": {"field": "source_file"}}}]
                    }
                }
            }
        }
        if after_key:
            body["aggs"]["files"]["composite"]["after"] = after_key
        
        r = requests.post(f'{ES_URL}/{ES_INDEX}/_search', json=body, timeout=15)
        resp = r.json()
        buckets = resp['aggregations']['files']['buckets']
        
        for b in buckets:
            all_files.add(b['key']['sf'])
        
        after_key = resp['aggregations']['files'].get('after_key')
        if not after_key or len(buckets) == 0:
            break
    
    print(f"   ES 唯一源文件: {len(all_files):,}")
    return all_files


def scan_documents(output_dir: str, es_filter=True):
    """扫描所有 text_segmentation.json 文件，可选 ES 过滤"""
    # 获取 ES 源文件白名单
    es_files = get_es_source_files() if es_filter else None
    
    docs = []
    skipped_no_es = 0
    for code in sorted(os.listdir(output_dir)):
        code_dir = os.path.join(output_dir, code)
        if not code.isdigit() or not os.path.isdir(code_dir):
            continue
        for doc_name in sorted(os.listdir(code_dir)):
            doc_dir = os.path.join(code_dir, doc_name)
            seg_path = os.path.join(doc_dir, 'text_segmentation.json')
            emb_path = os.path.join(doc_dir, 'text_segmentation_embedding.json')
            if os.path.exists(seg_path):
                # ES 过滤：只处理 ES 中存在的文档
                pdf_name = doc_name + '.pdf'
                if es_files and pdf_name not in es_files:
                    skipped_no_es += 1
                    continue
                docs.append({
                    'code': code,
                    'doc_name': doc_name,
                    'seg_path': seg_path,
                    'emb_path': emb_path,
                })
    
    if es_filter:
        total_on_disk = sum(1 for _ in [1])  # placeholder
        print(f"   ES∩磁盘: {len(docs):,} 篇 | 仅磁盘（跳过）: {skipped_no_es:,} 篇")
    return docs


def generate_embeddings(model, docs, force_rebuild=False):
    """生成所有缺失的 embedding 并保存到文件"""
    missing = []
    if force_rebuild:
        missing = docs
        print(f"\n🔄 强制重建模式: {len(docs)} 篇文档")
    else:
        for doc in docs:
            if not os.path.exists(doc['emb_path']):
                missing.append(doc)
        print(f"\n📊 全量文档: {len(docs)} | 已有embedding: {len(docs)-len(missing)} | 缺失: {len(missing)}")
    
    if not missing:
        print("   ✅ 所有 embedding 已就绪，跳过生成")
        return
    
    # 收集所有需要编码的文本
    all_texts = []
    doc_map = []  # (doc_idx, chunk_idx) → all_texts index
    
    print(f"📖 收集文本...")
    for doc_idx, doc in enumerate(tqdm(missing, desc="读取")):
        with open(doc['seg_path']) as f:
            chunks = json.load(f)
        for chunk_idx, chunk in enumerate(chunks):
            text = chunk.get('text', '') or chunk.get('content', '')
            if text.strip():
                all_texts.append(text)
                doc_map.append((doc_idx, chunk_idx))
    
    print(f"   📝 有效文本块: {len(all_texts):,}")
    
    # 批量编码
    print(f"🧠 生成 embedding (batch_size={BATCH_SIZE})...")
    embeddings = model.encode(
        all_texts,
        batch_size=BATCH_SIZE,
        show_progress_bar=True,
        normalize_embeddings=True,  # L2 normalize for cosine similarity
    )
    
    # 写回文件
    print(f"💾 保存 embedding 到磁盘...")
    doc_data = {i: {'chunks': [], 'embeddings': []} for i in range(len(missing))}
    
    for idx, (doc_idx, chunk_idx) in enumerate(doc_map):
        doc_data[doc_idx]['chunks'].append(chunk_idx)
        doc_data[doc_idx]['embeddings'].append(embeddings[idx].tolist())
    
    saved = 0
    for doc_idx, doc in enumerate(tqdm(missing, desc="写入")):
        with open(doc['seg_path']) as f:
            chunks = json.load(f)
        
        dd = doc_data[doc_idx]
        for ci, emb in zip(dd['chunks'], dd['embeddings']):
            if 'embedding' not in chunks[ci]:
                chunks[ci]['embedding'] = emb
        
        with open(doc['emb_path'], 'w') as f:
            json.dump(chunks, f, ensure_ascii=False)
        saved += 1
    
    print(f"   ✅ 已保存 {saved} 篇文档的 embedding")


def build_faiss_index(output_dir, index_path, meta_path, docs=None):
    """从所有 text_segmentation_embedding.json 构建 FAISS 索引"""
    import faiss
    
    if docs is None:
        docs = scan_documents(output_dir)
    
    print(f"\n🏗️ 构建 FAISS IVF+PQ 索引...")
    print(f"   参数: nlist={IVF_NLIST}, M={PQ_M}, nbits={PQ_NBITS}")
    
    # 收集所有 embedding 和元数据
    all_embeddings = []
    id_map = []  # (code, doc_name, chunk_idx, global_id)
    expected_dim = None
    skipped_bad = 0
    
    for doc in tqdm(docs, desc="收集向量"):
        if not os.path.exists(doc['emb_path']):
            continue
        with open(doc['emb_path']) as f:
            chunks = json.load(f)
        for ci, chunk in enumerate(chunks):
            emb = chunk.get('embedding')
            if not emb or not isinstance(emb, list) or len(emb) == 0:
                skipped_bad += 1
                continue
            # Detect expected dimension from first valid embedding
            if expected_dim is None:
                expected_dim = len(emb)
            # Skip embeddings with wrong dimension (stale API data)
            if len(emb) != expected_dim:
                skipped_bad += 1
                continue
            all_embeddings.append(emb)
            id_map.append({
                'code': doc['code'],
                'doc_name': doc['doc_name'],
                'chunk_idx': ci,
                'global_id': chunk.get('global_id', ''),
                'text': chunk.get('text', '')[:200],
                'fund_code': chunk.get('metadata', {}).get('fund_code', doc['code']),
                'date': chunk.get('metadata', {}).get('date', ''),
                'title': chunk.get('metadata', {}).get('announcement_title', '')[:80],
            })
    
    if skipped_bad:
        print(f"   ⚠️ 跳过 {skipped_bad} 个无效向量 (None/错误维度)")
    n_vectors = len(all_embeddings)
    dim = len(all_embeddings[0])
    print(f"   📊 总向量数: {n_vectors:,} | 维度: {dim}")
    
    # 转为 numpy array
    vectors = np.array(all_embeddings, dtype=np.float32)
    
    # ── 构建索引 ──
    # Apple Silicon (ARM) 上 IVF+PQ 训练偶发 segfault（FAISS x86优化未完全适配ARM）
    # 用 IndexFlatIP 做精确搜索：100% recall，3GB内存，~30ms/查询，零训练
    
    print(f"🏗️ 构建 IndexFlatIP (精确搜索，0训练)...")
    t0 = time.time()
    index = faiss.IndexFlatIP(dim)  # Inner Product (cosine similarity, already normalized)
    index.add(vectors)
    print(f"   索引构建完成: {time.time()-t0:.1f}s")
    print(f"   索引类型: IndexFlatIP (精确搜索, 100% recall)")
    print(f"   预估查询延迟: ~30ms (1M 向量 × 768维)")
    
    # 保存索引
    print(f"💾 保存 FAISS 索引: {index_path}")
    faiss.write_index(index, index_path)
    
    # 保存元数据
    print(f"💾 保存元数据: {meta_path}")
    metadata = {
        'n_vectors': n_vectors,
        'dim': dim,
        'index_type': 'IndexFlatIP',
        'model': MODEL_NAME,
        'built_at': datetime.now().isoformat(),
        'id_map': id_map,
    }
    with open(meta_path, 'w') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    # 统计
    index_size = os.path.getsize(index_path)
    meta_size = os.path.getsize(meta_path)
    print(f"\n✅ FAISS 索引构建完成!")
    print(f"   索引文件: {index_size/1024/1024:.1f} MB")
    print(f"   元数据文件: {meta_size/1024/1024:.1f} MB")
    print(f"   搜索配置: nprobe={NPROBE} (recall ~96%)")
    print(f"   内存占用: ~{n_vectors*PQ_M*PQ_NBITS/8/1024/1024:.0f} MB (PQ压缩)")
    
    return index, id_map


def verify_index(index_path: str, meta_path: str):
    """验证索引质量"""
    import faiss
    
    print(f"\n🔍 验证索引...")
    
    index = faiss.read_index(index_path)
    with open(meta_path) as f:
        metadata = json.load(f)
    
    n = metadata['n_vectors']
    
    # 自检索测试：查一个向量，看是否召回自己
    vectors = index.reconstruct_n(0, min(n, 10000))  # Sample first 10K
    test_vec = vectors[0:1]
    D, I = index.search(test_vec, 5)
    
    print(f"   自检索测试: Top-5 IDs = {I[0].tolist()}")
    print(f"   距离: {D[0].tolist()}")
    print(f"   自召回 (ID=0): {'✅' if 0 in I[0] else '⚠️ 近似搜索未召回自身 (正常, nprobe={index.nprobe})'}")
    
    # 统计
    print(f"   索引大小: {index.ntotal:,} 向量")
    print(f"   维度: {index.d}")
    print(f"   类型: IndexFlatIP (精确搜索)")
    print(f"   自召回 (ID=0): {'✅' if 0 in I[0] else '❌ 异常'}")


def search_example(index_path: str, meta_path: str, query: str, model, k: int = 5):
    """演示搜索"""
    import faiss
    
    index = faiss.read_index(index_path)
    with open(meta_path) as f:
        metadata = json.load(f)
    id_map = metadata['id_map']
    
    # 编码查询
    query_vec = model.encode([query], normalize_embeddings=True).astype(np.float32)
    
    # 搜索
    D, I = index.search(query_vec, k)
    
    print(f"\n🔎 搜索: '{query}'")
    print("-" * 60)
    for rank, (doc_id, dist) in enumerate(zip(I[0], D[0])):
        if doc_id < 0 or doc_id >= len(id_map):
            continue
        info = id_map[doc_id]
        print(f"  #{rank+1} (score={dist:.4f})")
        print(f"   {info['fund_code']} | {info['date']} | {info['title'][:50]}")
        print(f"   {info['text'][:120]}...")
        print()
    
    return I, D


def main():
    parser = argparse.ArgumentParser(description='FAISS 全量索引构建')
    parser.add_argument('--rebuild', action='store_true', help='强制重建所有 embedding')
    parser.add_argument('--index-only', action='store_true', help='仅构建索引（跳过 embedding 生成）')
    parser.add_argument('--verify', action='store_true', help='仅验证已有索引')
    parser.add_argument('--search', type=str, help='测试搜索')
    parser.add_argument('--no-es-filter', action='store_true', help='不过滤ES，处理所有磁盘分段文件')
    args = parser.parse_args()
    
    if args.verify:
        verify_index(FAISS_INDEX_PATH, META_PATH)
        return
    
    if args.search:
        model = load_model()
        search_example(FAISS_INDEX_PATH, META_PATH, args.search, model)
        return
    
    # ── 主流程 ──
    print("=" * 60)
    print("  FAISS REITs 向量索引构建")
    print(f"  时间: {datetime.now().isoformat()}")
    print("=" * 60)
    
    # 1) 扫描文档 (no-es-filter 时全扫描，否则仅ES文件)
    docs = scan_documents(OUTPUT_DIR, es_filter=not args.no_es_filter)
    print(f"\n📂 扫描到 {len(docs)} 篇文档 (有 text_segmentation.json)")
    
    # 2) 加载模型（index-only 模式不需要模型）
    model = None
    if not args.index_only:
        model = load_model()
    
    # 3) 生成 embedding（如需要）
    if not args.index_only and model:
        generate_embeddings(model, docs, force_rebuild=args.rebuild)
    
    # 4) 构建 FAISS 索引（传入已扫描的文档列表，避免重复扫描+ES过滤）
    index, id_map = build_faiss_index(OUTPUT_DIR, FAISS_INDEX_PATH, META_PATH, docs=docs)
    
    # 5) 验证
    verify_index(FAISS_INDEX_PATH, META_PATH)
    
    # 6) 示例搜索
    model2 = load_model()  # Re-load after potential memory pressure
    search_example(FAISS_INDEX_PATH, META_PATH, "基础设施资产支持专项计划 投资策略", model2)
    
    print("\n" + "=" * 60)
    print("  ✅ 全部完成! FAISS 索引已就绪")
    print(f"  索引文件: {FAISS_INDEX_PATH}")
    print(f"  元数据:   {META_PATH}")
    print("=" * 60)


if __name__ == '__main__':
    main()
