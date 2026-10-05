# 陷阱94：全扫描文档的架构缺口——step2 只渲染不提取，text.json 0 页（2026-10-05 发现）

> 承接陷阱91。当晚大回填攻坚时发现：真死锁的 940 份里绝大部分不是「中断事故」，而是**架构级缺口**。

## 现象
146+ 份评估报告（如 180101 蛇口 2021 年度评估报告，36 页全扫描）反复进 catchup 队列，pipeline 每轮都跑但永远 ES=False，attempts 打满后 abandoned。手动单跑 step2 复现：101 页全部「命中 pdf-inspector OCR 页, 直接渲染(跳过矢量提取)」→ text.json 0 页 0 字 → 陷阱91防线拒绝置位 → 死循环。

## 根因（架构事实，非 bug）
- step2 的设计是**矢量提取器**：只提取 PDF 原生文本层的页；扫描页只负责渲染成 PNG（供后续 step4 图像描述用）。
- **全扫描文档**（pdf_type=scanned，pages_needing_ocr ≥ 总页数）没有原生文本层 → step2 产出 0 页 text.json → 全链从 step6 起无正文可分段/嵌入。
- 现有 ES 库里的 71 份「全扫描已完成」是历史遗留半成品：靠 step4 表格描述勉强撑起 chunk（抽检 508066 华泰江苏交控：228 页扫描件 text.json 仅 1 页），**正文文本实际缺失**——质量黑洞，检索时表现为"只能搜到表格描述，搜不到正文"。
- OCR 路由器（ocr_router.py，local DeepSeek-OCR / api DashScope）已存在但**从未接入 step2 的扫描页路径**——这是缺口，不是没有工具。

## 当晚处置（保守，不扩伤）
1. 只读鉴定：898 份「text_extracted=True 且 text.json 0 页」僵尸（对应陷阱91的 940 真死锁存量）。
2. 曾重置 meta+删 0 页空壳 text.json 试跑 → 发现全扫描死循环 → **全量还原**（备份 manifest_backups/zombie_meta_reset_20261005/），913 份回到陷阱91原状。
3. catchup 加 `_is_full_scan` 过滤（陷阱94防线）：全扫描文档不进队列，挂起待架构决策。队列从 150 → 3 份（mixed 文档正常跑）。
4. mixed 文档（矢量页 + 扫描页混合）不受影响——矢量页撑起 text.json，扫描页走 step4。

## 根治方向（待用户批准后实施）
- 方案A（推荐）：step2 的 direct_render_pages 分支后接 ocr_router——渲染完直接 OCR 提取文本写入 text.json（ocr_router 已有 local/api 双通道）。涉及 918 份全扫描存量 + 每周新扫描件。
- 方案B：全扫描文档改走独立 OCR 管道（batch 1 份 1 份过 DeepSeek-OCR MPS），结果回填 text.json。
- 修复 71 份历史半成品（text.json 仅 1 页的）需同步纳入。
- **修复前禁令：这 918 份不可手动置标志入库**（正文缺失的半成品入库=污染检索质量）。

## 铁律
- 「渲染 ≠ 提取」：图片渲染出来只是原料，没有 OCR 调用就没有文本。判断提取完成必须看 text.json 内容物，不是图片目录。
- 架构缺口的存量不能靠重试解决：重试 100 次也不会凭空长出文本。识别出架构级失败模式后立即挂起，禁止烧 API 硬闯。
