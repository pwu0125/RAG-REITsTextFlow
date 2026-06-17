#!/bin/bash
# ============================================================
# V0 RAG Pipeline — 8 只新 REITs 全管道
# 508020, 508030, 508093, 508600, 508601, 508602, 508603, 180503
# ============================================================
set -euo pipefail

V0_DIR="/Users/pyemini/projects/REITs/REITs投研平台V0"
CONDA_ENV="deepseek-ocr"
LOG_DIR="$V0_DIR/log"
mkdir -p "$LOG_DIR"

# 激活 conda 环境
source "/Users/pyemini/anaconda3/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"

echo "============================================"
echo "开始时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "Python: $(which python)"
echo "============================================"

cd "$V0_DIR"

# --- Step 2: 文本提取 (最耗时) ---
echo ""
echo ">>> [Step2] 文本提取..."
python step2_extract_text_onlyvactor_multi_process.py 2>&1 | tee "$LOG_DIR/step2.log"
echo ">>> [Step2] 完成 $(date '+%H:%M:%S')"

# --- Step 3_1: 表格检测 (矢量) ---
echo ""
echo ">>> [Step3_1] 表格检测 (矢量PDF)..."
python step3_1_detection_vactor_multi_process.py 2>&1 | tee "$LOG_DIR/step3_1.log"
echo ">>> [Step3_1] 完成 $(date '+%H:%M:%S')"

# --- Step 3_2: 表格检测 (扫描) ---
echo ""
echo ">>> [Step3_2] 表格检测 (扫描PDF)..."
python step3_2_table_detection_scan_multifile.py 2>&1 | tee "$LOG_DIR/step3_2.log"
echo ">>> [Step3_2] 完成 $(date '+%H:%M:%S')"

# --- Step 3_cross: 跨页表格合并 ---
echo ""
echo ">>> [Step3_cross] 跨页表格合并..."
python step3_cross_page_table_detector.py 2>&1 | tee "$LOG_DIR/step3_cross.log"
echo ">>> [Step3_cross] 完成 $(date '+%H:%M:%S')"

# --- Step 4_1_1: 表格图片描述 ---
echo ""
echo ">>> [Step4_1_1] 表格图片描述..."
python step4_1_1_describe_table_images_multi_thread.py 2>&1 | tee "$LOG_DIR/step4_1_1.log"
echo ">>> [Step4_1_1] 完成 $(date '+%H:%M:%S')"

# --- Step 4_1_2: 表格图片描述 (二次) ---
echo ""
echo ">>> [Step4_1_2] 表格图片描述 (二次)..."
python step4_1_2_describe_table_images_multi_thread_second.py 2>&1 | tee "$LOG_DIR/step4_1_2.log"
echo ">>> [Step4_1_2] 完成 $(date '+%H:%M:%S')"

# --- Step 5: 合并表格到文本 ---
echo ""
echo ">>> [Step5] 合并表格到文本..."
python step5_merge_table_into_text.py 2>&1 | tee "$LOG_DIR/step5.log"
echo ">>> [Step5] 完成 $(date '+%H:%M:%S')"

# --- Step 6: 文本切分 ---
echo ""
echo ">>> [Step6] 文本切分..."
python step6_text_segmentation.py 2>&1 | tee "$LOG_DIR/step6.log"
echo ">>> [Step6] 完成 $(date '+%H:%M:%S')"

# --- Step 7: 文本向量化 ---
echo ""
echo ">>> [Step7] 文本向量化..."
python step7_text_embedding.py 2>&1 | tee "$LOG_DIR/step7.log"
echo ">>> [Step7] 完成 $(date '+%H:%M:%S')"

# --- Step 8_1: ES 入库 ---
echo ""
echo ">>> [Step8_1] ES 入库..."
python step8_1_ingest_elasticsearch_data.py 2>&1 | tee "$LOG_DIR/step8_1.log"
echo ">>> [Step8_1] 完成 $(date '+%H:%M:%S')"

# --- 完成 ---
echo ""
echo "============================================"
echo "✅ 全管道完成! $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"

# 输出各 REIT 状态
for code in 508020 508030 508093 508600 508601 508602 508603 180503; do
    echo ""
    echo "--- $code ---"
    for dir in "/Users/pyemini/projects/REITs/REITs投研平台V0/announcement_document_processing_local/$code"/*/; do
        meta="$dir/meta.json"
        if [ -f "$meta" ]; then
            echo "  $(basename "$dir"):"
            python3 -c "
import json
with open('$meta') as f:
    m = json.load(f)
for k in ['text_extracted','table_detection_vector_done','table_describe_done','merge_done','text_segmentation','embedding_done','elasticsearch_database_done']:
    v = m.get(k, False)
    print(f'    {k}: {v}')
" 2>/dev/null
        fi
    done
done
