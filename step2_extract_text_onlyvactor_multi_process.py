                                                                                                                #!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
step2_a_extract_text_onlyvactor_multi_process.py
文本提取，多进程处理

"""

import os
import regex as re
import json
import datetime
import pdfplumber
import fitz
import numpy as np
from PIL import Image
import concurrent.futures  # 多进程
import logging

from file_paths_config import OUTPUT_DIR, PDF_DIR
from common_utils import safe_json_dump, safe_json_load

PAGE_CHAR_THRESHOLD = 5
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========== 自定义JSON编码器 ===========
class DateTimeEncoder(json.JSONEncoder):
    """自定义JSON编码器，处理日期时间序列化"""
    def default(self, obj):
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.strftime('%Y-%m-%d')
        return super().default(obj)

# ========== 日志配置 ===========
# 创建日志目录，使用相对路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, "extract_text_onlyvactor.log")
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
fh = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
fh.setLevel(logging.WARNING)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
fh.setFormatter(formatter)
logger.addHandler(fh)

MANIFEST_FILE = os.path.join(OUTPUT_DIR, "processed_files_local.json")

DEFAULT_STATUS = {
    "text_extracted": False,
    "table_detection_vector_done": False,
    "table_detection_scan_done": False,
    "table_describe_done": False,
    "not_table_describe_done": False,
    "merge_done": False,
    "text_segmentation": False,
    "embedding_done": False,
    "vector_database_done": False,
    "elasticsearch_database_done": False,
}


def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        try:
            data = safe_json_load(MANIFEST_FILE)
            if isinstance(data, dict) and "files" in data and isinstance(data["files"], dict):
                from common_utils import filter_manifest_files_by_env
                data["files"] = filter_manifest_files_by_env(data["files"])
                return data
        except Exception:
            pass
    return {
        "generated_at": "",
        "source_dir": PDF_DIR,
        "output_dir": OUTPUT_DIR,
        "files": {}
    }


def save_manifest(manifest):
    manifest["generated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_json_dump(manifest, MANIFEST_FILE)


def _update_meta_json_status(fund_code: str, pdf_folder_name: str, updates: dict) -> None:
    meta_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "meta.json")
    if not os.path.exists(meta_path):
        return
    try:
        meta = safe_json_load(meta_path)
        if isinstance(meta, dict):
            meta.update(updates)
            safe_json_dump(meta, meta_path)
    except Exception as e:
        logging.warning(f"Failed to update meta.json {meta_path}: {e}")
        return


def _mark_manifest_status(manifest: dict, file_name: str, updates: dict) -> None:
    files = manifest.get("files", {})
    entry = files.get(file_name)
    if not isinstance(entry, dict):
        return
    entry.update(updates)
    files[file_name] = entry
    manifest["files"] = files


def _ensure_manifest_entry(manifest: dict, file_info: dict) -> None:
    file_name = (file_info or {}).get("file_name")
    if not file_name:
        return
    files = manifest.get("files", {})
    entry = files.get(file_name)
    if isinstance(entry, dict):
        return
    new_entry = {
        "file_name": file_name,
        "file_path": (file_info or {}).get("file_path", ""),
        "date": (file_info or {}).get("date", ""),
        "fund_code": (file_info or {}).get("fund_code", ""),
        "short_name": (file_info or {}).get("short_name", ""),
        "announcement_title": (file_info or {}).get("announcement_title", ""),
        "doc_type_1": (file_info or {}).get("doc_type_1", ""),
        "doc_type_2": (file_info or {}).get("doc_type_2", ""),
        "announcement_link": (file_info or {}).get("announcement_link", ""),
    }
    new_entry.update(DEFAULT_STATUS)
    files[file_name] = new_entry
    manifest["files"] = files


def reconcile_manifest_text_extracted(manifest: dict) -> int:
    updated = 0
    files = manifest.get("files", {})
    if not isinstance(files, dict):
        return 0

    for file_name, entry in files.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("text_extracted") is True:
            continue

        fund_code = entry.get("fund_code", "")
        if not fund_code:
            continue

        pdf_folder_name = os.path.splitext(file_name)[0]
        text_json_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "text.json")
        if not os.path.exists(text_json_path):
            continue

        try:
            with open(text_json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            meta = data.get("metadata", {}) or {}
            if meta.get("text_extracted") is True:
                _mark_manifest_status(manifest, file_name, {"text_extracted": True})
                _update_meta_json_status(fund_code, pdf_folder_name, {"text_extracted": True})
                updated += 1
        except Exception:
            continue

    return updated



def _parse_pdf_filename(file_name: str):
    """从文件名里尽量解析出 fund_code / short_name / announcement_title / date。"""
    base = os.path.basename(file_name)

    m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{6}\.\w{2})_(.*?)_(.+)\.pdf$", base)
    if m:
        date, fund_code, short_name, announcement_title = m.groups()
        return {
            "date": date,
            "fund_code": fund_code,
            "short_name": short_name,
            "announcement_title": announcement_title,
        }

    m = re.match(r"(\d{6})-(.+)-(\d{4}-\d{2}-\d{2})\.pdf$", base)
    if m:
        fund_code, mid, date = m.groups()
        if "_" in mid:
            short_name, announcement_title = mid.split("_", 1)
        else:
            short_name, announcement_title = "", mid
        return {
            "date": date,
            "fund_code": fund_code,
            "short_name": short_name,
            "announcement_title": announcement_title,
        }

    return None


def get_pending_files_from_local():
    """直接扫描 PDF_DIR，找出还没生成 text.json 的 PDF。"""
    if not os.path.exists(PDF_DIR):
        raise Exception(f"PDF_DIR 不存在: {PDF_DIR}")

    from common_utils import is_manifest_filtered
    _filtered = is_manifest_filtered()
    if _filtered:
        manifest = load_manifest()
        manifest_files = set(manifest.get("files", {}).keys())

    pending = []
    for fn in os.listdir(PDF_DIR):
        if not fn.lower().endswith('.pdf'):
            continue

        # Whitelist mode: 跳过不在过滤后 manifest 中的文件
        if _filtered and fn not in manifest_files:
            continue

        parsed = _parse_pdf_filename(fn)
        if not parsed:
            print(f"[跳过] 文件名无法解析(需要手动改名或扩展解析规则): {fn}")
            continue

        fund_code = parsed["fund_code"]
        pdf_folder_name = os.path.splitext(fn)[0]
        output_json_file = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "text.json")

        # 修复 2026-08-15: 已完成判定优先读 meta.json（权威源）。
        # 根因: pipeline_controller._auto_mark_merge_done 只写 meta.json merge_done=True，
        # 不回写 text.json → 4393 个已完成文件被误判 pending（无过滤裸跑会重复处理）。
        # 边界: meta.json 不存在/解析失败/字段缺失 → 不在此处跳过，落到下方 text.json 逻辑保守处理。
        meta_path = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta_data = json.load(f)
                if isinstance(meta_data, dict) and meta_data.get("merge_done") is True:
                    # ① meta.json merge_done=True: step5已完成正常删图 → 跳过
                    continue
            except Exception:
                pass

        if os.path.exists(output_json_file):
            try:
                with open(output_json_file, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
                meta = existing.get("metadata", {}) or {}
                if meta.get("text_extracted") is True:
                    # ① text.json merge_done=True: 历史文件（仅 text.json 有标志）→ 跳过
                    if meta.get("merge_done") is True:
                        continue
                    # ② temp_pdf_images存在且非空 → 跳过
                    temp_img_dir = os.path.join(OUTPUT_DIR, fund_code, pdf_folder_name, "temp_pdf_images")
                    if os.path.isdir(temp_img_dir) and os.listdir(temp_img_dir):
                        continue
                    # ③ 否则: 图片丢失（curation清理但merge未完成）→ 不跳过，重渲染
            except Exception:
                pass

        file_path = os.path.join(PDF_DIR, fn)
        file_info = {
            "file_name": fn,
            "file_path": file_path,
            "date": parsed["date"],
            "fund_code": fund_code,
            "short_name": parsed["short_name"],
            "announcement_title": parsed["announcement_title"],
            "doc_type_1": "",
            "doc_type_2": "",
            "announcement_link": "",
            "text_extracted": False,
            "table_detection_vector_done": False,
            "table_detection_scan_done": False,
            "table_describe_done": False,
            "not_table_describe_done": False,
            "merge_done": False,
            "text_segmentation": False,
            "embedding_done": False,
            "vector_database_done": False,
            "elasticsearch_database_done": False
        }
        pending.append(file_info)

    return pending


def clean_and_reorganize_text(text: str, title_max_length: int = 30) -> str:
    """
    清洗并重新组织矢量提取文本，使其更符合原文排版。
    """
    lines = text.split('\n')
    cleaned_lines = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if len(line) < title_max_length and not re.search(r'\p{P}+$', line):
            cleaned_lines.append(line)
            i += 1
            continue

        current_line = line
        while i + 1 < len(lines):
            next_line = lines[i + 1].strip()
            if not next_line:
                i += 1
                continue
            # 如果当前行不以句号结尾，并且下一行没有以空格/制表符开始，则合并到同一行
            if not re.search(r'[。]$', current_line) and not next_line.startswith((' ', '\t')):
                current_line += next_line
                i += 1
            else:
                break
        cleaned_lines.append(current_line)
        i += 1

    return '\n'.join(cleaned_lines)


def get_cropped_bbox(pdf_page, top_ratio=0.08, bottom_ratio=0.08):
    """
    返回裁剪掉页眉和页脚的区域，用于矢量文字提取。
    """
    parent_bbox = pdf_page.bbox
    px0, py0, px1, py1 = parent_bbox
    width = px1 - px0
    height = py1 - py0
    top = py0 + height * top_ratio
    bottom = py0 + height * (1 - bottom_ratio)
    return (px0, top, px1, bottom)


def extract_text_from_vector_page(pdf_page) -> str:
    """
    提取矢量页中的文字，并进行简单清洗。
    """
    bbox = get_cropped_bbox(pdf_page)
    cropped_page = pdf_page.within_bbox(bbox)
    text = cropped_page.extract_text() or ""
    return clean_and_reorganize_text(text)


def is_header_or_footer(text: str) -> bool:
    """
    判断文本是否仅是页眉/页脚（如只有页码等）。
    """
    return re.match(r'^\d+$', text.strip()) is not None


def convert_scanned_page_to_image(pdf_path: str, page_number: int, dpi: int, temp_img_dir: str) -> None:
    """
    扫描页只需转为图片存储到 temp_img_dir 文件夹。
    使用 dpi=300，以加快处理并减少磁盘占用。
    在转换前会先检查目标图片是否已存在，若存在则跳过转换。
    """
    image_path = os.path.join(temp_img_dir, f"page_{page_number}.png")
    if os.path.exists(image_path):
        print(f"[转换图片] 第 {page_number} 页图片已存在，跳过转换。")
        return
    print(f"[转换图片] 开始处理PDF第 {page_number} 页...")
    pdf_document = fitz.open(pdf_path)
    page = pdf_document.load_page(page_number - 1)
    pix = page.get_pixmap(dpi=dpi)
    pix.save(image_path)
    pdf_document.close()


# ── 陷阱94修复(2026-10-05 方案A): 扫描页渲染后接 OCR 路由 ──────────────────
# 每文档初始化一次: 按该文档扫描页总数决定全局后端(>20页大活走API, ≤20页本地MPS)。
# backend 决策与 OCRBackend.resolve_for_files 的 2026-09-26 用户裁定一致。
# 环境变量 STEP2_OCR_BACKEND=off 可整体关闭本功能(回退旧行为, 只渲染不提取)。
_OCR_STATE = {"backend": None, "router": None}


def _init_ocr_for_doc(pdf_folder_dir: str, scanned_pages: int) -> None:
    """文档级初始化: 决定 backend 并预热 router(local 模型懒加载在首次调用)。"""
    if os.environ.get("STEP2_OCR_BACKEND") == "off":
        _OCR_STATE["backend"] = "off"
        return
    try:
        from ocr_router import OCRRouter
    except ImportError:
        _OCR_STATE["backend"] = "off"
        print("[OCR路由] ocr_router 不可用, 关闭 OCR 提取(陷阱94旧行为)")
        return
    forced = os.environ.get("STEP2_OCR_BACKEND")
    if forced in ("local", "api"):
        _OCR_STATE["backend"] = forced
    else:
        _OCR_STATE["backend"] = "api" if scanned_pages > 20 else "local"
    _OCR_STATE["router"] = OCRRouter(mode="auto")
    print(f"[OCR路由] 文档扫描页 {scanned_pages} → backend={_OCR_STATE['backend']}"
          f" (决策写 {os.path.join(pdf_folder_dir, 'ocr_route.txt')})")
    try:
        with open(os.path.join(pdf_folder_dir, "ocr_route.txt"), "w", encoding="utf-8") as f:
            f.write(f"backend={_OCR_STATE['backend']}\nscanned_pages={scanned_pages}\ndecided_at={datetime.datetime.now().isoformat()}\n")
    except Exception:
        pass


def _ocr_rendered_page(image_path: str, pdf_folder_dir: str, scanned_pages_total: int) -> str:
    """对渲染好的扫描页图片执行 OCR, 返回文本(失败返回空串)。"""
    if _OCR_STATE.get("backend") is None:
        _init_ocr_for_doc(pdf_folder_dir, scanned_pages_total)
    backend = _OCR_STATE.get("backend")
    if backend in (None, "off"):
        return ""
    router = _OCR_STATE.get("router")
    if router is None:
        return ""
    for attempt in range(3):
        try:
            return router.ocr(image_path, page_type="scanned", force_backend=backend) or ""
        except Exception as e:
            print(f"[OCR重试] 第{attempt + 1}次失败: {e}")
            import time as _t
            _t.sleep(2 * (attempt + 1))
    print(f"[OCR失败] 3次重试后放弃: {image_path}")
    return ""


def load_pdf_detect_routing(pdf_folder_dir: str, total_pages: int):
    """
    读取 meta.json 中 step0_detect_pdf_type.py 写入的 pdf_type 路由信息。

    返回 (direct_render_pages, detect_type)：
    - direct_render_pages: 1索引物理页码集合（仅 meta.json pages_needing_ocr
      原集合，不含 mixed 保守扩边），应直接渲染为图片；
    - detect_type: pdf_type 字符串；
    - 若 meta.json 无 pdf_type / 为 detect_error / pages_needing_ocr 非 list，
      返回 (None, None) → 调用方走现状逻辑（向后兼容存量文件）。

    两档路由逻辑（mixed 扩边页设计缺陷修复，对应 508077 第 8/10 页丢失原生文本）：
    1. direct_render_pages 只渲染真正判定为扫描的页（pages_needing_ocr）。
       这些页原生文本层缺失，渲染后由后续 OCR 兜底，文本质量不受影响；
    2. mixed 扩边页（扫描页 p±1）**不** 强制渲染：它们通常有完整矢量文本层，
       走旧逻辑的矢量提取能拿到原生文本（质量优于 OCR）；旧逻辑在矢量提取失败
       （乱码/汉字过少）时自动回退渲染，天然覆盖半扫描边界页，作为扩边安全网保留。
    """
    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    if not os.path.exists(meta_path):
        return None, None
    try:
        meta = safe_json_load(meta_path)
    except Exception:
        return None, None
    if not isinstance(meta, dict):
        return None, None

    detect_type = meta.get("pdf_type")
    pno = meta.get("pages_needing_ocr")
    if not detect_type or detect_type == "detect_error" or not isinstance(pno, list):
        return None, None

    direct_render_pages = set()
    for p in pno:
        try:
            p = int(p)
        except (TypeError, ValueError):
            continue
        if 1 <= p <= total_pages:
            direct_render_pages.add(p)

    return direct_render_pages, detect_type


def process_single_file(args):
    """
    子进程处理函数：
    负责处理传入的单个 file_info 所对应的 PDF。

    注意：
    - 每个进程只处理一个 PDF，因此可以安全地进行 PDF 与相应 JSON 的读写。
    - 处理结束后返回更新过的 file_info，用于标记 text_extracted=True。
    - 如果遇到异常则会写入日志并跳过该文件。
    """
    file_info, log_file_name = args
    pdf_path = os.path.join(PDF_DIR, file_info["file_name"])
    file_name = file_info["file_name"]

    # 输出目录
    fund_folder_dir = os.path.join(OUTPUT_DIR, file_info["fund_code"])
    os.makedirs(fund_folder_dir, exist_ok=True)
    pdf_folder_name = os.path.splitext(file_name)[0]
    pdf_folder_dir = os.path.join(fund_folder_dir, pdf_folder_name)
    os.makedirs(pdf_folder_dir, exist_ok=True)
    # 统一使用简短文件名，避免超长路径/文件名导致报错
    output_json_file = os.path.join(pdf_folder_dir, "text.json")

    if os.path.exists(output_json_file):
        with open(output_json_file, 'r', encoding='utf-8') as f:
            existing_data = json.load(f)
        file_pages_dict = existing_data.get("pages", {})
    else:
        file_pages_dict = {}
    final_data = {"pages": file_pages_dict}  # 陷阱91防线: 预初始化, 防0页循环后未绑定
    # 用于存放扫描页转换后的图片
    temp_img_dir = os.path.join(pdf_folder_dir, "temp_pdf_images")
    os.makedirs(temp_img_dir, exist_ok=True)
    # 表格图片目录（如果有）
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            print(f"[PDF处理] 文件 {pdf_path} 共 {total_pages} 页")

            # pdf-inspector 前置路由（step0_detect_pdf_type.py 写入的 pdf_type）
            direct_render_pages, detect_type = load_pdf_detect_routing(pdf_folder_dir, total_pages)
            if detect_type is not None:
                print(f"[路由] pdf_type={detect_type}, 直接渲染页: {sorted(direct_render_pages) if direct_render_pages else []}")

            for i in range(total_pages):
                page_number = i + 1
                if str(page_number) in file_pages_dict:
                    print(f"[页面跳过] 第 {page_number} 页已处理，跳过。")
                    continue
                print(f"[页面处理] 开始处理第 {page_number} 页")
                page = pdf.pages[i]

                # 路由命中：仅 pages_needing_ocr 原集合直接渲染，跳过矢量提取尝试。
                # mixed 扩边页不在此集合（通常有完整矢量文本层，走下方旧逻辑拿到原生文本，
                # 质量优于 OCR），但旧逻辑在矢量提取失败时自动回退渲染，天然覆盖半扫描边界页。
                if direct_render_pages is not None and page_number in direct_render_pages:
                    print(f"[路由标记] 第 {page_number} 页 命中 pdf-inspector OCR 页, 直接渲染(跳过矢量提取)")
                    convert_scanned_page_to_image(pdf_path, page_number, 300, temp_img_dir)
                    # ── 陷阱94修复(2026-10-05 用户批准方案A): 渲染后接 OCR 路由 ──
                    # 全扫描文档此前只渲染不提取, text.json 0页 → 陷阱91防线拒绝 → 死循环。
                    # 现在渲染完的页立即走 ocr_router 提取文本写入 pages。
                    # 路由规则(resolve_for_files, 2026-09-26 用户裁定): >20页大活走API, 小活本地MPS。
                    ocr_text = _ocr_rendered_page(
                        os.path.join(temp_img_dir, f"page_{page_number}.png"),
                        pdf_folder_dir, len(direct_render_pages))
                    if ocr_text and len(ocr_text.strip()) >= 10:
                        page_metadata = file_info.copy()
                        page_metadata.update({
                            "source_file": file_info["file_name"],
                            "page_num": page_number,
                            "ocr_backend": _OCR_STATE.get("backend", "unknown"),
                        })
                        file_pages_dict[str(page_number)] = {
                            "text": ocr_text.strip(),
                            "metadata": page_metadata,
                        }
                        final_data = {"pages": file_pages_dict, "metadata": file_info}
                        with open(output_json_file, 'w', encoding='utf-8') as f:
                            json.dump(final_data, f, ensure_ascii=False, indent=4, cls=DateTimeEncoder)
                        print(f"[页面保存-OCR] 第 {page_number} 页 OCR文本 {len(ocr_text)} 字符 -> text.json")
                    else:
                        print(f"[OCR空返回] 第 {page_number} 页 OCR 未取到文本, 仅保留渲染图")
                    continue

                # 矢量文本提取
                vector_text = extract_text_from_vector_page(page)

                # —— 新增：乱码检测 —— #
                # 如果包含 (cid:数字)，视为乱码
                is_cid_garbled = bool(re.search(r'\(cid:\d+\)', vector_text))
                # 计算汉字数量
                han_count = len(re.findall(r'[\u4e00-\u9fff]', vector_text))
                is_low_chinese = han_count < PAGE_CHAR_THRESHOLD

                # 决策：既不是乱码也有足够汉字，才当做矢量页
                if (not is_cid_garbled
                        and not is_low_chinese
                        and len(vector_text) >= PAGE_CHAR_THRESHOLD):

                    current_text = vector_text
                    print(f"[矢量提取成功] 第 {page_number} 页, 文本长度: {len(vector_text)}, 汉字数: {han_count}")

                    if is_header_or_footer(current_text.strip()):
                        print(f"[忽略页眉页脚] 第 {page_number} 页")
                        current_text = ""

                    page_metadata = file_info.copy()
                    page_metadata.update({
                        "source_file": file_info["file_name"],
                        "page_num": page_number
                    })
                    
                    file_pages_dict[str(page_number)] = {
                        "text": current_text,
                        "metadata": page_metadata
                    }

                    final_data = {"pages": file_pages_dict, "metadata": file_info}
                    with open(output_json_file, 'w', encoding='utf-8') as f:
                        json.dump(final_data, f, ensure_ascii=False, indent=4, cls=DateTimeEncoder)
                    print(f"[页面保存] 第 {page_number} 页内容已保存 -> {output_json_file}")

                else:
                    # 当作扫描页：包括文字提取不足或乱码情况
                    print(f"[矢量提取不足或乱码] 第 {page_number} 页, 长度 {len(vector_text)}, 汉字数 {han_count}")
                    convert_scanned_page_to_image(pdf_path, page_number, 300, temp_img_dir)

    except Exception as e:
        print(f"[错误] 文件 {pdf_path} 处理失败: {e}")
        with open(log_file_name, "a", encoding="utf-8") as log_f:
            log_f.write(f"文件 {pdf_path} 处理失败\n原因: {e}\n\n")
        return file_info  # 不改 text_extracted

    # ── 陷阱91根治(2026-10-04): 置 True 前自检内容物 ──
    # 历史上此处中断只写标志不写内容 → text_extracted=True 但 text.json 0页,
    # 后续全链静默跳过(僵尸文档)。现在: pages 为空/文本总量<50 → 拒绝置 True,
    # 大声报错, 让文档留在待处理队列。
    final_data = final_data if (final_data is not None and isinstance(final_data, dict)) else {"pages": file_pages_dict}
    _pages = final_data.get("pages")
    if _pages is None and os.path.exists(output_json_file):
        try:
            with open(output_json_file, 'r', encoding='utf-8') as f:
                _latest = json.load(f)
            _pages = _latest.get("pages")
        except Exception:
            _pages = None
    _pv = _pages.values() if isinstance(_pages, dict) else (_pages or [])
    _n_pages = len(_pages) if isinstance(_pages, dict) else (len(_pages) if _pages else 0)
    _total_chars = sum(len(v.get("text") or "") for v in _pv if isinstance(v, dict))
    if _n_pages == 0 or _total_chars < 50:
        print(f"[拒绝置位] {file_name}: text.json 页数 {_n_pages}, 总字数 {_total_chars} — "
              f"内容物为空, 保留 text_extracted=False (陷阱91防线)")
        with open(log_file_name, "a", encoding="utf-8") as log_f:
            log_f.write(f"文件 {file_name}\n拒绝置位 text_extracted: 页数{_n_pages}/字数{_total_chars} (陷阱91防线)\n\n")
        return file_info  # 不置 True

    file_info["text_extracted"] = True

    try:
        if os.path.exists(output_json_file):
            with open(output_json_file, 'r', encoding='utf-8') as f:
                latest = json.load(f)
        else:
            latest = {"pages": file_pages_dict, "metadata": file_info}
        latest_meta = latest.get("metadata", {}) or {}
        latest_meta.update({"text_extracted": True})
        latest["metadata"] = latest_meta
        with open(output_json_file, 'w', encoding='utf-8') as f:
            json.dump(latest, f, ensure_ascii=False, indent=4, cls=DateTimeEncoder)
    except Exception as e:
        print(f"[警告] 写回 text.json 元数据失败: {file_name}, 原因: {e}")

    return file_info


def main():
    log_file_name = os.path.join(SCRIPT_DIR, "extract_text_onlyvactor_log.txt")

    manifest = load_manifest()
    # [BATCH-FIX] manifest reconcile removed — rebuilt by rebuild_manifest.py after step completes

    # 从本地目录扫描待处理文件
    try:
        pending_files = get_pending_files_from_local()
    except Exception as e:
        print(f"[错误] 扫描本地PDF目录失败: {e}")
        logger.warning(f"扫描本地PDF目录失败: {e}")
        return

    tasks = []
    for file_info in pending_files:
        tasks.append((file_info, log_file_name))

    total_to_process = len(tasks)
    if total_to_process == 0:
        print("没有文件需要处理。")
        logger.warning("没有文件需要处理。")
        return

    success_count = 0
    fail_list = []

    with concurrent.futures.ProcessPoolExecutor(max_workers=3) as executor:
        future_map = {executor.submit(process_single_file, t): t for t in tasks}
        for future in concurrent.futures.as_completed(future_map):
            file_info, _ = future_map[future]
            try:
                updated_info = future.result()
                if updated_info.get("text_extracted") is True:
                    success_count += 1
                    try:
                        file_name = updated_info.get("file_name") or file_info.get("file_name")
                        fund_code = updated_info.get("fund_code") or file_info.get("fund_code")
                        if file_name and fund_code:
                            pdf_folder_name = os.path.splitext(file_name)[0]
                            # [BATCH-FIX] manifest update removed — rebuilt by rebuild_manifest.py
                            _update_meta_json_status(fund_code, pdf_folder_name, {"text_extracted": True})
                    except Exception:
                        pass
                else:
                    fail_list.append((file_info["file_name"], "未设置 text_extracted"))
            except Exception as e:
                fail_list.append((file_info["file_name"], str(e)))

    remain = total_to_process - success_count
    print(f"[完成] 所有文件处理结束, 成功: {success_count}, 失败: {remain}.")
    logger.warning(f"[完成] 所有文件处理结束。")
    logger.warning(f"本次处理文件数: {total_to_process}, 成功: {success_count}, 失败: {remain}")
    if fail_list:
        logger.warning("失败列表:")
        for fname, reason in fail_list:
            logger.warning(f"  文件: {fname}, 原因: {reason}")
    # [BATCH-FIX] manifest save removed — rebuilt by rebuild_manifest.py after step completes


if __name__ == "__main__":
    main()

# 强制刷新日志
import logging
logging.shutdown()
