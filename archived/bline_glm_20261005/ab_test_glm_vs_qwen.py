# ab_test_glm_vs_qwen.py — B线(GLM-5.3-Flash) vs A线(qwen-vl-max) 表格描述质量对比
# 选样: 508610 招募书 table_image 前 8 张(表格+文字混合), 双跑落盘, 量化对比
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow")
import os
os.chdir("/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow")
from step4_table_utils_ali import generate_table_description as desc_ali
from step4_glm_vision_utils import generate_table_description as desc_glm

IMG_DIR = Path("announcement_document_processing_local/508610/508610-508610-广发新城吾悦封闭式商业不动产证券投资基金招募说明书-2026-09-18/table_image")
OUT = Path("/tmp/ab_test_results.json")

NUM_RE = re.compile(r"\d[\d,，.]*%?")  # 数字(含千分位/小数/百分号)

def norm_nums(text: str) -> set:
    """归一化数字集合: 去千分位逗号, 百分数去%, 统一为字符串"""
    out = set()
    for m in NUM_RE.findall(text):
        s = m.replace(",", "").replace("，", "").rstrip("%")
        # 丢弃纯年份和纯序号? 不丢 — 全保留, 年份也是信息
        try:
            out.add(f"{float(s):.4f}")
        except ValueError:
            continue
    return out

def run_line(fn, name, imgs):
    results = {}
    for img in imgs:
        t0 = time.time()
        try:
            r = fn(str(img))
            results[img.name] = {
                "ok": True, "len": len(r), "secs": round(time.time() - t0, 1),
                "has_table_header": "## 表格主题" in r,
                "nums": sorted(norm_nums(r)), "text_head": r[:120],
            }
            print(f"[{name}] {img.name}: {len(r)}字 {time.time()-t0:.0f}s "
                  f"数字{len(results[img.name]['nums'])}个 {'含表格' if results[img.name]['has_table_header'] else ''}", flush=True)
        except Exception as e:
            results[img.name] = {"ok": False, "error": str(e)[:200]}
            print(f"[{name}] {img.name}: 失败 {str(e)[:100]}", flush=True)
    return results

imgs = sorted(IMG_DIR.glob("page_*.png"))[:8]
print(f"样本: {len(imgs)} 张 (508610 招募书表格图)\n")

print("═══ B线: GLM-5.3-Flash ═══")
glm = run_line(desc_glm, "GLM", imgs)
print("\n═══ A线: qwen-vl-max ═══")
ali = run_line(desc_ali, "QWEN", imgs)

# ── 量化对比 ──
print("\n═══ 对比结论 ═══")
rows = []
for img in imgs:
    n = img.name
    a, b = ali.get(n, {}), glm.get(n, {})
    if not (a.get("ok") and b.get("ok")):
        rows.append({"img": n, "status": f"A:{a.get('ok')} B:{b.get('ok')}"})
        continue
    an, bn = set(a["nums"]), set(b["nums"])
    inter, union = an & bn, an | bn
    # 数字召回率: 双方共同数字占A线数字比例(B线漏了多少A看到的数字); 以及反向
    jaccard = len(inter) / len(union) if union else 1.0
    rows.append({
        "img": n,
        "A_len": a["len"], "B_len": b["len"],
        "A_nums": len(an), "B_nums": len(bn),
        "共同": len(inter), "A独有": len(an - bn), "B独有": len(bn - an),
        "数字Jaccard": round(jaccard, 3),
        "A_s": a["secs"], "B_s": b["secs"],
        "A表格": a["has_table_header"], "B表格": b["has_table_header"],
    })

ok_rows = [r for r in rows if "A_len" in r]
print(f"{'图片':<18}{'A字':>6}{'B字':>6}{'A数':>5}{'B数':>5}{'共同':>5}{'A独':>5}{'B独':>5}{'Jacc':>7}{'A秒':>5}{'B秒':>5}")
for r in ok_rows:
    print(f"{r['img']:<18}{r['A_len']:>6}{r['B_len']:>6}{r['A_nums']:>5}{r['B_nums']:>5}"
          f"{r['共同']:>5}{r['A独有']:>5}{r['B独有']:>5}{r['数字Jaccard']:>7}{r['A_s']:>5}{r['B_s']:>5}")

tot_a = sum(r["A_nums"] for r in ok_rows); tot_b = sum(r["B_nums"] for r in ok_rows)
tot_i = sum(r["共同"] for r in ok_rows); tot_ad = sum(r["A独有"] for r in ok_rows); tot_bd = sum(r["B独有"] for r in ok_rows)
print(f"\n合计: A数字{tot_a} B数字{tot_b} 共同{tot_i} | A独有{tot_ad} B独有{tot_bd}")
print(f"B对A数字召回率: {tot_i/tot_a*100:.1f}% | A对B: {tot_i/tot_b*100:.1f}%")
print(f"平均Jaccard: {sum(r['数字Jaccard'] for r in ok_rows)/len(ok_rows):.3f}")

OUT.write_text(json.dumps({"ali": ali, "glm": glm, "rows": rows}, ensure_ascii=False, indent=1))
print(f"\n结果落盘: {OUT}")
