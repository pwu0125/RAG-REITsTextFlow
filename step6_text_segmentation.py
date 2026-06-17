#!/usr/bin/env python
# -*- coding: utf-8 -*-
# 文本切分，结果保存至 mysql 表 text_segmentation_embedding 里（和 {pdf_folder_name}_text_segmentation.json 里）
"""
step6_text_segmentation.py

新增需求:
1) 对于页码字符串包含非纯数字(如 "3(1)")的页面, 直接跳过不纳入交集.
2) 在写入数据库前, 先根据 source_file 删除旧记录, 然后插入新的所有文本块;
   若插入出现错误, 整体回滚, 不更新 text_segmentation 状态.
3) 本步骤在更新数据库 announcement 中 processed_files 表相应记录的 text_segmentation 字段为 "true"，
   并且仅更新该字段，其它字段保持不变。为保证信息全部写入，必须先更新数据库成功，再保存JSON文件。
"""

import os
import re
import json
import time
import logging
import threading

import file_paths_config
from common_utils import safe_json_dump, safe_json_load

# ================== 日志配置 ===================
# 获取脚本所在目录，确保日志文件生成在log目录下
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(SCRIPT_DIR, "log")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILENAME = os.path.join(LOG_DIR, 'text_segmentation.log')
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)
file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
file_handler.setLevel(logging.WARNING)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)
# ================== 常量参数 ===================
MULTIFILE_OUTPUT_DIR = file_paths_config.OUTPUT_DIR
MANIFEST_FILE = os.path.join(MULTIFILE_OUTPUT_DIR, "processed_files_local.json")
json_lock = threading.Lock()

MIN_LEN = 200       # 普通文本块最少字符数
MAX_LEN = 1500      # 普通文本块最多字符数(强制切分)
TABLE_START = "## 表格主题"
TABLE_END = "表格内容描述完毕。"


def _safe_read_json(path):
    try:
        if os.path.exists(path):
            return safe_json_load(path)
    except Exception:
        return None
    return None


def _get_pdf_folder_dir(file_name: str, fund_code: str) -> str:
    pdf_folder_name = os.path.splitext(file_name)[0]
    return os.path.join(MULTIFILE_OUTPUT_DIR, fund_code, pdf_folder_name)


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
    manifest = _safe_read_json(MANIFEST_FILE) or {}
    files_map = manifest.get("files", {}) or {}
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
        if status.get("merge_done") is not True:
            continue
        if status.get("text_segmentation") is True:
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
            "merge_done": True,
            "text_segmentation": False,
        }
        grouped_files.setdefault(fund_code, []).append(row)

    for fc in grouped_files:
        grouped_files[fc].sort(key=lambda x: x.get("file_name", ""))

    return grouped_files


def update_local_text_segmentation_done(file_info):
    file_name = file_info.get("file_name", "")
    fund_code = file_info.get("fund_code", "")
    if not file_name or not fund_code:
        return False

    pdf_folder_dir = _get_pdf_folder_dir(file_name, fund_code)
    os.makedirs(pdf_folder_dir, exist_ok=True)

    meta_path = os.path.join(pdf_folder_dir, "meta.json")
    meta = _safe_read_json(meta_path) or {}
    meta.update(file_info)
    meta["text_segmentation"] = True
    safe_json_dump(meta, meta_path)

    text_path = os.path.join(pdf_folder_dir, "text.json")
    text_json = _safe_read_json(text_path)
    if isinstance(text_json, dict):
        text_meta = text_json.get("metadata", {}) or {}
        text_meta["text_segmentation"] = True
        text_json["metadata"] = text_meta
        safe_json_dump(text_json, text_path)

    manifest = _safe_read_json(MANIFEST_FILE) or {"files": {}}
    if "files" not in manifest or not isinstance(manifest["files"], dict):
        manifest["files"] = {}
    entry = manifest["files"].get(file_name, {}) or {}
    entry.update(file_info)
    entry["text_segmentation"] = True
    manifest["files"][file_name] = entry
    safe_json_dump(manifest, MANIFEST_FILE)
    return True


def get_pending_files_from_db():
    return get_pending_files_from_local()

