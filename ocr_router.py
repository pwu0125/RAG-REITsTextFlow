#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OCR 智能路由器 — 根据页面类型和批处理模式选择本地 DeepSeek OCR 或阿里云 API。

决策逻辑：
  矢量文字页 → 不进入 OCR（由 step2 pdfplumber 直接提取）
  扫描/图片页 → 按预估耗时路由
  表格页     → 同上，但使用表格专用 prompt

模式（优先级：环境变量 > CLI参数 > 默认值）：
  auto        — 自动判断：扫描页>50 或 预估>10min → api；否则 local
  local       — 强制本地 DeepSeek OCR 2 (MPS)
  api         — 强制阿里云 qwen-vl-ocr
  batch       — 批量建库模式（同 api）
  incremental — 增量更新模式（同 local）

使用方式:
  from ocr_router import OCRRouter
  router = OCRRouter(mode="auto")
  text = router.ocr(image_path, page_type="scanned", prompt="提取全部文字")

环境变量:
  OCR_BACKEND=local|api|auto    覆盖 mode 参数
  DEEPSEEK_OCR_MODEL_PATH       本地模型路径
  DASHSCOPE_API_KEY             阿里云 API Key
"""

import os
import sys
import time
import logging
from typing import Optional, Literal

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────
LOCAL_PER_PAGE_SEC = 30       # MPS DeepSeek OCR 2 实测 ~28s，留余量
API_PER_PAGE_SEC = 4          # 阿里云 qwen-vl-ocr 平均延迟
LOCAL_MAX_PAGES = 20          # 用户裁定(2026-09-26): 本地=补充辅助仅小活; >20页大活必走API
BATCH_THRESHOLD_PAGES = 21    # (历史常量保留) 与 LOCAL_MAX_PAGES 对齐为硬分界

# 默认模型路径
DEFAULT_MODEL_PATH = os.path.expanduser(
    "~/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-OCR-2/"
    "snapshots/aaa02f3811945a91062062994c5c4a3f4c0af2b0"
)

# ── 后端类型 ─────────────────────────────────────────
Backend = Literal["local", "api", "auto"]
PageType = Literal["vector", "scanned", "table"]


class OCRRouter:
    """OCR 智能路由器"""

    def __init__(
        self,
        mode: Backend = "auto",
        model_path: Optional[str] = None,
        dashscope_api_key: Optional[str] = None,
    ):
        # 环境变量覆盖
        self.mode: Backend = os.environ.get("OCR_BACKEND", mode)  # type: ignore
        self.model_path = model_path or os.environ.get(
            "DEEPSEEK_OCR_MODEL_PATH", DEFAULT_MODEL_PATH
        )
        self.api_key = dashscope_api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self._local_backend = None  # 懒加载
        # 陷阱95防线(2026-10-06): 本地推理内存超限自动降级 API 的阈值
        self.local_mem_limit_gb = self._rss_limit()

    _MEM_LIMIT_DEFAULT = 6.0  # GB, 环境变量 LOCAL_OCR_MEM_LIMIT_GB 可覆盖

    @classmethod
    def _rss_limit(cls) -> float:
        try:
            return float(os.environ.get("LOCAL_OCR_MEM_LIMIT_GB", cls._MEM_LIMIT_DEFAULT))
        except ValueError:
            return cls._MEM_LIMIT_DEFAULT

    @staticmethod
    def _rss_gb() -> float:
        """进程内存足迹(GB, top口径含MPS缓存)。psutil缺失时返回0(不降级,保守)。"""
        try:
            import subprocess
            out = subprocess.run(
                ["top", "-l", "1", "-pid", str(os.getpid()), "-stats", "mem"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            last = [l for l in out.splitlines() if l.strip()][-1]
            tok = last.split()[0]  # e.g. "27G" / "800M"
            if tok.endswith("G"):
                return float(tok[:-1])
            if tok.endswith("M"):
                return float(tok[:-1]) / 1024
            if tok.endswith("K"):
                return float(tok[:-1]) / (1024 * 1024)
            return 0.0
        except Exception:
            return 0.0

    # ── 公共接口 ──────────────────────────────────

    def ocr(
        self,
        image_path: str,
        page_type: PageType = "scanned",
        prompt: str = "提取本页全部文字内容。",
        force_backend: Optional[str] = None,
    ) -> str:
        """
        对单张图片执行 OCR，自动选择后端。

        Args:
            image_path: PNG/JPG 图片路径
            page_type: 页面类型（scanned | table）
            prompt: OCR 提示词
            force_backend: 强制指定后端（覆盖路由）

        Returns:
            提取的文字
        """
        backend = force_backend or self._resolve_backend(page_type)
        if backend == "local":
            # 本地模型缓存缺失时自动降级 API（2026-09-26 修复：缓存被清理导致 OSError）
            if not os.path.isdir(self.model_path):
                logger.warning(
                    f"[LocalOCR] 本地模型缺失: {self.model_path} → 降级 API"
                )
                return self._ocr_api(image_path, prompt)
            # 陷阱95防线(2026-10-06): 进程内存超限 → 剩余页降级 API, 不硬扛本地
            if self._rss_gb() > self.local_mem_limit_gb:
                logger.warning(
                    f"[LocalOCR] 进程内存 {self._rss_gb():.1f}G 超限 "
                    f"{self.local_mem_limit_gb}G → 本页降级 API"
                )
                return self._ocr_api(image_path, prompt)
            return self._ocr_local(image_path, prompt)
        else:
            return self._ocr_api(image_path, prompt)

    def estimate_batch(
        self, scanned_count: int, table_count: int
    ) -> dict:
        """
        预估批量处理时间和推荐模式。

        Returns:
            {
                "scanned": int, "table": int,
                "local_seconds": float, "api_seconds": float,
                "recommended": "local"|"api",
                "local_minutes": float, "api_minutes": float,
            }
        """
        local_s = scanned_count * LOCAL_PER_PAGE_SEC + table_count * (LOCAL_PER_PAGE_SEC + 5)
        api_s = (scanned_count + table_count) * API_PER_PAGE_SEC
        recommended = "api" if (scanned_count + table_count) >= BATCH_THRESHOLD_PAGES else "local"

        return {
            "scanned": scanned_count,
            "table": table_count,
            "local_seconds": local_s,
            "api_seconds": api_s,
            "local_minutes": round(local_s / 60, 1),
            "api_minutes": round(api_s / 60, 1),
            "recommended": recommended,
        }

    # ── 路由核心 ──────────────────────────────────

    def _resolve_backend(self, page_type: PageType) -> str:
        """根据模式和页面类型决定后端"""
        if page_type == "vector":
            return "local"  # 矢量页不进这里，兜底

        mode = self.mode
        if mode in ("api", "batch"):
            return "api"
        if mode in ("local", "incremental"):
            return "local"
        # auto: 由调用方传入预估页数，这里默认 local
        return "local"

    def resolve_for_files(
        self, scanned_count: int, table_count: int
    ) -> str:
        """批量场景下决定全局后端（2026-09-26 用户裁定：本地=补充辅助，仅小活；>20页大活必走API）

        本地 DeepSeek-OCR-2 (MPS) 密集行有丢字（实测基金会名漏字），定位为补充辅助方案；
        大批量(>20页)必须交给 API (qwen-vl-ocr)。
        """
        total = scanned_count + table_count
        if self.mode in ("api", "batch"):
            return "api"
        # local / incremental / auto 统一按页数硬规则路由：
        #   >20 页（大活）→ API
        #   ≤20 页（小活）→ 本地（模型缺失时由 ocr() 内部自动降级 API）
        if total > LOCAL_MAX_PAGES:
            logger.info(f"[Router] {total} pages > {LOCAL_MAX_PAGES} (大活) → API")
            return "api"
        logger.info(f"[Router] {total} pages ≤ {LOCAL_MAX_PAGES} (小活) → local")
        return "local"

    # ── 本地 OCR ─────────────────────────────────

    def _get_local_backend(self):
        """懒加载 DeepSeek OCR 2 后端"""
        if self._local_backend is not None:
            return self._local_backend

        # 插入 DeepSeek-OCR-WebUI 路径
        webui_path = os.path.expanduser("~/DeepSeek-OCR-WebUI")
        if webui_path not in sys.path:
            sys.path.insert(0, webui_path)

        from backends.mps_backend import MPSBackend

        backend = MPSBackend(model_path=self.model_path)
        backend.load_model()
        self._local_backend = backend
        return backend

    def _ocr_local(self, image_path: str, prompt: str) -> str:
        """本地 DeepSeek OCR 2 推理"""
        t0 = time.time()
        logger.info(f"[LocalOCR] Start: {os.path.basename(image_path)}")
        backend = self._get_local_backend()
        result = backend.infer(prompt=prompt, image_path=image_path)
        elapsed = time.time() - t0
        logger.info(f"[LocalOCR] Done: {elapsed:.1f}s, {len(result)} chars")
        return result if result else ""

    # ── 阿里云 API OCR ─────────────────────────────

    def _ocr_api(self, image_path: str, prompt: str) -> str:
        """阿里云 DashScope qwen-vl-ocr 推理"""
        t0 = time.time()
        logger.info(f"[ApiOCR] Start: {os.path.basename(image_path)}")

        try:
            from step4_table_utils_ali_multi_thread import get_model_config
            import dashscope
            import base64

            vendor = "ali"
            model_name = "qwen-vl-ocr"
            cfg = get_model_config(vendor, model_name)

            with open(image_path, "rb") as f:
                image_data = f.read()
            base64_image = base64.b64encode(image_data).decode("utf-8")

            messages = [{
                "role": "user",
                "content": [
                    {"image": f"data:image/png;base64,{base64_image}"},
                    {"text": prompt},
                ],
            }]

            # 最多重试 3 次
            for attempt in range(3):
                try:
                    response = dashscope.MultiModalConversation.call(
                        api_key=cfg["api_key"],
                        model=model_name,
                        messages=messages,
                    )
                    if response.status_code == 200:
                        text = response.output.choices[0].message.content[0]["text"]
                        elapsed = time.time() - t0
                        logger.info(f"[ApiOCR] Done: {elapsed:.1f}s, {len(text)} chars")
                        return text
                    else:
                        logger.warning(f"[ApiOCR] Attempt {attempt+1}: {response.code} {response.message}")
                        time.sleep(3)
                except Exception as e:
                    logger.warning(f"[ApiOCR] Attempt {attempt+1}: {e}")
                    time.sleep(3)

            raise RuntimeError("API OCR failed after 3 retries")

        except Exception as e:
            logger.error(f"[ApiOCR] Failed: {e}")
            raise

    # ── 工具方法 ─────────────────────────────────

    @staticmethod
    def classify_page(pdf_path: str, page_num: int) -> PageType:
        """
        分类单个 PDF 页面。
        复用 step2 的分类逻辑。

        Returns:
            "vector" | "scanned" | "table"
        """
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        page = doc[page_num]
        text = page.get_text()
        doc.close()

        # 检查是否有有效中文文本
        import regex as re
        han_count = len(re.findall(r'[\u4e00-\u9fff]', text))
        has_cid = bool(re.search(r'\(cid:\d+\)', text))

        if han_count >= 5 and not has_cid:
            return "vector"
        return "scanned"

    def shutdown(self):
        """释放本地模型资源"""
        self._local_backend = None
        # 模型会被 GC 回收
