#!/usr/bin/env python3
"""
REITs Text Data Pipeline — 全自动批量执行器

Usage:
    python batch_runner.py <BATCH_NAME>

Example:
    python batch_runner.py B2b
    nohup python batch_runner.py B2b >> /tmp/b2b_full_20260607_2252.log 2>&1 &

按序执行所有管道步骤，遇错即停，全部通过后输出摘要并写完成标记。
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

PYTHON_BIN = "/Users/pyemini/anaconda3/envs/deepseek-ocr/bin/python"
BATCH_CONFIG_PATH = SCRIPT_DIR / "BATCH_CONFIG.json"

PIPELINE = [
    ("step1_process_pdfs.py",                                None),
    ("step2_extract_text_onlyvactor_multi_process.py",       None),
    ("step3_1_detection_vactor_multi_process.py",            None),
    ("step3_2_table_detection_scan_multifile.py",            None),
    ("step4_1_1_describe_table_images_multi_thread.py",      None),
    ("step4_2_1_describe_not_table_images_llm.py",           None),
    ("step5_merge_table_into_text.py",                       ["--gate"]),
    ("step6_text_segmentation.py",                           None),
    ("step7_text_embedding.py",                              None),
    ("step8_1_ingest_elasticsearch_data.py",                 None),
    ("gate2_accuracy_check.py",                              "direct"),
    ("step8_2_ingest_vector_database.py",                    None),
]


def ts() -> str:
    """Return current time as [HH:MM:SS] prefix."""
    return datetime.now().strftime("[%H:%M:%S]")


def run_cmd(cmd: list, description: str) -> int:
    """Run a command, streaming stdout/stderr in real-time. Returns exit code."""
    print(f"\n{ts()} ── {description} ──")
    print(f"{ts()} CMD: {' '.join(cmd)}")
    print("-" * 60, flush=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(SCRIPT_DIR),
        env=env,
        bufsize=1,
        universal_newlines=True,
    )

    for line in proc.stdout:
        line = line.rstrip("\n")
        print(f"{ts()} {line}", flush=True)

    proc.wait()
    rc = proc.returncode
    print("-" * 60, flush=True)
    return rc


def run():
    if len(sys.argv) != 2:
        print("Usage: python batch_runner.py <BATCH_NAME>")
        print("Example: python batch_runner.py B2b")
        sys.exit(1)

    batch_name = sys.argv[1]

    # Load batch config
    if not BATCH_CONFIG_PATH.exists():
        print(f"{ts()} ERROR: BATCH_CONFIG.json not found at {BATCH_CONFIG_PATH}")
        sys.exit(1)

    with open(BATCH_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    if batch_name not in config:
        print(f"{ts()} ERROR: Unknown batch '{batch_name}'. Available: {list(config.keys())}")
        sys.exit(1)

    batch = config[batch_name]
    codes = batch["codes"]
    docs = batch["docs"]

    print(f"{ts()} ══════════════════════════════════════════════")
    print(f"{ts()}  REITs Pipeline — Batch Runner")
    print(f"{ts()}  Batch:   {batch_name}")
    print(f"{ts()}  Codes:   {codes}")
    print(f"{ts()}  Docs:    {docs}")
    print(f"{ts()}  Steps:   {len(PIPELINE)}")
    print(f"{ts()} ══════════════════════════════════════════════")

    passed = 0
    failed = 0
    start_time = datetime.now()

    for script, extra in PIPELINE:
        if extra == "direct":
            # Run directly (GATE2)
            cmd = [PYTHON_BIN, str(SCRIPT_DIR / script), batch_name]
            description = f"GATE2 — {script}"
        elif extra is not None:
            # Step with extra args (e.g., step5 --gate) — run directly, not via run_step.py
            # run_step.py only accepts 2 positional args and doesn't forward extras.
            # step5 with --gate reads BATCH_CONFIG internally and self-filters.
            cmd = [PYTHON_BIN, str(SCRIPT_DIR / script)]
            for e in extra:
                cmd.append(e)
            cmd.append(batch_name)  # --gate <BATCH>
            description = f"Step: {script} {' '.join(extra)} {batch_name} (direct)"
        else:
            # Regular step via run_step.py
            cmd = [PYTHON_BIN, str(SCRIPT_DIR / "run_step.py"), batch_name, script]
            description = f"Step: {script}"

        rc = run_cmd(cmd, description)

        if rc == 0:
            print(f"{ts()} ✅ OK — {script}")
            passed += 1
        else:
            print(f"{ts()} ❌ FAILED (exit code {rc}) — {script}")
            failed += 1
            break

    elapsed = (datetime.now() - start_time).total_seconds()

    # ── Final summary ──
    print(f"\n{ts()} ══════════════════════════════════════════════")
    if failed > 0:
        print(f"{ts()}  PIPELINE ABORTED — {passed}/{passed + failed} steps passed")
        print(f"{ts()}  Failed at step: {script}")
        print(f"{ts()}  Elapsed: {elapsed:.0f}s")
        print(f"{ts()} ══════════════════════════════════════════════")
        sys.exit(1)

    # All steps passed — query ES and write completion marker
    print(f"{ts()}  ALL {passed} STEPS PASSED")

    # Query ES document count
    es_total = "N/A"
    try:
        curl_cmd = ["curl", "-s", "localhost:9200/reits_announcements/_count"]
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            es_data = json.loads(result.stdout)
            es_total = es_data.get("count", "N/A")
            print(f"{ts()}  ES total documents: {es_total}")
        else:
            print(f"{ts()}  ⚠️ ES query failed: {result.stderr}")
    except Exception as e:
        print(f"{ts()}  ⚠️ ES query error: {e}")

    print(f"{ts()}  Elapsed: {elapsed:.0f}s")
    print(f"{ts()} ══════════════════════════════════════════════")

    # Write completion marker
    marker_path = Path(f"/tmp/{batch_name}_complete.txt")
    completed_at = datetime.now(timezone.utc).isoformat()
    marker_content = (
        f"BATCH={batch_name}\n"
        f"STATUS=PASS\n"
        f"ES_TOTAL={es_total}\n"
        f"COMPLETED_AT={completed_at}\n"
        f"GATE2=PASS\n"
    )
    marker_path.write_text(marker_content)
    print(f"{ts()}  Completion marker: {marker_path}")

    sys.exit(0)


if __name__ == "__main__":
    run()
