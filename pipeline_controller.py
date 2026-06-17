#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pipeline_controller.py — RunConductor: 端到端管道编排器

用法:
  python pipeline_controller.py <BATCH>                   # 批次模式（BATCH_CONFIG.json）
  python pipeline_controller.py --codes 508066,508028      # 指定 code 列表模式
  python pipeline_controller.py --codes 508066 --resume    # 续跑
  python pipeline_controller.py --doc 2022-05-06           # 单文档匹配模式

模式对比:
  批次模式: BATCH_CONFIG.json 定义的 batch → step5 带 GATE1 → step8.1 后 GATE2
  --codes:  逗号分隔的 code 列表 → step5 无 GATE1（控制器自动标记 merge_done）→ GATE2
  --doc:    从 manifest 中匹配文件名子串 → 同上非批模式
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONDA_PYTHON = "/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python"
STATE_FILE = os.path.join(SCRIPT_DIR, "pipeline_state.json")
METAGUARD_FILE = os.path.join(SCRIPT_DIR, ".metaguard_status.json")
BATCH_CONFIG_PATH = os.path.join(SCRIPT_DIR, "BATCH_CONFIG.json")
MANIFEST_PATH = os.path.join(
    SCRIPT_DIR, "announcement_document_processing_local", "processed_files_local.json"
)

# 步骤定义: (script_path, meta_flag, description)
STEP_SEQUENCE = [
    ("step1_process_pdfs.py", "text_extracted", "Step1 PDF→PNG"),
    ("step2_extract_text_onlyvactor_multi_process.py", "text_extracted", "Step2 文本提取"),
    ("step3_1_detection_vactor_multi_process.py", "table_detection_vector_done", "Step3.1 向量表检测"),
    ("step3_2_table_detection_scan_multifile.py", "table_detection_scan_done", "Step3.2 扫描表检测"),
    ("step4_1_1_describe_table_images_multi_thread.py", "table_describe_done", "Step4.1.1 表描述"),
    ("step4_2_1_describe_not_table_images_llm.py", "not_table_describe_done", "Step4.2.1 非表图像描述"),
    ("step5_merge_table_into_text.py", "merge_done", "Step5 合并+GATE1"),
    ("step6_text_segmentation.py", "text_segmentation", "Step6 文本分段"),
    ("step7_text_embedding.py", "embedding_done", "Step7 向量嵌入"),
    ("step8_1_ingest_elasticsearch_data.py", "elasticsearch_database_done", "Step8.1 ES入库+GATE2"),
    ("step8_2_ingest_vector_database.py", "vector_database_done", "Step8.2 Milvus入库"),
]

RETRY_MAX = 3
RETRY_BACKOFF = [30, 60, 120]
DISK_MIN_GB = 20


def safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state():
    return safe_read_json(STATE_FILE) or {}


def save_state(state: dict):
    state["updated_at"] = datetime.datetime.now().isoformat()
    safe_write_json(STATE_FILE, state)


def run_preflight(batch_name: str, batch_codes: list):
    """Pre-flight 检查，返回 True 表示通过。"""
    print("\n" + "=" * 50)
    print(f"  Pre-flight: {batch_name}")
    print("=" * 50)

    # ── MetaGuard ──
    mg = safe_read_json(METAGUARD_FILE)
    if mg and mg.get("status") == "pass" and mg.get("batch") == batch_name:
        print(f"  MetaGuard ✅，跳过重复对账 (上次: {mg.get('checked_at', '?')})")
    else:
        if mg and mg.get("status") == "needs_review":
            print(f"  MetaGuard ⚠️ needs_review，重新对账...")
        else:
            print(f"  MetaGuard: 未找到/过期，执行 reconcile...")
        rc = subprocess.run(
            [CONDA_PYTHON, os.path.join(SCRIPT_DIR, "reconcile_meta.py"), batch_name],
            cwd=SCRIPT_DIR,
        )
        if rc.returncode != 0:
            print(f"  ❌ reconcile_meta.py 返回 {rc.returncode}")
            return False
        # 重读标记
        mg = safe_read_json(METAGUARD_FILE)
        if mg and mg.get("status") == "needs_review":
            print(f"  ⚠️  MetaGuard 标记为 needs_review，继续但请检查上述重置项")

    # ── Docker 连通性 ──
    print(f"  Docker 连通性检查...")
    es_ok = _check_port("localhost", 9200)
    mv_ok = _check_port("localhost", 19530)
    if not es_ok:
        print(f"  ⚠️  ES :9200 不可达")
    if not mv_ok:
        print(f"  ⚠️  Milvus :19530 不可达")
    if es_ok and mv_ok:
        print(f"  ✅ ES + Milvus 均可达")
    else:
        print(f"  ⚠️  部分服务不可达，步骤 8.1/8.2 可能失败")

    # ── 磁盘 ──
    usage = shutil.disk_usage(SCRIPT_DIR)
    free_gb = usage.free / (1024 ** 3)
    if free_gb < DISK_MIN_GB:
        print(f"  ❌ 磁盘可用 {free_gb:.1f}GB < {DISK_MIN_GB}GB 阈值")
        return False
    print(f"  ✅ 磁盘可用: {free_gb:.1f}GB")

    print("=" * 50 + "\n")
    return True


