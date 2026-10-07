# AGENTS.md — REITs Text Data Pipeline (RAG-REITsTextFlow)

> **本文档适用于所有在此目录工作的 AI 智能体**（Claude Code、Codex、Cursor、Gemini CLI、Hermes、TRAE 等，不限于特定工具）。
> 开始任何任务前，先读完本文档。父目录 `~/REITs/AGENTS.md` 是项目总纲，两份都适用时以本文件（管道细则）为准。

## 🚫 数据完整性铁律（2026-07-23 确立，永久生效）

**任何情况下不得违反以下规则：**

1. **禁止手动编辑 `meta.json`** — 所有标志（`merge_done`、`embedding_done`、`elasticsearch_database_done` 等）必须由管道脚本写入。脚本卡住 → 修脚本，不修输出。
2. **禁止手动编辑 `processed_files_local.json`** — 仅 `rebuild_manifest.py` 可写入。添加文件 → 走 `link_pdfs_to_flat.py` + pipeline steps。
3. **状态查询优先级**：ES/磁盘文件 > meta.json 标志。当询问"有哪些数据"时，先查 ES/FAISS/文件系统，再用 meta.json 交叉验证。**绝不**用 meta.json 标志作为唯一结论来源。
4. **管道失败处理**：`verify_data_integrity.py` → 报告不一致 → 修复管道脚本根因 → 重跑步骤。禁止跳过步骤直接改标志。
5. **每次故障排查后**运行 `python verify_data_integrity.py` 确认 meta.json 与 ES/磁盘一致。

## Project Purpose

