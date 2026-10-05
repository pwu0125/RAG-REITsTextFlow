# file_paths_config.py — REITs Text Data Pipeline
# 所有路径限定在 /Users/pyemini/REITs/ 内

# PDF 原始文件目录（扁平化符号链接，指向 REITs_notice/ 子目录）
PDF_DIR = r"/Users/pyemini/REITs/2_公告数据/2_原始公告/_flat_pdfs"

# RAG 管道输出目录（结构: {fund_code}/{doc_type}/{pdf_folder}/）
OUTPUT_DIR = r"/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/announcement_document_processing_local"

# table_transformer 模型路径
table_transformer_path = r"/Users/pyemini/REITs/2_公告数据/3_提取管道/RAG-REITsTextFlow/table-transformer-detection"