def load_json_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json_file(data, path):
    safe_json_dump(data, path)

def sort_page_keys(pages_dict):
    """
    将 pages 按数字顺序排序。键可能为 "8", "9-10", "11-12-13", 
    这里按第一个数字从小到大排。
    遇到无法解析为数字的情况, 排到最后.
    """
    def extract_first_num(k):
        nums = re.findall(r'\d+', k)
        if not nums:
            return None
        try:
            return int(nums[0])
        except:
            return None

    def sort_key(k):
        val = extract_first_num(k)
        if val is None:
            return 999999999
        else:
            return val

    sorted_keys = sorted(pages_dict.keys(), key=sort_key)
    return sorted_keys

def merge_pages_text(pages_dict):
    """
    将 pages 按顺序合并为一个完整文本，并记录每页在全文的起止位置。
    返回 (full_text, page_map)
    """
    merged_text = ""
    page_map = []
    keys = sort_page_keys(pages_dict)
    for k in keys:
        content = pages_dict[k].get("text", "")
        start = len(merged_text)
        merged_text += content
        end = len(merged_text)
        page_map.append({
            "page_range": k,
            "start": start,
            "end": end
        })
    return merged_text, page_map

def cut_normal_text(chunk_str, base_idx, min_len, max_len):
    """
    普通文本切分:
      - 累积>= min_len(200), 在(\n或\n\n)处切分
      - 超过 max_len(1500) 强制切
    """
    segments = []
    i = 0
    length = len(chunk_str)
    linebreak_pat = re.compile(r'\n\n|\n')

    while i < length:
        remain = length - i
        if remain < min_len:
            seg_text = chunk_str[i:]
            s_pos = base_idx + i
            e_pos = s_pos + len(seg_text)
            segments.append({"text": seg_text, "start": s_pos, "end": e_pos})
            i = length
            break
        
        seg_start = i + min_len
        if seg_start > length:
            seg_start = length
        forced_end = i + max_len
        if forced_end > length:
            forced_end = length

        sub_region = chunk_str[seg_start:forced_end]
        match = linebreak_pat.search(sub_region)
        if match:
            lb_idx = seg_start + match.start()
            cut_pos = lb_idx
        else:
            cut_pos = forced_end

        seg_text = chunk_str[i:cut_pos]
        s_pos = base_idx + i
        e_pos = s_pos + len(seg_text)
        segments.append({"text": seg_text, "start": s_pos, "end": e_pos})
        i = cut_pos

    return segments

def split_table_block(table_str, base_idx, max_len=1500):
    """
    表格块也受1500字符限制, 无最小长度, 不管换行.
    """
    parts = []
    i = 0
    length = len(table_str)
    while i < length:
        remain = length - i
        if remain <= max_len:
            seg_text = table_str[i:]
            s_pos = base_idx + i
            e_pos = s_pos + len(seg_text)
            parts.append({
                "text": seg_text,
                "start": s_pos,
                "end": e_pos
            })
            i = length
        else:
            seg_text = table_str[i:i+max_len]
            s_pos = base_idx + i
            e_pos = s_pos + len(seg_text)
            parts.append({
                "text": seg_text,
                "start": s_pos,
                "end": e_pos
            })
            i += max_len
    return parts

def segment_text_simplified(full_text):
    """
    1) 遇到 "## 表格主题", 先切分普通文本
    2) 表格段 "## 表格主题"~"表格内容描述完毕。" 超过1500也分块
    3) 剩余都是普通文本 200~1500 + 换行
    """
    results = []
    idx = 0
    length = len(full_text)

    while idx < length:
        tstart = full_text.find(TABLE_START, idx)
        if tstart == -1:
            leftover = full_text[idx:]
            segs = cut_normal_text(leftover, idx, MIN_LEN, MAX_LEN)
            results.extend(segs)
            idx = length
        else:
            if tstart > idx:
                normal_part = full_text[idx:tstart]
                normal_segs = cut_normal_text(normal_part, idx, MIN_LEN, MAX_LEN)
                results.extend(normal_segs)
                idx = tstart
            
            tend = full_text.find(TABLE_END, idx)
            if tend == -1:
                table_str = full_text[idx:]
                tseg = split_table_block(table_str, idx, MAX_LEN)
                results.extend(tseg)
                idx = length
            else:
                table_end_idx = tend + len(TABLE_END)
                table_str = full_text[idx: table_end_idx]
                tseg = split_table_block(table_str, idx, MAX_LEN)
                results.extend(tseg)
                idx = table_end_idx

    return results

