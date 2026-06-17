# CLAUDE.md — REITs Text Data Pipeline (RAG-REITsTextFlow)

## Project Purpose

Extracts text and tables from REIT (Real Estate Investment Trust) prospectus PDFs, generates structured text descriptions for tables/images via LLM APIs (DashScope, DeepSeek OCR), merges table descriptions back into extracted text, segments and embeds the documents, and ingests them into Elasticsearch + Milvus for RAG retrieval.

## Directory Structure

```
RAG-REITsTextFlow/
├── announcement_document_raw/       # Raw PDF source files (flat, named with fund codes)
├── announcement_document_processing_local/  # Main output: fund_code/doc_folder/
│   ├── processed_files_local.json   # Master manifest tracking all processed files
│   └── {fund_code}/{pdf_name}/      # Per-document output:
│       ├── meta.json                # Processing status flags (text_extracted, table_describe_done, etc.)
│       ├── text.json                # Extracted/staged text (pages with metadata)
│       ├── table_describe.json      # LLM-generated table descriptions
│       ├── not_table_describe.json  # Non-table image descriptions
│       ├── temp_pdf_images/         # Intermediate: rendered PDF page images
│       └── table_image/             # Intermediate: detected table crops
├── log/                             # Per-step log files
├── log_rescan/                      # Rescan-specific logs
├── manifest_backups/                # Timestamped manifest snapshots
├── table-transformer-detection/     # Offline TableTransformer model (HuggingFace)
├── output/images/                   # Misc debug output
├── page_images/                     # Temp image staging
├── debug_output/                    # Debug artifacts
│
├── file_paths_config.py             # PDF_DIR, OUTPUT_DIR, table_transformer_path
├── common_utils.py                  # SafeJSONEncoder, safe_json_dump/load helpers
├── db_config.py                     # MySQL / Milvus / ES connection config
├── model_config.py                  # LLM model registry (ali, deepseek, zhipu, kimi)
├── ocr_router.py                    # OCR backend router (local DeepSeek OCR vs API)
│
├── run_batch.py                     # Batch orchestrator: single-step or full pipeline with quality gates
├── rebuild_manifest.py              # Rebuild manifest from disk meta.json files
├── run_full_rescan.sh               # Shell wrapper for full pipeline rescan
├── run_new_reits_pipeline.sh        # Shell wrapper for new REITs processing
├── run_pipeline_continue.sh         # Continuation/resume script
├── run_pipeline_step4_8.sh          # Step 4-8 partial pipeline
├── scan_bad_images.py               # Image quality scanner
│
├── step1_process_pdfs.py            # PDF → temp_pdf_images/ (page PNGs)
├── step2_extract_text_onlyvactor_multi_process.py  # Extract text via vector/vactor processing
├── step3_1_detection_vactor_multi_process.py       # Vector-based table detection
├── step3_2_table_detection_scan_multifile.py       # Scan-based table detection (TableTransformer)
├── step3_cross_page_table_detector.py              # Cross-page table detection logic
├── step4_1_1_describe_table_images_multi_thread.py # LLM table description (DashScope, multi-threaded)
├── step4_1_2_describe_table_images_multi_thread_second.py  # Second-pass table description
├── step4_2_1_describe_not_table_images_llm.py      # Non-table image LLM description
├── step4_2_2_describe_not_table_images_llm_second.py       # Second-pass non-table description
├── step4_compress_image.py          # Image compression for API retry
├── step4_describe_not_table_images_PaddleOCR.py    # PaddleOCR fallback for non-table images
├── step4_table_utils*.py            # Table description utilities (ali, multi-thread variants)
├── step5_merge_table_into_text.py   # Merge LLM descriptions into text.json
├── step6_text_segmentation.py       # Segment merged text into chunks
├── step7_text_embedding.py          # Generate embeddings (vector DB preparation)
├── step8_1_ingest_elasticsearch_data.py  # Ingest into Elasticsearch
├── step8_2_ingest_vector_database.py     # Ingest into Milvus vector DB
├── create_elasticsearch_index.py    # ES index setup
├── create_vector_database.py        # Milvus collection setup
└── es_bulk_ingest.py                # Bulk ES ingestion utility
```

## Pipeline Steps (1–8)

| Step | Script | Description |
|------|--------|-------------|
| 1 | `step1_process_pdfs.py` | Render PDF pages as PNG images into `temp_pdf_images/` |
| 2 | `step2_extract_text_onlyvactor_multi_process.py` | Extract plain text from rendered images, output `text.json` |
| 3.1 | `step3_1_detection_vactor_multi_process.py` | Vector-based table detection on each page |
| 3.2 | `step3_2_table_detection_scan_multifile.py` | Scan-based table detection using TableTransformer + cross-page merging; copies table images to `table_image/` |
| 4.1.1 | `step4_1_1_describe_table_images_multi_thread.py` | **Main entry**: LLM table description via DashScope (parallel, multi-threaded), writes `table_describe.json` |
| 4.1.2 | `step4_1_2_describe_table_images_multi_thread_second.py` | Second-pass table description for difficult cases |
| 4.2.1 | `step4_2_1_describe_not_table_images_llm.py` | LLM description of non-table images, writes `not_table_describe.json` |
| 5 | `step5_merge_table_into_text.py` | Merge `table_describe.json` into `text.json`, deletes intermediate images |
| 6 | `step6_text_segmentation.py` | Segment merged text into chunks for embedding |
| 7 | `step7_text_embedding.py` | Generate text embeddings |
| 8.1 | `step8_1_ingest_elasticsearch_data.py` | Ingest into Elasticsearch for keyword search |
| 8.2 | `step8_2_ingest_vector_database.py` | Ingest into Milvus for vector similarity search |

