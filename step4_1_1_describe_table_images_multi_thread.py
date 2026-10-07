#表格图片描述主脚本——多线程
#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import json
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import pymysql
import db_config

from step4_table_utils_ali_multi_thread import generate_table_description, is_comparable_rent_table, parse_comparable_rent_json  # 调用生成描述的函数
from step4_compress_image import compress_image  # 调用压缩图片函数
from file_paths_config import OUTPUT_DIR
from common_utils import safe_json_dump, safe_json_load

# 获取脚本所在目录，确保日志文件生成在log目录下
script_dir = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.join(script_dir, "log")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "table_detection.log")

# 创建 FileHandler，并指定 encoding 为 "utf-8"
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.WARNING)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

logger = logging.getLogger()
logger.setLevel(logging.WARNING)
logger.addHandler(file_handler)

# 用于并发写 JSON 文件时加锁，避免多线程竞争
json_lock = threading.Lock()
MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

# 读取整数环境变量的助手
def _get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default

def _safe_read_json_file(path):
    """安全读取 JSON 文件，处理编码损坏和格式错误。

    - UnicodeDecodeError: 以 errors='replace' 重新读取后解析
    - JSONDecodeError: 记录日志，删除损坏文件，重新抛出以便调用方重取
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except UnicodeDecodeError:
        logging.warning(f"JSON 文件编码异常 (非 UTF-8 字节): {path}，尝试替换字符重新读取")
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logging.warning(f"JSON 文件编码损坏无法修复: {path}，已删除并等待 API 重新生成")
            os.remove(path)
            raise
    except json.JSONDecodeError:
        logging.warning(f"JSON 文件格式损坏: {path}，已删除并等待 API 重新生成")
        os.remove(path)
        raise

def get_announcement_connection():
    """
    获取数据库 announcement 的连接
    """
    config = db_config.get_db_announcement_config()
    return pymysql.connect(
        host=config["host"],
        port=config["port"],
        user=config["user"],
        password=config["password"],
        database=config["database"],
        charset=config["charset"],
        cursorclass=pymysql.cursors.DictCursor
    )

def get_pending_files_from_db():
    """
    从数据库获取需要进行表格描述的文件
    条件: table_detection_vector_done='true' AND table_detection_scan_done='true' 
          AND table_describe_done='false' AND doc_type_1 != '无关'
    返回: 按fund_code分组的文件列表字典
    """
    try:
        conn = get_announcement_connection()
        with conn.cursor() as cursor:
            sql = """
            SELECT file_name, file_path, date, fund_code, short_name, announcement_title,
                   doc_type_1, doc_type_2, announcement_link, table_detection_vector_done,
                   table_detection_scan_done, table_describe_done
            FROM processed_files 
            WHERE table_detection_vector_done='true' 
              AND table_detection_scan_done='true' 
              AND table_describe_done='false' 
              AND doc_type_1 != '无关'
            ORDER BY fund_code, file_name
            """
            cursor.execute(sql)
            results = cursor.fetchall()
        conn.close()
        
        # 按fund_code分组
        grouped_files = {}
        for row in results:
            fund_code = row['fund_code']
            if fund_code not in grouped_files:
                grouped_files[fund_code] = []
            grouped_files[fund_code].append(row)
        
        return grouped_files
        
    except Exception as e:
        logging.error(f"数据库查询失败: {e}")
        raise e

def _safe_read_json(path):
    try:
        if os.path.exists(path):
            return safe_json_load(path)
    except Exception:
        return None
    return None

def _get_pdf_folder_dir(file_name: str, fund_code: str) -> str:
    pdf_folder_name = os.path.splitext(file_name)[0]
    return os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name)

def _infer_status_from_files(pdf_folder_dir: str):
    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path) or {}
    text_meta = text_json.get("metadata", {}) or {}
    merged = {}
    merged.update(text_meta)
    merged.update(meta)
    return merged


def get_pending_files():
    manifest = _safe_read_json(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {}) or {}
    from common_utils import filter_manifest_files_by_env; files_map = filter_manifest_files_by_env(files_map)
    grouped_files = {}
    for file_name, base_info in files_map.items():
        if not isinstance(base_info, dict):
            continue
        fund_code = (base_info or {}).get("fund_code") or ""
        if not fund_code:
            continue
        pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
        status = _infer_status_from_files(pdf_folder_dir)
        if status.get("doc_type_1") == "无关":
            continue
        if status.get("table_describe_done") is True:
            continue
        vector_done = status.get("table_detection_vector_done")
        scan_done = status.get("table_detection_scan_done")
        if vector_done is not True and scan_done is not True:
            continue
        row = {
            "file_name": file_name,
            "file_path": (base_info or {}).get("file_path", ""),
            "date": (base_info or {}).get("date") or status.get("date") or "",
            "fund_code": fund_code,
            "short_name": (base_info or {}).get("short_name") or status.get("short_name") or "",
            "announcement_title": (base_info or {}).get("announcement_title") or status.get("announcement_title") or "",
            "doc_type_1": status.get("doc_type_1") or "",
            "doc_type_2": status.get("doc_type_2") or "",
            "announcement_link": status.get("announcement_link") or "",
            "table_detection_vector_done": bool(vector_done is True),
            "table_detection_scan_done": bool(scan_done is True),
            "table_describe_done": False
        }
        grouped_files.setdefault(fund_code, []).append(row)
    for fund_code in grouped_files:
        grouped_files[fund_code].sort(key=lambda x: x.get("file_name", ""))
    return grouped_files, False

def update_local_table_describe_done(file_info):
    file_name = file_info.get("file_name", "")
    fund_code = file_info.get("fund_code", "")
    if not file_name or not fund_code:
        return False
    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    meta.update(file_info)
    meta["table_describe_done"] = True
    safe_json_dump(meta, meta_path)

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path)
    if isinstance(text_json, dict):
        text_meta = text_json.get("metadata", {}) or {}
        text_meta["table_describe_done"] = True
        text_json["metadata"] = text_meta
        safe_json_dump(text_json, text_path)

    # [BATCH-FIX] manifest write removed — rebuilt by rebuild_manifest.py after step completes
    return True


def parse_page_numbers_from_filename(filename):
    """
    根据图片文件名（如 "page_2.png" 或 "page_2-3.png"）解析出页码信息，返回字符串。
    """
    name = os.path.splitext(filename)[0]  # 去除扩展名
    if name.startswith("page_"):
        name = name[len("page_"):]
    return name  # 如 "2" 或 "2-3"

def process_single_image(img_file, table_img_dir, pdf_info, describe_json_path, use_db):
    """
    并行处理单张图片的核心逻辑：
      1. 调用 generate_table_description 生成描述，如果失败则尝试压缩后重试
      2. 成功生成描述后，先将信息写入数据库表 table_describe（采用 INSERT ... ON DUPLICATE KEY UPDATE 方式直接更新已有记录）
      3. 数据库写入成功后，再更新 {pdf_folder_name}_table_describe.json 文件
      4. 返回 True/False 表示是否成功（若数据库写入失败则直接返回 False）
    """
    image_path = os.path.join(table_img_dir, img_file)
    # 预压缩大图：>5MB 的 PNG 先压缩再传 API，避免 base64 内存爆炸
    try:
        if os.path.getsize(image_path) > 5 * 1024 * 1024:
            compressed = compress_image(image_path)
            if compressed and os.path.exists(compressed):
                image_path = compressed
    except Exception:
        pass
    # ═══ Fix 7: 空白页防线(2026-10-07用户批准) ═══
    # 纯白图(非白像素<0.5%)→qwen-vl恒返回空→重试全空→整份文档误判失败(黑名单误杀)。
    # 空白页无内容是事实,写占位描述算成功,不走API。降采样统计防内存膨胀(陷阱95教训)。
    _skip_api = False
    try:
        from PIL import Image as _PILImage
        import numpy as _np
        with _PILImage.open(image_path) as _im:
            _gray = _im.convert("L")
            if max(_gray.size) > 500:
                _ratio = 500 / max(_gray.size)
                _gray = _gray.resize((max(1, int(_gray.width * _ratio)), max(1, int(_gray.height * _ratio))))
            if float((_np.asarray(_gray) < 200).mean()) < 0.005:
                print(f"图片 {img_file} 判定为空白页(非白像素<0.5%),写占位描述,跳过API。")
                description = "本页为空白页,无表格内容。"
                _skip_api = True
    except Exception:
        _skip_api = False  # 检测失败保守走原路径
    if not _skip_api:
      try:
        start_time = time.time()
        description = generate_table_description(image_path)
        elapsed_time = time.time() - start_time
        print(f"图片 {img_file} 生成描述耗时 {elapsed_time:.2f} 秒。")
      except Exception as e:
        # ═══ Fix 3: transient error retry with backoff ═══
        transient_types = (BrokenPipeError, TimeoutError, ConnectionError,
                          ConnectionResetError, OSError)
        is_transient = isinstance(e, transient_types) or \
                       any(kw in str(e) for kw in ('Read timed out', 'RemoteDisconnected', 'Connection reset'))
        
        if is_transient:
            for retry_n in range(3):
                wait = 2 * (2 ** retry_n)
                print(f"图片 {img_file} 瞬态错误({type(e).__name__})，重试 {retry_n+1}/3，等待 {wait}s")
                time.sleep(wait)
                try:
                    description = generate_table_description(image_path)
                    print(f"图片 {img_file} 重试成功。")
                    break
                except Exception as e2:
                    if retry_n == 2:
                        print(f"图片 {img_file} 3次重试全部失败: {e2}")
                    continue
            else:
                # All retries failed → fall through to compress retry
                pass
        if 'description' not in dir():
            # Still failed — let existing compress-retry handle it
            if isinstance(e, ConnectionResetError):
                print(f"图片 {img_file} 描述生成失败: 远程连接被重置，尝试压缩后重试。")
            elif "Read timed out" in str(e):
                print(f"图片 {img_file} 描述生成失败: 请求超时，尝试压缩后重试。")
            elif "DashScope API 多次请求未返回有效流式输出" in str(e):
                print(f"图片 {img_file} 描述生成失败: DashScope API 请求失败，尝试压缩后重试。")
            else:
                msg = f"图片 {img_file} 描述生成失败: {e}"
                print(msg)
            #logging.warning(msg)
        try:
            # 压缩后重试
            compressed_path = compress_image(image_path)
            if compressed_path is None:
                compressed_path = image_path
            print(f"使用压缩图片: {compressed_path} 重新生成描述...")
            start_time = time.time()
            description = generate_table_description(compressed_path)
            elapsed_time = time.time() - start_time
            print(f"压缩后图片 {img_file} 生成描述耗时 {elapsed_time:.2f} 秒。")
            image_path = compressed_path
        except Exception as ex:
            msg = f"图片 {img_file} 压缩后描述生成失败: {ex}"
            print(msg)
            #logging.warning(msg)
            return False  # 不写 JSON，也不更新数据库

    # 清洗描述文本，确保为合法 UTF-8 字符串，避免 JSON 序列化时崩溃
    if description:
        description = description.encode("utf-8", errors="replace").decode("utf-8")
    if not isinstance(description, str) or not description.strip():
        print(f"图片 {img_file} 描述为空或无效，跳过。")
        return False

    # 构造记录
    page_info = parse_page_numbers_from_filename(img_file)
    source_file = os.path.basename(pdf_info.get("file_path", "")) or pdf_info.get("file_name", "")
    record = {
        "page_num": page_info,
        "picture_path": image_path,
        "file_path": pdf_info.get("file_path"),
        "fund_code": pdf_info.get("fund_code"),
        "short_name": pdf_info.get("short_name"),
        "announcement_title": pdf_info.get("announcement_title"),
        "source_file": source_file,
        "description": description
    }

    if use_db:
        try:
            conn = get_announcement_connection()
            with conn.cursor() as cursor:
                sql = """
                INSERT INTO table_describe 
                (fund_code, short_name, announcement_title, source_file, page_num, picture_path, file_path, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    fund_code = VALUES(fund_code),
                    short_name = VALUES(short_name),
                    announcement_title = VALUES(announcement_title),
                    picture_path = VALUES(picture_path),
                    file_path = VALUES(file_path),
                    description = VALUES(description)
                """
                params = (
                    record["fund_code"],
                    record["short_name"],
                    record["announcement_title"],
                    record["source_file"],
                    record["page_num"],
                    record["picture_path"],
                    record["file_path"],
                    record["description"]
                )
                cursor.execute(sql, params)
                conn.commit()
            conn.close()
        except Exception as e:
            msg = f"图片 {img_file} 插入数据库失败: {e}"
            print(msg)
            logging.warning(msg)
            return False

    with json_lock:
        if os.path.exists(describe_json_path):
            table_descriptions = _safe_read_json_file(describe_json_path)
            if table_descriptions is None:
                table_descriptions = {}
        else:
            table_descriptions = {}
        table_descriptions[img_file] = record
        safe_json_dump(table_descriptions, describe_json_path)
    
    # ─── OCR阶段结构化提取：检测「可比实例」表 → 追加结构化JSON ───
    if is_comparable_rent_table(description):
        pdf_folder_path = os.path.dirname(describe_json_path)
        comp_json_path = os.path.join(pdf_folder_path, "comp_rents_structured.json")
        try:
            # 只处理尚未提取的（避免重复API调用）
            if not os.path.exists(comp_json_path):
                structured = parse_comparable_rent_json(description)
                if "error" not in structured:
                    structured["fund_code"] = pdf_info.get("fund_code", "")
                    structured["source_file"] = os.path.basename(pdf_info.get("file_path", "")) or pdf_info.get("file_name", "")
                    structured["page_num"] = parse_page_numbers_from_filename(img_file)
                    safe_json_dump(structured, comp_json_path)
                    print(f"  → 可比租金结构化JSON已保存: {comp_json_path}")
                else:
                    print(f"  → 可比租金结构化失败: {structured.get('error')}")
        except Exception as e:
            print(f"  → 可比租金结构化异常: {e}")
    print(f"图片 {img_file} 已写入描述文件。")
    return True

def process_pdf_table_descriptions(pdf_info, max_workers=15, use_db=True, progress_secs=30, api_semaphore=None):
    """
    并行处理单个 PDF 文件的所有尚未描述的表格图片：
      1. 查找该 PDF 文件夹下的 table_image 文件夹
      2. 对每张未处理的图片调用 process_single_image
      3. 如果所有图片均成功，则返回 True；若任意图片处理失败则返回 False
    """
    pdf_filename = os.path.basename(pdf_info.get("file_path", "")) or pdf_info.get("file_name", "")
    pdf_folder_name = os.path.splitext(pdf_filename)[0]

    fund_code = pdf_info.get("fund_code", "")
    if not fund_code:
        msg = f"未找到基金文件夹，基金代码: {fund_code}，跳过 {pdf_filename}"
        print(msg)
        logging.warning(msg)
        return False

    pdf_folder_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name)
    if not os.path.exists(pdf_folder_path):
        msg = f"未找到PDF文件夹: {pdf_folder_path}，跳过 {pdf_filename}"
        print(msg)
        logging.warning(msg)
        return False

    table_img_dir = os.path.join(pdf_folder_path, "table_image")
    if not os.path.exists(table_img_dir):
        msg = f"文件夹 {table_img_dir} 不存在，跳过 {pdf_filename}"
        print(msg)
        logging.warning(msg)
        return False

    # 统一使用简短文件名，避免超长路径/文件名导致报错
    describe_json_path = os.path.join(pdf_folder_path, "table_describe.json")

    # 读取已处理的图片记录
    if os.path.exists(describe_json_path):
        table_descriptions = _safe_read_json_file(describe_json_path)
        if table_descriptions is None:
            table_descriptions = {}
    else:
        table_descriptions = {}

    # 找到所有需要处理的图片
    img_files = [
        f for f in os.listdir(table_img_dir)
        if f.lower().endswith(".png") and "compressed" not in f.lower()
    ]
    print(f"在 {pdf_folder_name} 中找到 {len(img_files)} 个表格图片。")

    # 过滤掉已经描述完成的图片
    to_process = [f for f in img_files if f not in table_descriptions]
    if not to_process:
        print(f"所有图片均已描述，跳过 {pdf_filename}")
        return True
    print(f"有 {len(to_process)} 张图片需要并行处理...")

    # 并行处理 + 进度心跳
    all_images_success = True

    progress_lock = threading.Lock()
    progress = {"start_ts": time.time(), "total": len(to_process), "done": 0, "failed": 0}
    stop_event = threading.Event()

    def _heartbeat():
        while not stop_event.wait(max(1, int(progress_secs))):
            with progress_lock:
                done = progress["done"]; total = progress["total"]; failed = progress["failed"]
                elapsed = time.time() - progress["start_ts"]
            print(f"{pdf_folder_name} 进度: {done}/{total}，失败: {failed}，已用时: {elapsed:.0f}s")

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()

    # Semaphore 限流：限制单PDF同时只有3个 DashScope API 调用（最优=3, RPM<2%）
    _semaphore = api_semaphore or threading.Semaphore(3)

    def _throttled_process_single_image(img_file, table_img_dir, pdf_info, describe_json_path, use_db):
        _semaphore.acquire()
        try:
            return process_single_image(img_file, table_img_dir, pdf_info, describe_json_path, use_db)
        finally:
            _semaphore.release()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _throttled_process_single_image, img_file, table_img_dir, pdf_info, describe_json_path, use_db
            ): img_file
            for img_file in to_process
        }
        for future in as_completed(futures):
            img_name = futures[future]
            success = future.result()
            with progress_lock:
                progress["done"] += 1
                if not success:
                    progress["failed"] += 1
            if not success:
                all_images_success = False

    stop_event.set()

    if all_images_success:
        print(f"完成 {pdf_folder_name} 的表格描述（并行处理）。")
    else:
        print(f"{pdf_folder_name} 中有部分图片处理失败。")
    return all_images_success

def main():
    processed_files, use_db = get_pending_files()
    max_workers = _get_env_int("TABLE_DESC_MAX_WORKERS", 2)
    progress_secs = _get_env_int("TABLE_DESC_PROGRESS_SECS", 30)
    print(f"并发线程数: {max_workers}，进度心跳: {progress_secs}s")
    logging.info(f"max_workers={max_workers}, progress_secs={progress_secs}")

    # 全局速率限制：跨PDF总并发 DashScope 调用 ≤ 4
    global_api_semaphore = threading.Semaphore(3)  # 降低到3避免内存压力

    # 收集待处理PDF
    files_to_process = []
    for fund_code, pdf_list in processed_files.items():
        files_to_process.extend(pdf_list)

    if not files_to_process:
        msg = "没有找到需要处理的文件。"
        print(msg)
        logging.warning(msg)
        print("表格图片描述全部完成！描述信息已写入本地文件。")
        logging.warning("表格图片描述全部完成！描述信息已写入本地文件。")
        return

    total_files = len(files_to_process)
    print(f"本次需要处理 {total_files} 个文件。")
    logging.info(f"待处理文件总数: {total_files}")

    processed_count = 0
    failed_files = []  # 用于记录处理失败的文件及原因

    # 文件层级处理
    for idx, pdf_info in enumerate(files_to_process, start=1):
        file_name = pdf_info.get("file_name")
        print(f"\n----- 正在处理第 {idx}/{total_files} 个文件: {file_name} -----")
        logging.info(f"开始处理文件: {file_name}")
        success = process_pdf_table_descriptions(
            pdf_info,
            max_workers=max_workers,
            use_db=use_db,
            progress_secs=progress_secs,
            api_semaphore=global_api_semaphore
        )
        if success:
            ok = update_local_table_describe_done(pdf_info)
            if ok:
                msg = f"已更新本地状态 {file_name} 的 table_describe_done=True。"
                print(msg)
                logging.info(msg)
                processed_count += 1
            else:
                msg = f"文件 {file_name} 更新本地状态失败。"
                print(msg)
                logging.warning(msg)
                failed_files.append((file_name, "更新本地状态失败"))
        else:
            msg = f"{file_name} 处理表格描述失败。"
            print(msg)
            logging.warning(msg)
            failed_files.append((file_name, "部分图片处理失败"))
        print(f"文件层级进度: 已完成 {idx}/{total_files} 个文件")

    remaining_files = total_files - processed_count
    summary_msg = (
        f"\n处理总结：\n"
        f"待处理文件总数: {total_files}\n"
        f"本次成功处理: {processed_count}\n"
        f"未处理文件: {remaining_files}\n"
    )
    print(summary_msg)
    logging.warning(summary_msg)

    if failed_files:
        fail_msg = "处理失败的文件及原因：\n" + "\n".join([f"文件: {fn}, 原因: {reason}" for fn, reason in failed_files])
        print(fail_msg)
        logging.warning(fail_msg)

    final_msg = "表格图片描述全部完成！描述信息已写入本地文件。"
    print(f"\n{final_msg}")
    logging.warning(final_msg)

if __name__ == "__main__":
    main()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()
