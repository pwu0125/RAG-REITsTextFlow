#!/bin/bash
# ═══════════════════════════════════════════════════════════
# one-click-test.sh — 一键管道测试: B2g 批次 + 自动发现
# ═══════════════════════════════════════════════════════════
# 自动扫描 raw/ → 注册新 PDF → 跑全管道 step1→step8

set -euo pipefail
cd /Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow

PYTHON=/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python
LOG_DIR=/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/log
mkdir -p "$LOG_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$LOG_DIR/oneclick_B2g_${TIMESTAMP}.log"

echo "════════════════════════════════════════"
echo "  一键管道测试: B2g @ $(date '+%Y-%m-%d %H:%M:%S')"
echo "════════════════════════════════════════"
echo ""

# 1. 自动发现 raw/ 中新 PDF
echo "📂 扫描 raw/ 目录..."
RAW_COUNT=$(ls announcement_document_raw/*.pdf 2>/dev/null | wc -l | tr -d ' ')
echo "   raw/ 中 PDF 数量: $RAW_COUNT"

# 2. 运行 pipeline_controller（自动发现 + 全管道）
echo ""
echo "🚀 启动 pipeline_controller B2g..."
$PYTHON pipeline_controller.py B2g 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

echo ""
echo "════════════════════════════════════════"
if [ $EXIT_CODE -eq 0 ]; then
    echo "  ✅ 管道完成"
else
    echo "  ❌ 管道失败 (exit=$EXIT_CODE)"
fi
echo "  日志: $LOG_FILE"
echo "════════════════════════════════════════"

exit $EXIT_CODE
