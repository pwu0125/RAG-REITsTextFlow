#矢量页跨页表格检测,多进程处理
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
step3_1_detection_vactor_multi_process.py
(矢量页跨页表格检测，多进程)

"""

import os
import fitz
import pdfplumber
from PIL import Image
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from file_paths_config import OUTPUT_DIR, PDF_DIR

from common_utils import safe_json_dump, safe_json_load

MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

# -------------------- 日志设置 --------------------
logger = logging.getLogger("TableDetection")
logger.setLevel(logging.DEBUG)  # 内部记录全部信息

# 创建日志目录，使用相对路径
script_dir = os.path.dirname(os.path.abspath(__file__))
log_dir = os.path.join(script_dir, "log")
os.makedirs(log_dir, exist_ok=True)
log_file_path = os.path.join(log_dir, 'table_processing_vactor_multifile.log')
file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler.setFormatter(file_formatter)

class FileFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # 保留 WARNING+ 或 特殊提示
        if msg in ["没有找到需要处理的文件。", "矢量表格检测全部完成！状态已写回本地。", "矢量表格检测全部完成！状态以更新至数据库中。"]:
            return True
        return record.levelno >= logging.WARNING

file_handler.addFilter(FileFilter())

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
console_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
console_handler.setFormatter(console_formatter)

class TerminalFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        # 允许 特殊提示
        if msg in ["没有找到需要处理的文件。", "矢量表格检测全部完成！状态已写回本地。", "矢量表格检测全部完成！状态以更新至数据库中。"]:
            return True
        if record.levelno >= logging.WARNING:
            return True
        if record.levelno == logging.INFO and ("处理文件:" in msg or "添加任务:" in msg):
            return True
        return False

console_handler.addFilter(TerminalFilter())
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# -------------------- 参数设置 --------------------
THRESHOLD_TOP = 90
THRESHOLD_BOTTOM = 90
DPI = 300

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


def get_pending_files_from_local():
    if not os.path.exists(OUTPUT_DIR):
        return {}

    manifest = _safe_read_json(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {}) or {}

    grouped = {}
    for file_name, base_info in files_map.items():
        fund_code = (base_info or {}).get("fund_code") or ""
        if not fund_code:
            continue

        pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
        status = _infer_status_from_files(pdf_folder_dir)

        if status.get("doc_type_1") == "无关":
            continue
        if status.get("text_extracted") is not True:
            continue
        if status.get("table_detection_vector_done") is True:
            continue

        row = {
            "file_name": file_name,
            "file_path": (base_info or {}).get("file_path") or os.path.join(PDF_DIR, file_name),
            "date": (base_info or {}).get("date") or status.get("date") or "",
            "fund_code": fund_code,
            "short_name": (base_info or {}).get("short_name") or status.get("short_name") or "",
            "announcement_title": (base_info or {}).get("announcement_title") or status.get("announcement_title") or "",
            "doc_type_1": status.get("doc_type_1") or "",
            "doc_type_2": status.get("doc_type_2") or "",
            "announcement_link": status.get("announcement_link") or "",
        }

        grouped.setdefault(fund_code, []).append(row)

    for fund_code in grouped:
        grouped[fund_code].sort(key=lambda x: x.get("file_name", ""))

    return grouped


def update_local_table_detection_done(file_info):
    file_name = file_info["file_name"]
    fund_code = file_info["fund_code"]
    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    meta.update(file_info)
    meta["text_extracted"] = True
    meta["table_detection_vector_done"] = True
    safe_json_dump(meta, meta_path)

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path)
    if isinstance(text_json, dict):
        text_meta = text_json.get("metadata", {}) or {}
        text_meta.update({
            "text_extracted": True,
            "table_detection_vector_done": True,
        })
        text_json["metadata"] = text_meta
        safe_json_dump(text_json, text_path)

    # [BATCH-FIX] manifest write removed — rebuilt by rebuild_manifest.py after step completes
    return True

def detect_tables_in_page(pdf_path, page_number):
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number]
        tables = page.find_tables()
        bboxes = [tbl.bbox for tbl in tables]

        logger.info(f"[Page {page_number + 1}] 表格检测结果: 发现 {len(tables)} 个表格")
        for i, bbox in enumerate(bboxes, 1):
            logger.info(f"  表格{i}边界框: (x0={bbox[0]:.1f}, y0={bbox[1]:.1f}, x1={bbox[2]:.1f}, y1={bbox[3]:.1f})")
        return (len(tables) > 0), bboxes

def convert_pdf_page_to_image(pdf_path, page_number, output_dir):
    doc = fitz.open(pdf_path)
    page = doc.load_page(page_number)
    pix = page.get_pixmap(dpi=DPI)
    img_path = os.path.join(output_dir, f"page_{page_number + 1}.png")
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    img.save(img_path, "PNG")
    logger.info(f"  已生成图片: {img_path}")
    doc.close()
    return img_path

def process_pdf(pdf_path, output_dir):
    """
    对单个PDF进行矢量表格检测:
      - 遍历每页, detect_tables_in_page
      - 有表格 => convert_pdf_page_to_image
      - 再检测跨页 => 拼接
    """
    with pdfplumber.open(pdf_path) as pdf:
        total_pages = len(pdf.pages)
        table_info_list = []
        logger.info("="*50)
        logger.info(f"开始处理PDF(矢量检测): {pdf_path},总页数:{total_pages}")

        for pg_i, page in enumerate(pdf.pages):
            logger.info(f"处理文件: {os.path.basename(pdf_path)} - 第 {pg_i+1}/{total_pages} 页:")
            has_table, bboxes = detect_tables_in_page(pdf_path, pg_i)
            img_path = None
            if has_table:
                img_path = convert_pdf_page_to_image(pdf_path, pg_i, output_dir)
            else:
                logger.info("  本页未检测到表格,跳过生成图片")
            page_height = page.height
            first_bbox = bboxes[0] if has_table else None
            last_bbox = bboxes[-1] if has_table else None

            table_info_list.append({
                "page_num": pg_i,
                "has_table": has_table,
                "first_bbox": first_bbox,
                "last_bbox": last_bbox,
                "page_height": page_height,
                "img_path": img_path
            })

    i = 0
    while i < len(table_info_list) - 1:
        pending_merge = []
        merged_pages = []
        while i < len(table_info_list) - 1:
            current = table_info_list[i]
            nxt = table_info_list[i+1]
            logger.info(f"检查跨页表格: 第{i+1}页 与 第{i+2}页")
            if current["has_table"] and nxt["has_table"]:
                current_last_bbox = current["last_bbox"]
                current_height = current["page_height"]
                next_first_bbox = nxt["first_bbox"]

                if current_last_bbox:
                    dist_current = current_height - current_last_bbox[3]
                else:
                    dist_current = 99999
                if next_first_bbox:
                    dist_next = next_first_bbox[1]
                else:
                    dist_next = 99999

                logger.info(f"  当前页表格底部距离: {dist_current:.1f}点(阈值={THRESHOLD_BOTTOM})")
                logger.info(f"  下页表格顶部距离: {dist_next:.1f}点(阈值={THRESHOLD_TOP})")
                if dist_current <= THRESHOLD_BOTTOM and dist_next <= THRESHOLD_TOP:
                    logger.info("  ✅ 满足跨页条件 => 合并队列")
                    pending_merge.append(current)
                    merged_pages.append(i+1)
                else:
                    break
            else:
                break
            i += 1

        if pending_merge:
            pending_merge.append(table_info_list[i])
            merged_pages.append(i+1)
            logger.info(f"  ⚡ 合并 {merged_pages}页的跨页表格")
            scale = DPI / 72
            merged_imgs = []
            for pginfo in pending_merge:
                if not pginfo["img_path"]:
                    continue
                img = Image.open(pginfo["img_path"])
                if pginfo == pending_merge[0]:
                    crop_lower = int(pginfo["last_bbox"][3] * scale) if pginfo["last_bbox"] else img.height
                    cropped_img = img.crop((0, 0, img.width, crop_lower))
                elif pginfo == pending_merge[-1]:
                    crop_upper = int(pginfo["first_bbox"][1] * scale) if pginfo["first_bbox"] else 0
                    cropped_img = img.crop((0, crop_upper, img.width, img.height))
                else:
                    if pginfo["first_bbox"] and pginfo["last_bbox"]:
                        crop_up = int(pginfo["first_bbox"][1] * scale)
                        crop_lo = int(pginfo["last_bbox"][3] * scale)
                    else:
                        crop_up, crop_lo = 0, img.height
                    cropped_img = img.crop((0, crop_up, img.width, crop_lo))
                merged_imgs.append(cropped_img)

            if merged_imgs:
                total_h = sum(im.height for im in merged_imgs)
                merged_img = Image.new('RGB', (merged_imgs[0].width, total_h))
                y_off = 0
                for mg in merged_imgs:
                    merged_img.paste(mg, (0, y_off))
                    y_off += mg.height
                merged_name = f"page_{merged_pages[0]}-{merged_pages[-1]}.png"
                merged_path = os.path.join(output_dir, merged_name)
                merged_img.save(merged_path)
                logger.info(f"  ✅ 已保存合并图片: {merged_path}")
                for pginfo in pending_merge:
                    if pginfo["img_path"] and os.path.exists(pginfo["img_path"]):
                        os.remove(pginfo["img_path"])
                        logger.info(f"  ❌ 删除原始图片: {pginfo['img_path']}")
        i += 1

    logger.info(f"PDF(矢量检测)处理完成: {pdf_path}")
    logger.info("="*50)
    return table_info_list

def process_pdf_file(task):
    """
    执行矢量检测:
      - detect tables
      - 若成功 => update_db_table_detection_done => table_detection_vector_done='true'
      - 若失败 => 不设置
    """
    fund_code, file_info, pdf_path, output_dir = task
    file_name = file_info["file_name"]
    try:
        process_pdf(pdf_path, output_dir)
        update_local_table_detection_done(file_info)
        return (fund_code, file_info, None)
    except Exception as e:
        logger.warning(f"处理 {file_name} 失败: {e}")
        return (fund_code, file_info, str(e))

def main():
    processed_files = get_pending_files_from_local()

    tasks = []
    # 收集待处理
    for fund_code, fund_list in processed_files.items():
        for fi in fund_list:
            fn = fi["file_name"]
            pdf_path = os.path.join(PDF_DIR, fn)
            if not os.path.exists(pdf_path):
                logger.warning(f"PDF不存在: {pdf_path}")
                continue
            pdf_folder_name = os.path.splitext(fn)[0]
            pdf_folder_dir = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name)
            if not os.path.exists(pdf_folder_dir):
                logger.warning(f"输出子目录不存在: {pdf_folder_dir}")
                continue
            table_img_dir = os.path.join(pdf_folder_dir, "table_image")
            os.makedirs(table_img_dir, exist_ok=True)

            tasks.append((fund_code, fi, pdf_path, table_img_dir))
            logger.info(f"添加任务: {fn}")

    total_count = len(tasks)
    if total_count == 0:
        logger.info("没有找到需要处理的文件。")
        print("没有找到需要处理的文件。")
        return

    success_count = 0
    fail_list = []

    with ProcessPoolExecutor(max_workers=2) as executor:
        future_map = {executor.submit(process_pdf_file, t): t for t in tasks}
        for fut in as_completed(future_map):
            (fund_code, upd_info, err) = fut.result()
            file_name = upd_info["file_name"]
            if err is None:
                success_count += 1
            else:
                fail_list.append((file_name, err))

    remain = total_count - success_count
    print(f"共 {total_count} 个文件需矢量检测, 成功 {success_count} 个, 失败 {remain} 个.")
    if fail_list:
        print("失败文件:")
        for (fname, reason) in fail_list:
            print(f"  - {fname}: {reason}")

    logger.warning(f"待处理文件数:{total_count}, 本次处理:{total_count}, 成功:{success_count}, 剩余:{remain}")
    if fail_list:
        logger.warning(f"失败文件({len(fail_list)})详情:")
        for (fname, reason) in fail_list:
            logger.warning(f"  {fname}, 原因:{reason}")

    # 全部成功 => 提示
    if remain == 0:
        msg = "矢量表格检测全部完成！状态已写回本地。"
        logger.info(msg)
        print(msg)
    else:
        print("矢量检测部分完成,请查看失败详情.")

if __name__ == "__main__":
    main()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()