def safe_parse_ints_from_range(page_range_str):
    """
    从 page_range_str 里提取纯数字, 若含非纯数字(如 3(1)), 则跳过(返回空列表).
    若可以成功提取, 返回转换好的 int 列表.
    """
    # 如果字符串含有除数字或 '-' 以外的其他字符(如( )), 直接认为无效
    # 也可逐段检查, 看您需求
    # 这里示例: 只要检测到括号, 就放弃
    if '(' in page_range_str or ')' in page_range_str:
        return []# 直接跳过

    # 否则常规split
    all_nums = []
    for part in page_range_str.split('-'):
        part = part.strip()
        if not part.isdigit():
            # 遇到非纯数字 => 放弃
            return []
        all_nums.append(int(part))
    return all_nums

def get_metadata_for_segment(segment_start, segment_end, page_map, pages_dict):
    """
    根据片段 [segment_start, segment_end), 找到与 page_map 有交集的页面range,
    但如果该range含非纯数字(如 "3(1)"), 则跳过该range.
    """
    overlapping_pages = []
    for mapping in page_map:
        if mapping["end"] > segment_start and mapping["start"] < segment_end:
            overlapping_pages.append(mapping["page_range"])

    if not overlapping_pages:
        return {}
    # 过滤掉含非法数字的range
    valid_ranges = []
    for rng in overlapping_pages:
        nums = safe_parse_ints_from_range(rng)
        if nums: # 若为空说明不合法
            valid_ranges.append(rng)

    if not valid_ranges:
        return {} # 全部跳过

    # 继续合并 valid_ranges
    all_nums = []
    for rng in valid_ranges:
        page_nums = safe_parse_ints_from_range(rng)
        all_nums.extend(page_nums)
    unique_pages = sorted(set(all_nums))
    page_range_str = '-'.join(map(str, unique_pages)) if unique_pages else ""
    # 以第一个 valid_ranges[0] 的 metadata 为基准
    # (按页面顺序再排, 取最小数字优先)
    def first_num(r):
        p = safe_parse_ints_from_range(r)
        return p[0] if p else 9999999
    valid_ranges.sort(key=first_num)
    first_key = valid_ranges[0]
    meta = pages_dict.get(first_key, {}).get("metadata", {}).copy()
    meta["page_num"] = page_range_str
    return meta

def insert_segmentation_to_db(chunks):
    return

