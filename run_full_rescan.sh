#!/bin/bash
# ============================================================
# RAG Pipeline — 全量重扫 (Steps 2-7)
# 工作目录: /Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/
# 数据: 1,465 核心文档 (年报+中报+季报+招募说明书)
# ============================================================
set -euo pipefail

WORK_DIR="/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow"
CONDA_ENV="deepseek-ocr"
LOG_DIR="$WORK_DIR/log_rescan"
mkdir -p "$LOG_DIR"

# 激活 conda 环境
source "/Users/pyemini/anaconda3/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"

TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
echo "============================================"
echo "RAG 全量重扫启动: $TIMESTAMP"
echo "Python: $(which python)"
echo "============================================"

cd "$WORK_DIR"

# === Step 2: 文本提取 (pdfplumber) ===
echo ""
echo ">>> [Step2] 文本提取 $(date '+%H:%M:%S')"
python step2_extract_text_onlyvactor_multi_process.py 2>&1 | tee "$LOG_DIR/step2.log"
echo ">>> [Step2] 完成 $(date '+%H:%M:%S')"

# === Step 3_1: 表格检测 (矢量PDF) ===
echo ""
echo ">>> [Step3_1] 表格检测 (矢量) $(date '+%H:%M:%S')"
python step3_1_detection_vactor_multi_process.py 2>&1 | tee "$LOG_DIR/step3_1.log"
echo ">>> [Step3_1] 完成 $(date '+%H:%M:%S')"

# === Step 3_2: 表格检测 (扫描PDF) ===
echo ""
echo ">>> [Step3_2] 表格检测 (扫描) $(date '+%H:%M:%S')"
python step3_2_table_detection_scan_multifile.py 2>&1 | tee "$LOG_DIR/step3_2.log"
echo ">>> [Step3_2] 完成 $(date '+%H:%M:%S')"

# === Step 3_cross: 跨页表格合并 ===
echo ""
echo ">>> [Step3_cross] 跨页表格合并 $(date '+%H:%M:%S')"
python step3_cross_page_table_detector.py 2>&1 | tee "$LOG_DIR/step3_cross.log"
echo ">>> [Step3_cross] 完成 $(date '+%H:%M:%S')"

# === Step 4_1_1: 表格图片描述 (qwen-vl-ocr) ===
echo ""
echo ">>> [Step4_1_1] 表格图片描述 $(date '+%H:%M:%S')"
python step4_1_1_describe_table_images_multi_thread.py 2>&1 | tee "$LOG_DIR/step4_1_1.log"
echo ">>> [Step4_1_1] 完成 $(date '+%H:%M:%S')"

# === Step 4_2_1: 非表格图片描述 ===
echo ""
echo ">>> [Step4_2_1] 非表格图片描述 $(date '+%H:%M:%S')"
python step4_2_1_describe_not_table_images_llm.py 2>&1 | tee "$LOG_DIR/step4_2_1.log"
echo ">>> [Step4_2_1] 完成 $(date '+%H:%M:%S')"

# === Step 5: 合并表格到文本 ===
echo ""
echo ">>> [Step5] 合并表格到文本 $(date '+%H:%M:%S')"
python step5_merge_table_into_text.py 2>&1 | tee "$LOG_DIR/step5.log"
echo ">>> [Step5] 完成 $(date '+%H:%M:%S')"

# === Step 6: 文本切分 ===
echo ""
echo ">>> [Step6] 文本切分 $(date '+%H:%M:%S')"
python step6_text_segmentation.py 2>&1 | tee "$LOG_DIR/step6.log"
echo ">>> [Step6] 完成 $(date '+%H:%M:%S')"

# === Step 7: 文本向量化 ===
echo ""
echo ">>> [Step7] 文本向量化 $(date '+%H:%M:%S')"
python step7_text_embedding.py 2>&1 | tee "$LOG_DIR/step7.log"
echo ">>> [Step7] 完成 $(date '+%H:%M:%S')"

echo ""
echo "============================================"
echo "✅ Steps 2-7 全量完成! $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================"
