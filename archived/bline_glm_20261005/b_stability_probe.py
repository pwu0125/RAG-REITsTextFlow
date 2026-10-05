import sys, re, json
sys.path.insert(0, '.')
from step4_glm_vision_utils import generate_table_description
NUM_RE = re.compile(r"\d[\d,，.]*%?")
def norm(text):
    out = set()
    for m in NUM_RE.findall(text):
        s = m.replace(",", "").replace("，", "").rstrip("%")
        try: out.add(f"{float(s):.4f}")
        except ValueError: continue
    return out
img = 'announcement_document_processing_local/508610/508610-508610-广发新城吾悦封闭式商业不动产证券投资基金招募说明书-2026-09-18/table_image/page_1008.png'
target = {'1757.8700','2419.8500','2746.7200','2827.2300','425.6300','5907.9400','6557.0600','6967.6100'}
for i in range(3):
    r = generate_table_description(img)
    nums = norm(r)
    hit = target & nums
    print(f"run{i+1}: {len(r)}字, 数字{len(nums)}个, 收入表命中 {len(hit)}/8: {sorted(hit)[:4]}...", flush=True)
    json.dump({'len': len(r), 'nums': sorted(nums)}, open(f'/tmp/b_run{i}.json', 'w'), ensure_ascii=False)
