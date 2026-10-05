#!/bin/bash
# run_508066_pipeline.sh — 508066招募说明书 全量提取管道
set -e
cd /Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow
PY=/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python
DOC="2022-05-06"
LOG=/tmp/pipeline_508066_$(date +%Y%m%d_%H%M%S).log
exec > >(tee -a "$LOG") 2>&1

echo "=============================================="
echo " 508066 招募说明书 管道提取 "
echo " 开始时间: $(date)"
echo "=============================================="

step() {
    echo ""
    echo ">>> [$1] $(date)"
    $PY "$2" --doc "$DOC"
    echo "<<< [$1] OK"
}

step "Step3.1 向量表检测"   step3_1_detection_vactor_multi_process.py
step "Step3.2 扫描表检测"   step3_2_table_detection_scan_multifile.py
step "Step4.1.1 表描述LLM"  step4_1_1_describe_table_images_multi_thread.py
step "Step4.2.1 非表描述LLM" step4_2_1_describe_not_table_images_llm.py
step "Step5 合并+GATE1"     step5_merge_table_into_text.py --gate 508066_solo
step "Step6 文本分段"        step6_text_segmentation.py
step "Step7 向量嵌入"        step7_text_embedding.py
step "Step8.1 ES入库+GATE2"  step8_1_ingest_elasticsearch_data.py
step "Step8.2 Milvus入库"    step8_2_ingest_vector_database.py

echo ""
echo "=============================================="
echo " ✅ 管道完成: $(date)"
echo " 日志: $LOG"
echo "=============================================="