Extracts text and tables from REIT (Real Estate Investment Trust) prospectus PDFs, generates structured text descriptions for tables/images via LLM APIs (DashScope, DeepSeek OCR), merges table descriptions back into extracted text, segments and embeds the documents, and ingests them into Elasticsearch + FAISS for RAG retrieval.

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
├── db_config.py                     # MySQL / ES connection config
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
├── step8_2_ingest_vector_database.py     # FAISS 向量标志同步（索引由 build_faiss_index.py 全量重建）
├── create_elasticsearch_index.py    # ES index setup
├── create_vector_database.py        # Milvus collection setup（已废弃，FAISS 见 build_faiss_index.py）
└── es_bulk_ingest.py                # Bulk ES ingestion utility
```

## Pipeline Steps (1–8) (结构化提取已内置在 step4.1.1 OCR阶段)

| Step | Script | Description |
|------|--------|-------------|
| 1 | `step1_process_pdfs.py` | Render PDF pages as PNG images into `temp_pdf_images/` |
| 2 | `step2_extract_text_onlyvactor_multi_process.py` | Extract plain text from rendered images, output `text.json` |
| 3.1 | `step3_1_detection_vactor_multi_process.py` | Vector-based table detection on each page |
| 3.2 | `step3_2_table_detection_scan_multifile.py` | Scan-based table detection using TableTransformer + cross-page merging; copies table images to `table_image/` |
| 4.1.1 | `step4_1_1_describe_table_images_multi_thread.py` | **Main entry**: LLM table description via DashScope (parallel, multi-threaded), writes `table_describe.json`。**🆕 内置OCR阶段结构化提取**: 自动检测「可比实例」表 → DashScope qwen-turbo 文本解析 → 并行写入 `comp_rents_structured.json` |
| 4.1.2 | `step4_1_2_describe_table_images_multi_thread_second.py` | Second-pass table description for difficult cases |
| 4.2.1 | `step4_2_1_describe_not_table_images_llm.py` | LLM description of non-table images, writes `not_table_describe.json` |
| 5 | `step5_merge_table_into_text.py` | Merge `table_describe.json` into `text.json`, deletes intermediate images |
| 6 | `step6_text_segmentation.py` | Segment merged text into chunks for embedding |
| 7 | `step7_text_embedding.py` | Generate text embeddings |
| 8.1 | `step8_1_ingest_elasticsearch_data.py` | Ingest into Elasticsearch for keyword search |
| 8.2 | `step8_2_ingest_vector_database.py` | FAISS 向量标志同步（索引由 build_faiss_index.py 全量重建） |

## Key Entry Points

- **增量更新（推荐入口）**: `bash /Users/pyemini/.hermes/scripts/reits_update_cycle.sh`（下载→处理→入RAG 全链，自带 ES 探活自愈，stdout 输出 NO_NEW 或逐条清单）
- **Batch orchestration**: `python run_batch.py <batch> --full` (full pipeline with quality gates) or `python run_batch.py <batch> <script.py>` (single step)
- **Manual step execution**: `python step4_1_1_describe_table_images_multi_thread.py` (reads manifest, processes pending files)
- **Continuation**: `bash run_pipeline_continue.sh` (resume from where left off)
- **New REITs**: `bash run_new_reits_pipeline.sh`

## Python Environment

- **Path**: `/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python`
- **Key dependencies**: pytorch, transformers (TableTransformer), opencv-python, Pillow, pymysql, dashscope, openai, pytesseract, elasticsearch, faiss-cpu
- **Local models**: TableTransformer stored at `table-transformer-detection/` (offline HF mode via `HF_HUB_OFFLINE=1`)
- **OCR backend**: Configurable via `OCR_BACKEND` env var (`local` for DeepSeek OCR 2 MPS, `api` for DashScope/API)
- **运行前置**: 某些智能体 VM（如 TRAE）会注入冲突的 `PYTHONHOME`，报 `No module named 'encodings'` 时在命令前加 `env -u PYTHONHOME -u PYTHONPATH`

## Infrastructure Dependencies（外部服务）

- **Elasticsearch 跑在 Docker 容器 `reits-es` 中（不是 brew！）**。9200 拒连时先跑 `bash /Users/pyemini/.hermes/scripts/es_heal.sh`（探活自愈，详见陷阱86）。
- **Docker Desktop 不随 macOS 开机自启**。Mac 重启后第一次管道运行由 es_heal.sh 自动拉起 Docker + ES，首次可能多花 1-3 分钟。

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

## 🔴 陷阱86-87（2026-10-04 故障复盘：RAG 检索失败）

**陷阱86：ES 没在 brew 里——它在 Docker 容器 `reits-es` 中。**
- 症状：step8_1 批量报 `Connection refused 127.0.0.1:9200`，FAISS 线正常。
- 根因：Docker Desktop 未启动（重启/崩溃后不自启）。brew 版 elasticsearch-full 是残留安装（日志停在 2026-03，数据目录仅 76K），排查时不要被它误导。
- 修复：`open -a Docker` → 容器自启（重启策略）→ 30 秒内 9200 恢复。之后必须：①`verify_data_integrity.py` 对齐标志 ②重跑 `step8_1` 回补 ES 断连期间失败的公告。
- **已自动化**：`es_heal.sh` 探活自愈脚本已挂在增量管道入口（reits_update_cycle.sh 第 0 步），自愈失败会计入 ERR_MSG 报警。

**陷阱87：verify_data_integrity.py --fix 的前缀匹配 bug（v1.12 已修）。**
- 旧版 `fix_integrity` 用 `doc[:60]` 截断 + 前 20 字符前缀匹配定位文件夹；同一基金下公告文件名前 20 字几乎全同（"180101-博时招商蛇口产业园封闭式基础设施…"），导致 3924 次"修复"写到错误文档，且复核永远显示不一致（42.2% 假象）。
- v1.12 修复：条目格式改 `code|完整目录名`，精确路径匹配。
- 铁律：**--fix 后必须重跑 verify，数字没动 = 修错了文件，禁止视为"已修复"**。

## 智能体协作规范（通用）

无论你是哪个智能体，在此目录工作时：

1. **动手前**：读完本文件 + 父目录 `~/REITs/AGENTS.md`（项目总纲）。
2. **改脚本**：改完立即记录到本文件（新增陷阱/约定），让下一个智能体受益。
3. **跑管道**：优先走增量入口（reits_update_cycle.sh），不要直接跑 step 脚本全量。
4. **报数字**：任何「已完成/已修复」结论必须来自磁盘实查（ES 计数、文件数、verify 脚本输出），不来自记忆或推断。
5. **文档即契约**：AGENTS.md 是唯一权威；CLAUDE.md 仅是指向它的兼容指针，不要往 CLAUDE.md 写内容。

## 🔴 陷阱88（2026-10-04：is_core_doc 与分类器脱钩，文档永久失明）

**现象**：周六下载的 4 条 core_lite 公告（508008/508069 运营数据、508055 扩募×2）目录和 meta.json 都在，但永远不进 manifest、不被管道处理。
**根因**：`rebuild_manifest_v3.py` 的 `is_core_doc` 用独立的旧关键词表（年度报告/招募说明书/分红/收益分配/评估报告），**不含** classify_policy.json v2.1 里 core_lite 的「运营数据/扩募」等词——两套规则脱钩，这类文档建了目录就“失明”。
**修复（2026-10-04）**：`is_core_doc` 末尾接入分类器兜底——命中旧关键词 或 classify() 判 core_full/core_lite 均算核心。对齐后 manifest 从 2362 → 3189 篇（827 份历史 core_lite 一并正确纳入，catchup 会按 25 篇/批逐步消化）。
**铁律**：收录范围变更只改 classify_policy.json（唯一真相源），别再动 CORE_KEYWORDS；改完必须重建 manifest 并抽查新增数。

## 🔴 陷阱89（2026-10-04：before 快照格式错误被静默吞掉 → 全量重扫）

**现象**：手工构造的 before 快照格式不对（把整个 manifest dict 当快照传入），管道不报错，直接把全部 3189 篇文档当「新增」逐篇重扫。
**根因**：`reits_daily_rag.py load_before_keys` 对格式不识别的快照**静默返回空集**——空集 = 全部都是新的。静默降级是反模式：错误场景必须大声失败。
**修复（2026-10-04）**：快照不存在/不可读/格式错误一律 `SystemExit` 带明确报错；仅 `{"keys": []}`（调用方明确意图）返回空集。四场景单测通过。
**铁律**：任何「增量语义」的入口，对坏输入的默认行为必须是拒绝而非降级为全量。

## 🔴 陷阱90（2026-10-04：meta/text 双标志分歧 → catchup 假滞留，重复烧 API）

**现象**：catchup 队列虚报 845 份，其中 571 份是假滞留（meta.json ES=True，text.json.metadata 旧值=False，而 ES 里实际有数据）——每个周日 catchup 都重复处理它们，白烧 LLM 表格描述 API 费。
**根因**：① catchup 的 `doc_es_done` 用「text.json 覆盖 meta.json」的合并语义，单侧旧值可把 True 拉回 False；② 历史上 verify --fix 只写 meta 不写 text，错误运行的秒跳分支只改一侧，分歧从未收敛。
**修复（2026-10-04 双管齐下）**：
- 数据侧：修复脚本以「ES 实况」为仲裁，569 份 text.json 已同步（备份在 manifest_backups/text_meta_sync_20261004/）；2 份 ES 实无的保留 False 交人工。
- 代码侧：`doc_es_done` 改为「任一侧 True 即完成」，不再让旧值否决新值。
**铁律**：多源标志判完成，语义必须是「任一完成即完成」；写标志必须双写（meta+text），单侧写=埋雷。

## 🔴 陷阱91（2026-10-04：text_extracted=True + 空 text.json 死锁；修复时误伤 8349 份已全部还原）

**现象**：meta 标志 `text_extracted=True` 但 text.json 页数=0（如 180606 交割审计公告）——step2「没有文件需要处理」、step6「没有页面数据」，全链静默跳过，永不入库。
**根因**：step2 历史上某次中断只写了标志没写内容（与 docs/pitfall-text-extracted-flag.md 记载的 19 份丢失同源）。
**⚠️ 修复事故（必须记住）**：首轮修复脚本对 text.json 判空时把 pages 当 list（实际是 dict，键为页码字符串），导致 8,349 份正常文档被误判为空、meta/text 被重置。**已全量从备份还原**（备份：manifest_backups/empty_text_reset_20261004/，还原后 text_extracted=True 8349 份全部恢复，ES 全程未受影响）。
**正确鉴定姿势**：pages 是 dict → `len(pages)`；逐页正文在 `page['text']`。正确口径下的真死锁 = **940 份**（non_core 916 + core_lite 24，全部 ES=False，无一在 RAG 库中）。
**铁律**：
1. 修复脚本动手前必须先「只读鉴定」出准确清单，再按清单精确执行——禁止边扫边改。
2. 判空逻辑必须适配真实数据结构（pages 是 dict 不是 list）——先看一份正常样例的键结构再写判定。
3. 批量改动前备份先行；还原路径必须可逆（tag = 现存目录反解，不猜路径）。

## 🛡️ 三项根治加固（2026-10-04 用户批准，陷阱87-91 的防复发层）

**1. verify_data_integrity.py --fix 双写**：新增 `_write_flag_dual()`——修标志时 meta.json 与 text.json.metadata 同轮写入。从此 --fix 不再制造单侧分歧（陷阱90 的诞生源）。

**2. step2 置位防线（陷阱91 根治）**：`text_extracted=True` 落盘前自检 text.json 内容物——页数=0 或总字数<50 → 拒绝置位、打印「拒绝置位」、写日志、文档留在待处理队列。僵尸文档从此不可能诞生。final_data 预初始化防 0 页循环未绑定。

**3. read_doc_status 单调合并 + 内容校验（reits_daily_rag.py）**：
- 单调布尔标志（`*_done`/`*_extracted`）合并语义改为「任一侧 True 即 True」——旧 False 不再否决新 True（陷阱90 消费侧根治）。
- text_extracted=True 但 text.json **存在且 0 页** → 拉回 False 重跑（陷阱91 消费侧防线）；text.json 不存在则保守保留标志（交 step2 防线把关），不误伤。

**单测**：8 场景全过（任一True/对侧True/僵尸拉回/正常不误伤/无text.json不崩/双写pages无损/防线顺序正确）。
**边界教训**：文件不存在 ≠ 空文件，防线必须区分；测试脚本自身的 bug 不代表产品 bug。

## 🗄️ B线归档（2026-10-05 用户裁定：维持A线）

GLM-5.3-Flash 替代 qwen 视觉线的评估已完成并归档至 `archived/bline_glm_20261005/`（含A/B测试数据、结论README）。要点：数字质量基本达标(Jaccard 0.965)但偶发缩水(同图5测2次漏整张收入表)+跨页合并图(占24%)连续空返回+API直连夜间五折不免费(全量约1.23万分)。A线继续为唯一表格描述通道。复活条件见归档README。

## 🔴 陷阱95（2026-10-06：step2 本地 OCR 内存膨胀引发内核 panic；本地通道对扫描件不可用）

**现象**：RAG 补入库夜间运行 step2 时机器整机卡死 94 秒 → watchdog 内核 panic 强制重启（21:44，panic 报告 `panic-full-2026-10-06-214442`，压缩器 100% + 68 个 swapfile）。之后实测：worker 处理 508021 概要类扫描件时 phys_footprint 10 分钟 30G→38G 单调上涨；另一 worker 起步 2 分钟即 21G。
**根因（两层叠加）**：
1. DeepSeek-OCR-2 的 MPS 推理在**单页 generate 内部**即持续膨胀（max_new_tokens=8192 + use_cache=True，页后 empty_cache 救不了页内失控；10-05 陷阱94修复加入的 OCR 路由首次让本地通道吃到真实扫描页流量）；
2. ProcessPoolExecutor worker 常驻 → 6.3G 模型 ×3 worker + MPS 缓存累积不归还系统；叠加 Docker VM 当时 12G 锁定，把 24G 机器直接压死。
**修复（2026-10-06/07，三道防线）**：
1. `mps_backend.py::infer` finally 中 `gc.collect() + torch.mps.synchronize() + torch.mps.empty_cache()`（页后回收）；
2. `step2` 的 Pool 加 `max_tasks_per_child=1`（每 worker 处理 1 个 PDF 即退出，模型随之释放）；
3. `ocr_router.py` 加进程足迹熔断（top 口径，默认 6G，`LOCAL_OCR_MEM_LIMIT_GB` 可调）超限自动降级 API；另配外部看门狗 `~/.hermes/scripts/ocr_mem_watchdog.sh`（>7G 熔断 KILL，双保险）。
**运营裁定（2026-10-07 凌晨实测后）**：即使打了补丁，本地 MPS 通道跑真实扫描件仍在页内膨胀（9 分钟 27G、单页未出）。**夜间无人值守批量一律 `STEP2_OCR_BACKEND=api` 强制 API**（符合 2026-09-26「本地仅小活辅助」裁定）；本地通道只允许交互式在场使用。
**附带发现**：①Docker VM 内存已从 12G 降到 8G（settings-store.json，备份 .bak-20261006）；②CC CLI 通道 403 鉴权失效（api_key_source: payg，上游为 cc-switch 代理 127.0.0.1:15721）待修；③机器 swap 残留 14G 需自然重启归零。待修清单见 `docs/pending_fixes_20261007.md`。

## 🔴 陷阱96（2026-10-07：verify 只做单向对账 → 16 份标志虚高文档漏网）

**现象**：verify_data_integrity.py 全绿（"100% 对齐"），但 16 份文档 ES 实无数据——10 份假空壳（text 在盘、step8 从未成功）+ 6 份真空壳（纯扫描件全链未跑）长期失明。
**根因**：verify 只查「ES 有 → 标志对不对」（正向）；「标志 True → ES 实有」（反向）是盲区。历史修复对齐标志时按 ES 单向校验通过就置 True，反向缺口永不可见。
**修复（2026-10-07）**：verify_data_integrity.py 主流程追加反向对账区块——拉全量 ES source_file（composite 聚合，**带 .pdf 后缀**）与 manifest 中 ES=True 键集求差。终验反向 = 0。
**铁律**：状态对账必须双向。单侧校验通过 ≠ 数据在库。

## 🔴 陷阱97（2026-10-07：ES source_file 键带 .pdf 后缀——核查脚本键口径必须一致）

**现象**：多轮"ES 缺 N 份"审计误报（含 16 份回补的发现过程走了弯路）；早期"7137 差额 0"验收也带毒。
**根因**：step8_1 写入 ES 的 source_file = manifest 键（带 .pdf）；我的核查脚本用目录名（不带后缀）构造 term 查询 → 永远 0 命中。
**铁律**：对 ES 做 source_file 精确查询时，键必须带 .pdf 后缀（= manifest 键原样）。凡写"查某文档在不在 ES"的脚本，先跑一份已知在库文档校准键口径。

## 🔴 陷阱98（2026-10-07：step6 偶发 global_id 裸化 → ES _id 碰撞，入库"成功"但查无此文档）

**现象**：508015 明阳 2026 第一次收益分配公告——step8 报"入库成功"，ES 按文档查 0 段。text_segmentation.json 里 8 个 chunk 的 global_id 是裸的 `_1`~`_8`（正常应为 `目录名_N`）。
**根因**：step6 生成 chunk 时目录名变量为空（具体触发路径待查——这是 RAG 侧未修的代码根因）。global_id 即 ES _id，裸 `_N` 与其它文档碰撞互相覆盖。
**临时处置**：八标志双写回退 + 坏 segmentation 移出 + 全链重跑（9 段 2112 字正常入库）。
**根治（2026-10-07 晚，双层防御已落地）**：
- 生产端（step6:438）：`base_name` 为空时 raise ValueError 拒绝切分——裸 global_id 从源头不可能再生成；
- 入库端（step8_1:237）：_id 形态校验（必须为 `<目录名>_<N>` 且长度>8），可疑 _id 拒绝入库并计数（`rejected`）。单元验证 4/4 用例通过（正常 id 放行、裸 _N 拒、纯数字拒、短目录名放行）。
**铁律**：入库脚本必须校验 _id 构造完整性；_id 碰撞是静默数据丢失（后写覆盖先写），不报错。

## 🔴 陷阱99（2026-10-07：rebuild_manifest 会复活已删的判重键——根治需物理移目录）

**现象**：508601 单前缀 05-15 招募书与双前缀 05-15 为同一文档两个目录名（首页逐字相同）。从 manifest 删除后，下一次 rebuild_manifest_v3 又把它加回来（目录在盘即收录）。
**修复**：判重文档的目录物理移入 `manifest_backups/dup_removed_20261007/`（原始 PDF 仍在 2_原始公告，无数据损失）。
**铁律**：manifest 删除 ≠ 收录范围变更。要让键永久消失，要么物理移走目录，要么改 is_core_doc/classify_policy 收录规则。
