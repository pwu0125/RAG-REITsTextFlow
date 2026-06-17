# RAG-REITsTextFlow — REITs 公告 PDF 全自动文本提取管道

> **Fork from** [adennng/RAG-REITsTextFlow](https://github.com/adennng/RAG-REITsTextFlow)  
> **Maintainer**: [pwu0125](https://github.com/pwu0125)  
> **Last update**: 2026-06-17

本项目从原始仓库 fork 而来，在原有 PDF 提取流程基础上进行了**大规模架构重构和工程加固**——新增管道编排器、GATE 质量门控、元数据一致性治理、多级重试与崩溃恢复，使管道从"手动逐步执行"升级为"一键批量自动化运行"。

---

## 📊 生产验证结果

| 指标 | 数据 |
|------|------|
| 处理文档总数 | **1,425 份** REITs 公告 PDF |
| 覆盖 REITs 代码 | **87 个** (全量) |
| Elasticsearch 入库 | **165 万** text chunks |
| Milvus 向量库 | **110 万** embedding vectors |
| 批次通过率 | **65/87 codes** 全链路闭环 |

---

## 🔄 修改内容（与原始仓库对比）

### 🆕 新增文件

#### 管道编排与质量控制
| 文件 | 功能 | 原因 |
|------|------|------|
| `pipeline_controller.py` | **RunConductor** — 批次管道端到端编排器。支持 pre-flight 检查(Docker/磁盘/MetaGuard)、checkpoint/resume、三步重试 | 原仓库需手动逐步运行 12 个脚本，无法批量无人值守执行 |
| `gate1_coverage_check.py` | **GATE1 覆盖率检查** — 验证 table_describe/not_table_describe/text.json 三项完整，≥99% 通过 | 需在 merge 前确保提取质量，不通过则保留图片诊断 |
| `gate2_accuracy_check.py` | **GATE2 精度检查** — 验证 ES 索引文档数+字段完整性，≥99.5% 通过 | 入库前质量门控，防止脏数据进入检索库 |
| `batch_runner.py` | 批次执行器 — 处理批次内的所有 PDF | 简化批量操作 |
| `run_batch.py` | 单步/全量批次入口 | 支持 `--full` 全管道或 `--script stepX` 单步 |
| `run_step.py` | 单步轻量执行器 | 调试和修复时使用 |

#### 元数据治理（三位一体体系）
| 文件 | 功能 | 原因 |
|------|------|------|
| `reconcile_meta.py` | **标记对账** — 以 ES/Milvus 物理数据为真相锚点，按 8 条推理链(R0-R8)修正 meta.json 错误标志 | 原仓库重建 manifest 时只看磁盘文件，被 step5 删除的图片骗到，导致"假阴性" |
| `rebuild_manifest.py` | **manifest 重建** — 从磁盘 meta.json 重建，新增 ES/Milvus 双向交叉校验 | 单向传播导致错误标志扩散 |
| `sync_text_metadata.py` | **标志同步** — text.json metadata → meta.json 同步，防止旧标志覆盖正确值 | step5 merge 时 text.json 可能携带过期 metadata |

#### 诊断与修复工具
| 文件 | 功能 |
|------|------|
| `diagnose_pipeline.py` | 全管道状态诊断（从 ES/Milvus 倒推） |
| `diagnose_pipeline_state.py` | pipeline_state.json 检查 |
| `diagnose_full_disk.py` | 磁盘使用量诊断 |
| `fix_meta_for_recovery.py` | 崩溃恢复标记修复 |
| `fix_meta_phase1.py` | Phase A 全量标记清理(1,425份) |
| `scan_progress.py` | 批次进度扫描 |
| `scan_bad_images.py` | 坏图像扫描 |

#### 基础设施
| 文件 | 功能 |
|------|------|
| `ocr_router.py` | OCR 后端路由（本地 DeepSeek OCR 2 MPS vs 云端 API） |
| `es_bulk_ingest.py` | ES 批量入库工具 |
| `sync_all.py` | 全量三路同步(disk→meta→manifest) |
| `BATCH_CONFIG.json` | 87 个 REITs 代码的 24 批次分组配置 |
| `CLAUDE.md` | AI Agent 上下文文档 |

---

### ♻️ 修改的原有文件

#### step4_1_1 — 表格描述可靠性加固
```diff
+ 单图级 API 重试 + 指数退避(30s/60s/120s)
+ DashScope BrokenPipeError 捕获后自动重试
+ 并发控制优化
```
**原因**: 原始代码单次失败即放弃，B2m 批次 3/28 因代理断连失败。

#### step4_2_1 — 非表描述零图修复
```diff
+ 零图文档（无非表页）不再返回 False
+ 正确写入 ntd_done=true
```
**原因**: 纯矢量 PDF 无非表图，原始代码误判为失败，阻塞后续步骤。

#### step5 — GATE1 两阶段提交
```diff
+ 合并后不立即标记 merge_done（先跑 GATE1）
+ GATE1 通过 → 标记 + 删 temp_pdf_images/
+ GATE1 失败 → 保留图片 + 保留待处理状态 + sys.exit(1)
+ merge 前自动运行 sync_text_metadata
```
**原因**: 原始代码无 GATE1 机制，合并后直接删图——合并质量不可验证，删图后无法诊断。

#### step7 — 嵌入崩溃恢复
```diff
+ 嵌入失败捕获 + 自动重试(3次)
+ 重试耗尽后降级跳过（不阻塞管道）
```
**原因**: B2m 首次运行时 step7 静默崩溃，整个批次无嵌入数据需重跑。

#### step3_2 — Tesseract 异常捕获
```diff
+ 捕获 TesseractNotFoundError → OSError 基类
```
**原因**: macOS 上 pytesseract 抛 OSError 而非 TesseractError，原始 except 未覆盖。

#### step8_1/step8_2 — ES/Milvus 入库增强
```diff
+ 批量写入优化
+ 字段完整性校验
+ 幂等重入（delete + re-insert）
```

---

## 🏗️ 架构设计

### 管道流水线（12 步 → 8 核心步骤）

```
┌─────────────────────────────────────────────────────────────────┐
│                  RunConductor (pipeline_controller.py)            │
│                                                                  │
│  Pre-flight: reconcile_meta → Docker → Disk                     │
│       ↓                                                         │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ Step1 PDF→PNG                                                ││
│  │ Step2 文本提取                                                ││
│  │ Step3.1 向量表检测   Step3.2 扫描表检测                        ││
│  │ Step4.1.1 LLM表格描述  Step4.2.1 LLM非表描述                  ││
│  │ Step5 合并 + GATE1 (≥99% coverage)                            ││
│  │      ✅ PASS → 标记merge_done + 删中间图                       ││
│  │      ❌ FAIL → 保留全图 + 中断（修复后 --resume）               ││
│  │ Step6 文本分段                                                 ││
│  │ Step7 向量嵌入                                                 ││
│  │ Step8.1 ES入库 + GATE2 (≥99.5% accuracy)                      ││
│  │ Step8.2 Milvus入库                                            ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  每个步骤支持: checkpoint/resume + 3次瞬时重试(30s/60s/120s)     │
└─────────────────────────────────────────────────────────────────┘
```

### GATE 质量门控

| 门控 | 位置 | 阈值 | 检查项 | 失败行为 |
|------|------|------|--------|----------|
| **GATE1** | step5 之后 | ≥99% | table_describe_done / not_table_describe_done / text.json>1KB | 保留图片，管道中断 |
| **GATE2** | step8_1 之后 | ≥99.5% | ES 索引文档数 / 字段完整性 | 管道中断 |

### 元数据治理三层体系

```
Layer 1 — reconcile_meta.py (ES安克卫士)
  ES 中有数据的文档永不回退上游标志
  
Layer 2 — sync_text_metadata.py (merge前的最后一公里)
  step5 执行前同步 text.json→meta.json，消除过时标志
  
Layer 3 — rebuild_manifest.py (双向交叉校验)
  manifest→ES/Milvus 正向 + ES/Milvus→manifest 反向
```

### 标志位状态推导规则

| 你要知道的 | 等价条件 | 复杂度 |
|-----------|---------|--------|
| step1-5 全部完成 | meta.json 中 merge_done=true **且** GATE1=pass | O(1) |
| 全链路成功(ES入库) | elasticsearch_database_done=true **且** GATE2=pass | O(1) |
| 全链路成功(Milvus) | vector_database_done=true | O(1) |
| step5 之前全部完成 | GATE1=pass（等价推导） | O(1) |

---

## 📂 数据产出结构

```
announcement_document_processing_local/
├── processed_files_local.json          # 主 manifest
├── {fund_code}/                        # 如 180101/, 508066/
│   └── {pdf_name}/                     # 180101-xxx-2021-05-20
│       ├── meta.json                   # 处理标志位（权威真相）
│       ├── text.json                   # 提取/合并后的全文（含 metadata）
│       ├── table_describe.json         # LLM 表格描述（每页/跨页一行）
│       ├── not_table_describe.json     # LLM 非表页描述
│       ├── temp_pdf_images/            # [GATE1后删除] 中间产物 — PDF 渲染 PNG
│       └── table_image/                # [GATE1后删除] 中间产物 — 检测到的表格裁图
│
└── manifest_backups/                   # manifest 时间戳快照

# 最终检索数据（外部系统）
ES:  http://localhost:9200/reits_docs       ← 165 万 chunks（全文检索）
Milvus: http://localhost:19530/reits_embeddings ← 110 万 vectors（语义检索）
MySQL: announcement.page_data                ← 逐页结构化数据
```

### 关键标志位（meta.json）

```json
{
  "text_extracted": true,           // step1+step2 完成
  "table_detection_vector_done": true,  // step3.1
  "table_detection_scan_done": true,    // step3.2
  "table_describe_done": true,      // step4.1.1
  "not_table_describe_done": true,  // step4.2.1
  "merge_done": true,               // step5 + GATE1 pass
  "text_segmentation": true,        // step6
  "embedding_done": true,           // step7
  "elasticsearch_database_done": true,  // step8.1 + GATE2 pass
  "vector_database_done": true      // step8.2
}
```

> ⚠️ **信任链方向**: 物理数据(ES/Milvus/磁盘文件) > meta.json。meta.json 是**线索**不是**真相**，可能假阳性或假阴性。判断文档状态时以 ES/Milvus 实际入库数据为准。

---

## 🚀 使用方法

### 环境要求
- Python 3.11+ (conda env: `deepseek-ocr`)
- MySQL 5.7+
- Elasticsearch 7.x+
- Milvus 2.x+
- Docker (ES + Milvus)
- 足够的磁盘空间（完整处理 1,425 份 PDF 需 ~300GB）

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/pwu0125/RAG-REITsTextFlow.git
cd RAG-REITsTextFlow

# 2. 创建 conda 环境
conda create -n deepseek-ocr python=3.11
conda activate deepseek-ocr

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置（复制模板后编辑）
cp db_config.example.py db_config.py   # 数据库密码
# 编辑 model_config.py                 # API 密钥

# 5. 创建数据库索引
python create_elasticsearch_index.py
python create_vector_database.py

# 6. 下载 TableTransformer 模型
# (下载到 table-transformer-detection/ 目录，参见 SETUP.md)
```

### 运行管道

```bash
# 全新运行一个批次
python pipeline_controller.py B2m

# 从崩溃恢复
python pipeline_controller.py B2m --resume

# 运行单步
python run_step.py B2m step5

# 运行 GATE 检查
python gate1_coverage_check.py B2m
python gate2_accuracy_check.py B2m

# 诊断
python diagnose_pipeline.py           # 全管道状态
python scan_progress.py               # 批次进度
```

### 添加新 PDF

```bash
# 1. 将 PDF 放入 announcement_document_raw/
cp your_file.pdf announcement_document_raw/

# 2. 更新 BATCH_CONFIG.json (如果使用新 fund_code)
# 3. 运行管道
python pipeline_controller.py <batch_name>
```

---

## 🐛 已知问题 / Pitfalls

| 问题 | 影响 | 修复方案 |
|------|------|----------|
| CC(claude -p) MCP 进程组清理杀子进程 | 管道步骤通过 CC 运行会被 kill | 只能用 conda python 直接运行或 cron job |
| macOS TesseractNotFoundError 继承 OSError | step3_2 误判 Tesseract 未安装 | ✅ 已修复 |
| step5 删图后无法追溯中间产物 | 管道断点后无法定位根因 | ✅ GATE1 通过才删图 |
| meta.json 假阳性/假阴性 | 盲目信任标志位导致误判 | ✅ ES/Milvus 锚定 + reconcile_meta |
| 零图 PDF 不标记 not_table_describe_done | 纯矢量文档卡在 step4→step5 | ✅ 已修复 |

---

## 📄 License

MIT License. 原始版权归属 [adennng/RAG-REITsTextFlow](https://github.com/adennng/RAG-REITsTextFlow)。修改部分 © 2026 pwu0125。