def process_pdf_segmentation(pdf_folder):
    """
    针对单个 PDF:
      1) 读取 json
      2) 合并文本
      3) 调用 segment_text_simplified
      4) 构建 chunks + metadata
      5) 前后关联
      6) 保存+插入 DB(若插入失败则回滚)
    """
    pdf_folder_name = os.path.basename(pdf_folder)
    text_json_path = os.path.join(pdf_folder, "text.json")
    if not os.path.exists(text_json_path):
        print(f"提取文本文件不存在: {text_json_path}")
        return False

    text_data = load_json_file(text_json_path)
    pages = text_data.get("pages", {})
    if not pages:
        print(f"{text_json_path} 中没有页面数据。")
        return False

    full_text, page_map = merge_pages_text(pages)
    raw_segments = segment_text_simplified(full_text)

    global_meta = text_data.get("metadata", {})
    final_chunks = []
    for idx, seg in enumerate(raw_segments, 1):
        start_ = seg["start"]
        end_ = seg["end"]
        page_meta = get_metadata_for_segment(start_, end_, page_map, pages)
        combined_meta = global_meta.copy()
        combined_meta.update(page_meta)
        char_cnt = len(seg["text"])
        combined_meta["char_count"] = char_cnt
        source_file = combined_meta.get("source_file", "")
        base_name = os.path.splitext(source_file)[0] if source_file else ""
        global_id = f"{base_name}_{idx}" if base_name else f"_{idx}"
        final_chunks.append({
            "chunk_id": idx,
            "global_id": global_id,
            "text": seg["text"],
            "metadata": combined_meta
        })

    n = len(final_chunks)
    for i in range(n):
        ck = final_chunks[i]
        if i == 0:
            ck["metadata"]["prev_chunks"] = []
        else:
            prev_chunk = final_chunks[i-1]
            pids = []
            if len(prev_chunk["text"]) > 100:
                pids.append(prev_chunk["global_id"])
            else:
                if i-2 >= 0:
                    pids.append(final_chunks[i-2]["global_id"])
                pids.append(prev_chunk["global_id"])
            ck["metadata"]["prev_chunks"] = pids

        if i == n-1:
            ck["metadata"]["next_chunks"] = []
        else:
            next_chunk = final_chunks[i+1]
            nids = []
            if len(next_chunk["text"]) > 100:
                nids.append(next_chunk["global_id"])
            else:
                if i+2 < n:
                    nids.append(final_chunks[i+2]["global_id"])
                nids.append(next_chunk["global_id"])
            ck["metadata"]["next_chunks"] = nids

    output_path = os.path.join(pdf_folder, "text_segmentation.json")
    save_json_file(final_chunks, output_path)
    print(f"已保存切分结果到: {output_path}")

    return True

def main():
    processed_files = get_pending_files_from_local()
    if not processed_files:
        print("没有找到需要处理的文件。")
        logger.warning("没有找到需要处理的文件。")
        return

    files_to_process = []
    
    # 遍历数据库查询结果，构建待处理列表
    for fund_code, pdf_list in processed_files.items():
        for pdf_info in pdf_list:
            files_to_process.append((fund_code, pdf_info))

    total_count = len(files_to_process)
    if total_count == 0:
        print("没有找到需要处理的文件。")
        logger.warning("没有找到需要处理的文件。")
        return

    processed_count = 0
    failed_files = []

    for fund_code, pdf_info in files_to_process:
        file_name = pdf_info.get("file_name", "")
        pdf_folder_name = os.path.splitext(file_name)[0]
        pdf_folder = os.path.join(MULTIFILE_OUTPUT_DIR, fund_code, pdf_folder_name)
        if not os.path.exists(pdf_folder):
            print(f"未找到PDF文件夹, 跳过 {file_name}: {pdf_folder}")
            logger.warning(f"未找到PDF文件夹, 跳过 {file_name}: {pdf_folder}")
            failed_files.append(file_name)
            continue

        print(f"开始处理 {file_name} 的数据切分...")

        success = False
        try:
            success = process_pdf_segmentation(pdf_folder)
        except Exception as e:
            logger.warning(f"文件 {file_name} 处理文本切分时出现异常: {e}")
            success = False

        if success:
            with json_lock:
                ok = update_local_text_segmentation_done(pdf_info)
            if ok:
                print(f"已完成 {file_name} 的文本切分处理。")
                processed_count += 1
            else:
                print(f"文件 {file_name} 更新本地状态 text_segmentation 失败。")
                logger.warning(f"文件 {file_name} 更新本地状态 text_segmentation 失败。")
                failed_files.append(file_name)
        else:
            print(f"{file_name} 处理文本切分失败。")
            failed_files.append(file_name)

    remain = total_count - processed_count
    if processed_count > 0 and remain == 0:
        print("文本切分全部完成！")
        logger.warning("文本切分全部完成！")
    else:
        if processed_count == 0:
            print("所有文件文本切分均失败或跳过。")
            logger.warning("所有文件文本切分均失败或跳过。")
        else:
            print(f"有 {remain} 个文件未完成文本切分。")
            logger.warning(f"有 {remain} 个文件未完成文本切分。")

    if failed_files:
        logger.warning(f"以下文件未能完成文本切分：{failed_files}")

if __name__ == "__main__":
    main()

# 强制刷新日志，放在脚本最后一行
import logging
logging.shutdown()