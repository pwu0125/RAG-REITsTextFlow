# 代码陷阱记录：text_extracted 标志不一致导致文档入库缺失

## 症状

GATE2 发现批次中少量文档（1-6份）缺失于 ES，但 GATE1 通过（100%覆盖）。审计发现：
- `text.json` 存在于磁盘（>1KB，有实质内容）
- `meta.json` 中 `text_extracted = False`
- 下游步骤（step3→8）全部跳过

## 根因

双重 Bug 交叉作用：

### Bug 1: `run_step.py` manifest 恢复覆盖 (行 116)

```python
# run_step.py:85-116
shutil.copy2(MANIFEST_PATH, BACKUP_PATH)          # 备份全量 manifest
...
save_json(MANIFEST_PATH, {"files": filtered})      # 写入过滤版
...
result = subprocess.run(cmd)                        # 步骤运行（更新过滤版）
...
shutil.move(str(BACKUP_PATH), str(MANIFEST_PATH))  # ❌ 恢复备份 → 过滤版的更新全丢
```

step2 更新了过滤版 manifest 的 `text_extracted=True`，但 `run_step.py` 恢复全量 manifest 时覆盖了这些更新。

### Bug 2: step2 静默异常吞没 (行 94-95)

```python
# step2_extract:85-95
def _update_meta_json_status(fund_code, pdf_folder_name, updates):
    meta_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "meta.json")
    if not os.path.exists(meta_path):
        return
    try:
        meta = safe_json_load(meta_path)
        if isinstance(meta, dict):
            meta.update(updates)
            safe_json_dump(meta, meta_path)
    except Exception:
        return  # ❌ 任何写入异常静默丢弃，无日志
```

如果 JSON 解码失败、写入权限问题、或 `safe_json_dump` 异常，标志更新丢失且无任何告警。

### 叠加效应

```
Bug 2: step2 写入 meta.json 静默失败 → meta.json text_extracted=False
         +
Bug 1: run_step.py 恢复覆盖 manifest → manifest text_extracted=False
         ↓
step3 检查 manifest → 看到 False → 跳过文档 → 永久缺失
```

## 受影响批次

B1c(6), B2b(2), B2c(3), B2e(5), B2f(2), B2i(1) = 19份招募说明书

## 修复

### Fix 1: run_step.py — 恢复后从 meta.json 同步回 manifest

```python
# 在 restore 之后增加 reconcile
shutil.move(str(BACKUP_PATH), str(MANIFEST_PATH))

# [NEW] 从磁盘 meta.json 同步标志回 manifest
manifest = load_json(MANIFEST_PATH)
reconciled = 0
for file_name, info in manifest["files"].items():
    fund_code = info.get("fund_code", "")
    pdf_folder = os.path.splitext(file_name)[0]
    meta_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder, "meta.json")
    if os.path.exists(meta_path):
        meta = safe_json_load(meta_path)
        for flag in ["text_extracted", "table_detection_vector_done", 
                     "table_detection_scan_done", "table_describe_done"]:
            if meta.get(flag) and not info.get(flag):
                info[flag] = True
                reconciled += 1
if reconciled:
    save_json(MANIFEST_PATH, manifest)
    print(f"Reconciled {reconciled} flags from meta.json → manifest")
```

### Fix 2: step2 — 错误降级为 WARNING 而非静默

```python
except Exception as e:
    logging.warning(f"Failed to update meta.json {meta_path}: {e}")
    return  # 维持原 fallthrough，但记录日志
```

### Fix 3: 批次闭环一致性检查

GATE2 后自动对比 manifest 预期文档数 vs ES 实际覆盖数。不等则阻塞"完成"标记并输出差异清单。

## 预防

- 每个批次完成后运行 `reconcile_meta.py VERIFY_FULL=True` 做全量校验
- 新增 `validate_manifest_consistency.py` 作为 CI check

## FlagSync A+B 硬结合方案（已纳入待执行计划）

### 方案 A：合并为 sync_all.py

```python
# sync_all.py — disk → meta.json → manifest 单命令同步
# 替代手动 rebuild_manifest.py + reconcile_meta.py

def sync_all():
    # 1. reconcile: disk状态 → meta.json
    #    text.json>1KB → text_extracted=True
    #    table_image 非空 → detection flags=True
    #    table_describe.json → describe flag=True
    reconcile_meta(verify_full=False)
    
    # 2. rebuild: meta.json → manifest
    rebuild_manifest()
    
    print("✅ disk → meta.json → manifest 三层同步完成")
```

**效果**: 消除"修了 meta 忘了 manifest"的人为失误，单命令完成全部同步。

### 方案 B：run_step.py 内嵌同步（核心）

```python
# run_step.py:116 — 替换现有 restore 逻辑

# 原代码：
# shutil.move(str(BACKUP_PATH), str(MANIFEST_PATH))

# 新代码：
shutil.move(str(BACKUP_PATH), str(MANIFEST_PATH))

# [FlagSync B] 从 meta.json 同步标志回 manifest
manifest = load_json(MANIFEST_PATH)
reconciled = sync_flags_from_meta(manifest)
if reconciled:
    save_json(MANIFEST_PATH, manifest)
    print(f"[FlagSync] {reconciled} flags synced from meta.json → manifest")
```

