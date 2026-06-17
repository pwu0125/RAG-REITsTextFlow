import argparse
import os
import sys
from typing import List, Tuple


def _ask_yes_no(prompt: str) -> bool:
    while True:
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no", ""):
            return False
        print("请输入 y 或 n（直接回车等同于 n）。")


def _verify_png(path: str) -> Tuple[bool, str]:
    try:
        from PIL import Image  # type: ignore
    except Exception as e:
        return False, f"缺少 Pillow 依赖，无法校验图片：{e}"

    try:
        with Image.open(path) as im:
            im.verify()
        return True, ""
    except Exception as e:
        return False, str(e)


def _iter_temp_image_dirs(root: str):
    for dirpath, _, _ in os.walk(root):
        if os.path.basename(dirpath) == "temp_pdf_images":
            yield dirpath


def scan(root: str, limit: int) -> List[Tuple[str, str]]:
    bad: List[Tuple[str, str]] = []
    for temp_dir in _iter_temp_image_dirs(root):
        try:
            filenames = sorted(os.listdir(temp_dir))
        except Exception:
            continue

        for fn in filenames:
            if not fn.lower().endswith(".png"):
                continue
            path = os.path.join(temp_dir, fn)
            ok, err = _verify_png(path)
            if ok:
                continue
            bad.append((path, err))
            if limit > 0 and len(bad) >= limit:
                return bad
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="scan_bad_images.py",
        description="扫描 announcement_document_processing_local 下的 temp_pdf_images，找出损坏/不可识别的 PNG。",
    )
    parser.add_argument(
        "--root",
        default="/Users/pyemini/projects/REITs/RAG-REITsTextFlow-main/announcement_document_processing_local",
        help="扫描根目录（默认是本地处理输出目录）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多输出多少条坏图（0 表示不限制）",
    )
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.exists(root):
        print(f"[错误] 目录不存在：{root}")
        return 2

    bad = scan(root, args.limit)

    if bad and bad[0][1].startswith("缺少 Pillow 依赖"):
        print(bad[0][1])
        print("请先安装：python -m pip install -U pillow")
        return 3

    print(f"扫描目录：{root}")
    print(f"坏图总数：{len(bad)}")
    for path, err in bad:
        print(path)
        print(f"  -> {err}")

    if bad:
        print("\n是否删除以上坏图？每张图会单独确认一次。")
        deleted = 0
        kept = 0
        for path, err in bad:
            ok = _ask_yes_no(f"删除坏图？y/n  {path} ")
            if not ok:
                kept += 1
                continue
            try:
                os.remove(path)
                deleted += 1
                print(f"已删除：{path}")
            except Exception as e:
                kept += 1
                print(f"删除失败，已保留：{path} -> {e}")
        print(f"\n删除完成：已删除 {deleted} 张，保留 {kept} 张。")

    return 0 if len(bad) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
