#!/usr/bin/env python3
"""
check_env.py — 管道运行前环境预检
验证：Python依赖、Docker容器、磁盘空间、manifest完整性
"""
import json, os, subprocess, sys, shutil

BASE = os.path.dirname(os.path.abspath(__file__))
ERRORS = []

def check(desc, ok, detail=""):
    status = "✅" if ok else "❌"
    print(f"  {status} {desc}")
    if not ok and detail:
        ERRORS.append(f"{desc}: {detail}")
    return ok

print("=== REITs Pipeline Pre-flight Check ===\n")

# 1. Python deps
print("1. Python Dependencies")
try:
    import torch; check("torch", True)
except: check("torch", False, "pip install torch torchvision")
try:
    import transformers; check("transformers", True)
except: check("transformers", False, "pip install transformers")
try:
    import dashscope; check("dashscope", True)
except: check("dashscope", False, "pip install dashscope")
try:
    import elasticsearch; check("elasticsearch", True)
except: check("elasticsearch", False, "pip install elasticsearch")
try:
    import faiss; check("faiss-cpu", True)
except: check("faiss-cpu", False, "pip install faiss-cpu")

# 2. Docker
print("\n2. Docker Containers")
r = subprocess.run(["docker", "ps", "--format", "{{.Names}} {{.Status}}"], capture_output=True, text=True)
containers = {}
for line in r.stdout.strip().split("\n"):
    if line:
        parts = line.split(" ", 1)
        containers[parts[0]] = parts[1] if len(parts) > 1 else ""
for name in ["reits-es", "reits-mysql"]:
    check(name, name in containers, f"container not running: {containers}")

# 3. Disk
print("\n3. Disk Space")
stat = shutil.disk_usage(BASE)
gb = stat.free / (1024**3)
check(f"Free: {gb:.1f} GB", gb > 5, f"only {gb:.1f} GB free")

# 4. FAISS 索引（替代已退役的 Milvus）
print("\n4. FAISS Index")
faiss_index = os.path.abspath(os.path.join(BASE, "..", "..", "5_分析结果", "faiss_data", "reits_faiss.index"))
check("faiss index", os.path.isfile(faiss_index), f"missing: {faiss_index}")

# 5. Manifest
print("\n5. Manifest Integrity")
manifest_path = os.path.join(BASE, "announcement_document_processing_local", "processed_files_local.json")
try:
    with open(manifest_path) as f:
        m = json.load(f)
    count = len(m.get("files", {}))
    check(f"Manifest ({count} docs) valid JSON", True)
except Exception as e:
    check(f"Manifest", False, str(e))

# 6. TableTransformer model
print("\n6. TableTransformer Model")
tt_path = os.path.join(BASE, "table-transformer-detection")
check("model dir", os.path.isdir(tt_path), f"{tt_path} not found")

# Summary
print(f"\n{'='*40}")
if ERRORS:
    print(f"❌ {len(ERRORS)} errors found:")
    for e in ERRORS:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("✅ All checks passed")