**效果**: 消除 run_step.py 自动覆盖 bug——每次 restore 后自动修正不一致的标志。

---

## Bug 3: step5 清理 temp_pdf_images 引发 step3_2 崩溃（2026-06-14 发现）

### 症状

```
B2_splmnt step3_2 处理 180301 招募说明书时:
  FileNotFoundError: .../temp_pdf_images 目录不存在
```

step3_1 报告"成功 11 个"，但该文档 `temp_pdf_images/` 缺失 → step3_2 第 924 行 `os.listdir(input_img_dir)` 💥。

### 根因

**跨步骤资源生命周期断裂**：

```
之前 B2 批次：
  step3_2 完成扫描 → step5 GATE1 通过 
  → step5:699: shutil.rmtree(temp_pdf_images/)  ← 清理磁盘

FlagSync bug（Bug 1+2）→ 标志重置：
  table_detection_scan_done → False

B2_splmnt 重跑：
  step3_1: 重新矢量检测 → 写入 table_image/ ✅
  step3_2: 期待 temp_pdf_images/ → 💥 已被 step5 删除
```

**核心矛盾**：step3_2 依赖 `temp_pdf_images` 作为输入资源，但 step5 将其作为"已消费"清理标记。当标志被重置后 step3_2 重跑，资源已不存在。

### 受影响文档

180301 红土创新盐田港扩募招募说明书（更新）-2023-12-29：
- `table_image/` 有 63 张表图（之前批次产物）
- `table_describe.json` 存在（LLM 描述完成）
- `text_segmentation_embedding.json` 33MB（已入库）
- 但 `temp_pdf_images/` 缺失（被 step5 清理）

### 修复

详见 [Fix 4: step3_2 防御性资源检查]。

---

## FlagSync 三维修补方案（更新）

| 维度 | 修复什么 | 涉及文件 | 状态 |
|:---|:---|:---|:---:|
| A: sync_all.py | meta.json ↔ manifest 标志不同步 | sync_all.py（合并 rebuild + reconcile） | 📋 待执行 |
| B: run_step 内嵌 + step2 降级 | 标志被覆盖 + 异常静默吞没 | run_step.py:116 + step2_extract.py:94-95 | 📋 待执行 |
| C: 步骤间资源依赖 | step5 清理 → step3_2 缺资源 | step3_2_table_detection_scan_multifile.py:918-924 | 📋 待执行 |

### 方案 C：step3_2 防御性资源检查（最小改动）

```python
# 文件: step3_2_table_detection_scan_multifile.py
# 位置: 第 918 行附近，替换现有 input_img_dir/os.listdir 逻辑

input_img_dir = os.path.join(pdf_folder_dir, "temp_pdf_images")
table_img_dir = os.path.join(pdf_folder_dir, "table_image")

# [FlagSync C] 防御性检查：temp_pdf_images 可能被 step5 清理
if not os.path.exists(input_img_dir):
    if os.path.exists(table_img_dir) and os.listdir(table_img_dir):
        # 资源已清理但下游产物完备 → 步骤等效已完成
        logging.warning(
            f"temp_pdf_images missing but table_image exists "
            f"({len(os.listdir(table_img_dir))} images). "
            f"Marking scan_done for: {pdf_info['file_path']}"
        )
        update_database_status(pdf_info)
        return True, None
    else:
        error_msg = (
            f"temp_pdf_images missing and no table_image found: "
            f"{pdf_info['file_path']}"
        )
        logging.error(error_msg)
        return False, error_msg

# 原逻辑：只有目录存在时才列出文件
img_files = sorted(
    [f for f in os.listdir(input_img_dir) 
     if f.startswith('page_') and f.endswith('.png')],
    key=lambda x: int(re.search(r'page_(\d+)', x).group(1))
)
```

**效果**：
- `temp_pdf_images` 不存在 + `table_image/` 有内容 → 直接标记 `scan_done` 跳过（不报错）
- `temp_pdf_images` 不存在 + `table_image/` 空 → 报错（真正需要人工介入）
- `temp_pdf_images` 存在但空 → 保持现有逻辑（update_database_status + return True）

### 方案 C-Alt：长期架构优化（可选）

替代 step5 的即时清理 → 批次完成后统一清理临时资源：

```python
# 在 batch_runner.py 批次完全闭环后
def cleanup_batch_resources(batch_name):
    for doc in get_batch_docs(batch_name):
        temp_dir = os.path.join(doc['output_dir'], 'temp_pdf_images')
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
```

**权衡**: 磁盘占用增加（每文档 100-500MB），但消除资源竞态。当前采纳方案 C（防御性检查）而非 C-Alt。

---

## 叠加效应（全量）

```
Bug 2: step2 写入 meta.json 静默失败 → meta.json text_extracted=False
         +
Bug 1: run_step.py 恢复覆盖 manifest → manifest text_extracted=False
         ↓
step3 检查 manifest → 看到 False → 全部跳过 → 19份文档缺失
         ↓ (B2_splmnt 重跑)
Bug 3: step5 已清理 temp_pdf_images → step3_2 FileNotFoundError → 180301 报错
```

三层 Bug 交叉作用：同一批次反复重跑时，第一轮产生的资源清理与第二轮标志重置形成死锁。
