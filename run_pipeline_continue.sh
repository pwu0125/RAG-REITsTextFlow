#!/bin/bash
# ============================================================
# V0 RAG Pipeline — 断点续跑：Step3_2 → Step8
# ============================================================
set -euo pipefail

V0_DIR="/Users/pyemini/projects/REITs/REITs投研平台V0"
LOG_DIR="$V0_DIR/log"
mkdir -p "$LOG_DIR"

source "/Users/pyemini/anaconda3/etc/profile.d/conda.sh"
conda activate deepseek-ocr

echo "============================================"
echo "续跑开始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "已完成: Step2, Step3_1"
echo "============================================"

cd "$V0_DIR"

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

# --- Step 4_1_1: 表格图片描述 (多线程, 调用 qwen-vl-ocr) ---
echo ""
echo ">>> [Step4_1_1] 表格图片描述..."
python step4_1_1_describe_table_images_multi_thread.py 2>&1 | tee "$LOG_DIR/step4_1_1.log"
echo ">>> [Step4_1_1] 完成 $(date '+%H:%M:%S')"

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
echo ">>> [Step7] 文本向量化 (DashScope)..."
python step7_text_embedding.py 2>&1 | tee "$LOG_DIR/step7.log"
echo ">>> [Step7] 完成 $(date '+%H:%M:%S')"

# --- Step 8_1: ES 入库 ---
echo ""
echo ">>> [Step8_1] ES 入库..."
python step8_1_ingest_elasticsearch_data.py 2>&1 | tee "$LOG_DIR/step8_1.log"
echo ">>> [Step8_1] 完成 $(date '+%H:%M:%S')"

echo ""
echo "============================================"
echo "✅ 续跑完成! $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