## Key Entry Points

- **Batch orchestration**: `python run_batch.py <batch> --full` (full pipeline with quality gates) or `python run_batch.py <batch> <script.py>` (single step)
- **Manual step execution**: `python step4_1_1_describe_table_images_multi_thread.py` (reads manifest, processes pending files)
- **Continuation**: `bash run_pipeline_continue.sh` (resume from where left off)
- **New REITs**: `bash run_new_reits_pipeline.sh`

## Python Environment

- **Path**: `/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python`
- **Key dependencies**: pytorch, transformers (TableTransformer), opencv-python, Pillow, pymysql, dashscope, openai, pytesseract, elasticsearch, pymilvus
- **Local models**: TableTransformer stored at `table-transformer-detection/` (offline HF mode via `HF_HUB_OFFLINE=1`)
- **OCR backend**: Configurable via `OCR_BACKEND` env var (`local` for DeepSeek OCR 2 MPS, `api` for DashScope/API)

## Important Conventions

1. **Manifest-driven processing**: `announcement_document_processing_local/processed_files_local.json` is the master manifest. Steps read pending files from it. Individual scripts do NOT write to it — `rebuild_manifest.py` rebuilds it from disk `meta.json` files after each step completes.

2. **Status flags**: Each document's `meta.json` tracks boolean flags: `text_extracted`, `table_detection_scan_done`, `table_detection_vector_done`, `table_describe_done`, `not_table_describe_done`, `merge_done`, `text_segmentation`, `embedding_done`, `elasticsearch_database_done`.

3. **Quality gates** (in `run_batch.py` full pipeline mode):
   - **GATE1** (after step5 merge): Coverage > 99% — checks `table_describe_done` + `not_table_describe_done` + `text.json` exists and > 1KB. If passed: marks `merge_done=True` and deletes intermediate images. Fails: images preserved for diagnosis.
   - **GATE2** (after step8_1): Accuracy > 99.5% — verifies ES index document count and field completeness.

4. **JSON encoding**: Always use `common_utils.safe_json_dump` / `safe_json_load` (handles datetime objects). Never use raw `json.dump`/`json.load` for persistence.

5. **Thread safety**: `json_lock = threading.Lock()` used in step4_1_1 and step5 for concurrent JSON writes.

6. **API resilience**: LLM API calls have retry logic (3 attempts default). Image compression fallback on timeout/connection errors.

7. **Concurrency**: `TABLE_DESC_MAX_WORKERS` env var (default 5) controls thread pool size for step4_1_1.

8. **PDF naming**: Files follow `{fund_code}-{description}-date.pdf` pattern (e.g., `508093-易方达广西北投...基金招募说明书-2026-04-17.pdf`). Output folders mirror pdf basename.

9. **Environment**: `.env` file at project root for API keys (`ALI_API_KEY`, `DASHSCOPE_API_KEY`, `KIMI_API_KEY`, etc.).

10. **No git repo**: This directory is not a git repository. Changes are tracked manually.

## Batch Protocol

- **Always run step scripts through `run_step.py` with a batch name.** Never run step scripts directly — they process ALL documents in the manifest, not just the intended batch.
- **`BATCH_CONFIG.json`** defines the batch scope: each batch maps to a set of fund codes with an expected document count.
- `run_step.py` backs up the manifest, filters it to the batch's fund codes, executes the step script, then restores the full manifest — so the step script sees only batch docs but the manifest is never permanently altered.
- Example:
  ```
  python run_step.py B1a step4_1_1_describe_table_images_multi_thread.py
  ```
- Use `python run_step.py --list` to see all available batches.

## ⚠️ IRON RULE: meta.json ↔ Manifest 同步

**meta.json 和 processed_files_local.json 在任何时刻必须一致。** 两者任一被修改后，必须立即同步另一方。

| 操作 | 同步动作 |
|------|---------|
| 修改 `meta.json` 标志 | → 重建 manifest: `python rebuild_manifest.py` |
| 修改 manifest 标志 | → 回写 meta.json: `python reconcile_meta.py` |
| `run_step.py` 恢复全量 manifest | → 从 meta.json 同步回 manifest（待 FlagSync 修复实现） |
| 手工修改任何一方的标志 | → 运行 `reconcile_meta.py VERIFY_FULL=False` 自动纠正 |

**禁止**：修改 meta.json 后直接启动管道而不先重建 manifest。
**禁止**：修改 manifest 后不运行 reconcile_meta.py 验证。

详见 `docs/pitfall-text-extracted-flag.md` — 违反本规则导致 B1c/B2b/c/e/f/i 共 19 份文档永久缺失。