def _check_port(host: str, port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def _collect_doc_statuses(batch_codes: list) -> list:
    """扫描 batch 内所有文档的 meta.json，返回每个文档的步骤标志。"""
    manifest = safe_read_json(MANIFEST_PATH) or {}
    files_map = manifest.get("files", {}) or {}
    output_dir = os.path.join(SCRIPT_DIR, "announcement_document_processing_local")
    results = []

    for file_name, info in files_map.items():
        fund_code = (info or {}).get("fund_code", "")
        if fund_code not in batch_codes:
            continue
        doc_dir = os.path.join(output_dir, fund_code, os.path.splitext(file_name)[0])
        meta_path = os.path.join(doc_dir, "meta.json")
        meta = safe_read_json(meta_path) or {}

        # 从 text.json metadata 也读取（兼容旧逻辑，meta.json 为最终权威）
        text_path = os.path.join(doc_dir, "text.json")
        text_json = safe_read_json(text_path) or {}
        text_meta = text_json.get("metadata", {}) or {}

        merged = {}
        merged.update(text_meta)
        merged.update(meta)  # meta 最后 → 最终权威

        results.append({
            "file_name": file_name,
            "fund_code": fund_code,
            "doc_dir": doc_dir,
            "status": merged,
        })
    return results


def _step_completed(doc_statuses: list, flag: str) -> bool:
    """所有非无关文档该步骤均已完成。"""
    for d in doc_statuses:
        if d["status"].get("doc_type_1") == "无关":
            continue
        if not d["status"].get(flag):
            return False
    return True


def _find_start_index(doc_statuses: list) -> int:
    """找到第一个未完成的步骤索引。"""
    for i, (_, flag, _) in enumerate(STEP_SEQUENCE):
        if not _step_completed(doc_statuses, flag):
            return i
    return len(STEP_SEQUENCE)


def _run_step(script: str, flag: str, desc: str, batch_name: str, batch_codes: list, current: int, total: int, gate_mode: bool = True) -> bool:
    """执行单个步骤，支持瞬时重试。返回 True 表示成功。

    gate_mode=False 时，step5 不加 --gate 参数（非批模式合并后由控制器标记 merge_done）。
    """
    script_path = os.path.join(SCRIPT_DIR, script)
    cmd = [CONDA_PYTHON, script_path]

    # step5 GATE1：只有 batch 模式才传 --gate
    if script == "step5_merge_table_into_text.py" and gate_mode:
        cmd.append("--gate")
        cmd.append(batch_name)

    # 通过 CLI --batch-codes 参数传递给 step 脚本
    if batch_codes:
        cmd.extend(["--batch-codes", ",".join(batch_codes)])

    for attempt in range(1, RETRY_MAX + 1):
        print(f"\n  [{desc}] 执行中 (attempt {attempt}/{RETRY_MAX})...")
        print(f"    {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, cwd=SCRIPT_DIR, timeout=3600)
            if result.returncode == 0:
                print(f"  [{desc}] ✅ 完成")
                return True
            else:
                print(f"  [{desc}] ❌ exit={result.returncode}")
        except subprocess.TimeoutExpired:
            print(f"  [{desc}] ⏱️  超时")
        except Exception as e:
            print(f"  [{desc}] ❌ 异常: {e}")

        if attempt < RETRY_MAX:
            wait = RETRY_BACKOFF[attempt - 1]
            print(f"  [{desc}] 等待 {wait}s 后重试...")
            time.sleep(wait)

    print(f"  [{desc}] ❌ {RETRY_MAX} 次重试后仍失败，永久错误退出")
    return False


def _auto_mark_merge_done(batch_codes: list):
    """非批模式下，step5 合并后由控制器直接标记 merge_done。"""
    from common_utils import safe_json_dump, safe_json_load
    output_dir = os.path.join(SCRIPT_DIR, "announcement_document_processing_local")
    manifest = safe_read_json(MANIFEST_PATH) or {}
    files_map = manifest.get("files", {}) or {}
    marked = 0
    for file_name, info in files_map.items():
        fund_code = (info or {}).get("fund_code", "")
        if fund_code not in batch_codes:
            continue
        doc_dir = os.path.join(output_dir, fund_code, os.path.splitext(file_name)[0])
        meta_path = os.path.join(doc_dir, "meta.json")
        meta = safe_read_json(meta_path)
        if not isinstance(meta, dict):
            continue
        # 前置条件检查：table_describe_done 和 not_table_describe_done 必须 True
        if meta.get("table_describe_done") is not True or meta.get("not_table_describe_done") is not True:
            continue
        meta["merge_done"] = True
        safe_json_dump(meta, meta_path)
        # 清理 temp_pdf_images/
        temp_dir = os.path.join(doc_dir, "temp_pdf_images")
        if os.path.isdir(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
        marked += 1
        print(f"  ✅ merge_done + 清图: {file_name}")
    if marked:
        print(f"  非批模式：已标记 {marked} 个文档 merge_done 并清理中间图片")


def run_pipeline_with_codes(batch_codes: list, label: str = "adhoc", resume: bool = False):
    """非批模式：直接指定 code 列表跑全流程。无 GATE1，step5 合并后自动标记 merge_done。"""
    from common_utils import safe_json_dump, safe_json_load

    batch_name = f"{label}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"RunConductor (adhoc): {label}  codes={batch_codes}\n")

    # ── Pre-flight 检查 ──
    if not run_preflight(batch_name, batch_codes):
        print("❌ Pre-flight 失败，退出")
        sys.exit(1)

    # ── 扫描文档状态 ──
    doc_statuses = _collect_doc_statuses(batch_codes)
    total_docs = len(doc_statuses)
    print(f"目标文档数: {total_docs}\n")

    start_idx = _find_start_index(doc_statuses)

    if start_idx > 0:
        skipped = [STEP_SEQUENCE[i][2] for i in range(start_idx)]
        print(f"已完成步骤: {skipped}")
        print(f"从步骤 {start_idx} 开始 ({STEP_SEQUENCE[start_idx][2]})\n")

    # ── 逐步执行 ──
    for i in range(start_idx, len(STEP_SEQUENCE)):
        script, flag, desc = STEP_SEQUENCE[i]

        if _step_completed(doc_statuses, flag):
            print(f"[{desc}] ✅ 已完成，跳过")
            continue

        print(f"\n{'=' * 50}")
        print(f"  步骤 {i + 1}/{len(STEP_SEQUENCE)}: {desc}")
        print(f"{'=' * 50}")

        success = _run_step(script, flag, desc, batch_name, batch_codes, i, len(STEP_SEQUENCE), gate_mode=False)
        if not success:
            print(f"\n❌ [{desc}] 永久失败，管道中断")
            sys.exit(1)

        # step5 后：自动标记 merge_done（无 GATE1 模式）
        if script == "step5_merge_table_into_text.py":
            print(f"\n  [非批模式] step5 完成，自动标记 merge_done + 清图...")
            _auto_mark_merge_done(batch_codes)

        # step8_1 后 run GATE2
        if script == "step8_1_ingest_elasticsearch_data.py":
            gate2_ok = _run_gate2(batch_name)
            if not gate2_ok:
                print(f"\n❌ GATE2 精度检查失败")
                sys.exit(1)

        doc_statuses = _collect_doc_statuses(batch_codes)

    print(f"\n{'=' * 60}")
    print(f"  RunConductor: {label} 全部完成 ✅")
    print(f"{'=' * 60}")
    print(f"  步骤总数: {len(STEP_SEQUENCE)}")
    print(f"  文档总数: {total_docs}")
    print(f"{'=' * 60}\n")
    """运行 GATE2 精度检查。"""
    gate2_path = os.path.join(SCRIPT_DIR, "gate2_accuracy_check.py")
    if not os.path.exists(gate2_path):
        print(f"  ⚠️  gate2_accuracy_check.py 不存在，跳过 GATE2")
        return True
    print(f"\n  [GATE2] 精度检查...")
    try:
        result = subprocess.run(
            [CONDA_PYTHON, gate2_path, batch_name],
            cwd=SCRIPT_DIR,
            timeout=600,
        )
        if result.returncode == 0:
            print(f"  [GATE2] ✅ 通过")
            return True
        else:
            print(f"  [GATE2] ❌ 失败 (exit={result.returncode})")
            return False
    except Exception as e:
        print(f"  [GATE2] ❌ 异常: {e}")
        return False


def run_batch(batch_name: str, resume: bool = False):
    """主入口：运行完整批处理管道。"""
    # ── 加载配置 ──
    config = safe_read_json(BATCH_CONFIG_PATH)
    if not config or batch_name not in config:
        print(f"批次 '{batch_name}' 不在 BATCH_CONFIG.json 中")
        sys.exit(1)
    batch_entry = config[batch_name]
    batch_codes = batch_entry.get("codes", [])
    print(f"RunConductor: {batch_name}  codes={batch_codes}" + "\n")

    # ── 状态恢复 ──
    state = load_state()
    if state and state.get("batch") == batch_name and not state.get("done"):
        if not resume:
            print(f"发现未完成的 pipeline_state.json (updated={state.get('updated_at', '?')})")
            print(f"可使用 --resume 从中断处继续\n")
        else:
            print(f"🔄 从 pipeline_state.json 恢复...")
    elif resume and state.get("done"):
        print(f"✅ pipeline_state.json 标记为已完成，重新运行所有步骤。")

    if resume and not state:
        print(f"--resume 指定但无 state 文件，视为全新运行")

    # ── Pre-flight 检查 ──
    if resume and state and state.get("preflight_ok"):
        print("Pre-flight 已通过（从 state 恢复），跳过")
    else:
        if not run_preflight(batch_name, batch_codes):
            print("❌ Pre-flight 失败，退出")
            sys.exit(1)

    # ── 扫描文档状态，确定起点 ──
    doc_statuses = _collect_doc_statuses(batch_codes)
    total_docs = len(doc_statuses)
    print(f"批次文档数: {total_docs}\n")

    start_idx = _find_start_index(doc_statuses)

    if resume and state:
        saved_step = state.get("current_step_idx", 0)
        start_idx = min(start_idx, saved_step)
        print(f"恢复起点: 步骤 {start_idx} ({STEP_SEQUENCE[start_idx][2] if start_idx < len(STEP_SEQUENCE) else '完成'})\n")
    elif start_idx > 0:
        skipped = [STEP_SEQUENCE[i][2] for i in range(start_idx)]
        print(f"已完成步骤: {skipped}")
        print(f"从步骤 {start_idx} 开始 ({STEP_SEQUENCE[start_idx][2]})\n")

    # ── 初始化/更新状态 ──
    state = {
        "batch": batch_name,
        "codes": batch_codes,
        "started_at": state.get("started_at") or datetime.datetime.now().isoformat(),
        "preflight_ok": True,
        "total_docs": total_docs,
        "current_step_idx": start_idx,
        "total_steps": len(STEP_SEQUENCE),
        "done": False,
    }

    # ── 逐步执行 ──
    for i in range(start_idx, len(STEP_SEQUENCE)):
        script, flag, desc = STEP_SEQUENCE[i]

        # 启动前再检查一次（避免重复运行）
        if _step_completed(doc_statuses, flag):
            print(f"[{desc}] ✅ 已完成，跳过")
            state["current_step_idx"] = i + 1
            save_state(state)
            continue

        # 进度上报
        print(f"\n{'=' * 50}")
        print(f"  步骤 {i + 1}/{len(STEP_SEQUENCE)}: {desc}")
        print(f"{'=' * 50}")
        state["current_step_idx"] = i
        save_state(state)

        # 执行
        success = _run_step(script, flag, desc, batch_name, batch_codes, i, len(STEP_SEQUENCE))
        if not success:
            print(f"\n❌ [{desc}] 永久失败，管道中断")
            state["failed_step"] = desc
            state["failed_at"] = datetime.datetime.now().isoformat()
            save_state(state)
            print(f"保存状态到 {STATE_FILE}。修复后执行:")
            print(f"  python pipeline_controller.py {batch_name} --resume")
            sys.exit(1)

        state["current_step_idx"] = i + 1
        save_state(state)

        # step5 后 run GATE1（嵌入在 step5 中，step5 自带 --gate）
        if script == "step5_merge_table_into_text.py":
            print(f"\n  [GATE1] 覆盖率检查（由 step5 内部完成）")

        # step8_1 后 run GATE2
        if script == "step8_1_ingest_elasticsearch_data.py":
            gate2_ok = _run_gate2(batch_name)
            if not gate2_ok:
                print(f"\n❌ GATE2 精度检查失败")
                state["failed_step"] = "GATE2"
                state["failed_at"] = datetime.datetime.now().isoformat()
                save_state(state)
                sys.exit(1)

        # 刷新文档状态（用于下一轮检查）
        doc_statuses = _collect_doc_statuses(batch_codes)

    # ── 完成 ──
    state["done"] = True
    state["completed_at"] = datetime.datetime.now().isoformat()
    save_state(state)

    print(f"\n{'=' * 60}")
    print(f"  RunConductor: {batch_name} 全部完成 ✅")
    print(f"{'=' * 60}")
    print(f"  步骤总数: {len(STEP_SEQUENCE)}")
    print(f"  文档总数: {total_docs}")
    print(f"  开始: {state.get('started_at', '?')}")
    print(f"  完成: {state.get('completed_at', '?')}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  python pipeline_controller.py <BATCH>              # 批次模式")
        print("  python pipeline_controller.py --codes 508066,508028  # 指定 code 列表")
        print("  python pipeline_controller.py --codes 508066 --resume  # 续跑")
        print(f"\n可用 batch: {list(safe_read_json(BATCH_CONFIG_PATH).keys()) if safe_read_json(BATCH_CONFIG_PATH) else 'N/A'}")
        sys.exit(1)

    do_resume = "--resume" in sys.argv

    if sys.argv[1] == "--codes":
        # 指定 code 列表模式
        if len(sys.argv) < 3:
            print("用法: python pipeline_controller.py --codes 508066,508028,180601")
            sys.exit(1)
        codes = [c.strip() for c in sys.argv[2].split(",") if c.strip()]
        label = f"codes_{','.join(codes)}"
        run_pipeline_with_codes(codes, label=label, resume=do_resume)

    elif sys.argv[1] == "--doc":
        # 单文档模式：从 manifest 中匹配包含指定子串的文档
        if len(sys.argv) < 3:
            print("用法: python pipeline_controller.py --doc <pattern>")
            print("示例: python pipeline_controller.py --doc 2022-05-06")
            sys.exit(1)
        doc_pattern = sys.argv[2]
        manifest = safe_read_json(MANIFEST_PATH) or {}
        files_map = manifest.get("files", {}) or {}
        matching_codes = set()
        for fname, info in files_map.items():
            if doc_pattern in fname:
                code = (info or {}).get("fund_code", "")
                if code:
                    matching_codes.add(code)
        if not matching_codes:
            print(f"❌ 未找到匹配 '{doc_pattern}' 的文档")
            sys.exit(1)
        codes = sorted(matching_codes)
        print(f"匹配文档: {doc_pattern} → codes={codes}")
        run_pipeline_with_codes(codes, label=f"doc_{doc_pattern}", resume=do_resume)

    else:
        # 批次模式（原有逻辑）
        batch = sys.argv[1]
        run_batch(batch, resume=do_resume)
