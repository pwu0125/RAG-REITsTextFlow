# step4_glm_vision_utils.py — GLM-5.3-Flash 视觉描述后端（B 线, 2026-10-05）
# 复刻 step4_table_utils_ali.generate_table_description 的 prompt 与接口,
# 走 BigModel Coding Plan 的 anthropic-messages 端点(Zcode 同一 key, 包月不按量计费)。
# 失败大声抛错, 不静默降级。
import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_config import MODEL_CONFIG  # noqa: E402

DEFAULT_VENDOR = "glm"
DEFAULT_MODEL_NAME = "GLM-5.3-Flash"
ANTHROPIC_URL = os.environ.get(
    "GLM_ANTHROPIC_URL", "https://open.bigmodel.cn/api/anthropic/v1/messages")


def get_model_config(vendor: str, model_name: str) -> dict:
    if vendor not in MODEL_CONFIG:
        raise ValueError(f"厂商 {vendor} 不存在于配置文件中。")
    vendor_config = MODEL_CONFIG[vendor]
    if model_name not in vendor_config:
        raise ValueError(f"模型 {model_name} 不存在于厂商 {vendor} 的配置中。")
    return vendor_config[model_name]


# 与 step4_table_utils_ali 完全一致的 prompt(逐字复制, 保证 A/B 可比)
TEXT_PROMPT = (
    "请根据以下规则描述图片中的内容：\n"
    "1. **描述范围**：\n"
    "   - 只描述图片中的正文内容，忽略页眉、页脚、页码等非正文信息。\n"
    "   - 如果包含文字，请完整描述文字内容；如果图片中包含表格，请完整描述表格内容。\n"
    "2. **表格描述规则**：\n"
    "   - 如果图片中包含表格，请按照以下格式描述：\n"
    "     ## 表格主题\n"
    "     - 明确表格的主题（例如“XX项目历史运营数据”或“XX指标历史趋势”）。\n"
    "     - 如果表格有编号，请包含编号。\n"
    "     - 示例：“表格展示了基础设施项目2021至2024年的历史租金收缴率。”\n"
    "     ## 表格内容\n"
    "     - 按“横向行”逐行描述，对每个指标，按列顺序（从左到右）说明各时间段的具体数值，需包含单位和百分比。"
    "     - 示例：“表格内容为：出租率在2024年1-6月为100.00%，2023年度为100.00%，2022年度为100.00%，2021年度为100.00%”。\n"
    "     ## 描述结尾\n"
    "     - 在表格内容后面加上“表格内容描述完毕。”\n"
    "     ##格式要求：\n"
    "     - 请注意！使用纯文本段落，禁止使用表格、列表符号，如“|”、“---”等。\n"
    "     - 数值需与指标名称直接关联，避免歧义（例如“2023年度运营收入（不含税）为2,194.72万元”）。\n"
    "3. **文字描述规则**：\n"
    "   - 如果图片中包含文字（非表格），请逐段一字不落的描述文字内容，保留原文的段落结构和顺序。\n"
    "   - 如果判断本页的首行信息是某一段落的开始（例如首个字前面有空格或缩进）、或者你判断本页首行为标题行，则请在本页首个字的前面加上换行符“\n”。\n"
    "4. **限制条件**：\n"
    "   - 请注意！不要遗漏正文中任何信息！禁止合并相关内容！生成完毕检查是否有信息没有被描述到，如果有，请补充。\n"
    "   - 仅基于图片中的正文内容生成，禁止添加推测性信息。\n"
    "   - 严格按照图片中的原始顺序生成描述内容。\n"
)


def generate_table_description(
    image_path: str,
    vendor: str = DEFAULT_VENDOR,
    model_name: str = DEFAULT_MODEL_NAME,
    retries: int = 3,
    delay: int = 5,
) -> str:
    """GLM-5.3-Flash 视觉表格描述(anthropic-messages 通道)。
    接口与 ali 版完全一致, 直接可替换。"""
    model_config = get_model_config(vendor, model_name)
    api_key = model_config["api_key"]
    if not api_key:
        raise RuntimeError(f"[{vendor}/{model_name}] api_key 为空 — 检查 .env 的 GLM_API_KEY")

    try:
        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        raise Exception(f"读取图片失败: {e}")

    payload = {
        "model": model_config["model"],
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": base64_image}},
                {"type": "text", "text": TEXT_PROMPT},
            ],
        }],
    }

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                ANTHROPIC_URL,
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                r = json.loads(resp.read().decode())
            text = "".join(
                b.get("text", "") for b in r.get("content", [])
                if b.get("type") == "text"
            ).strip()
            if text:
                return text
            last_err = RuntimeError(f"第 {attempt+1} 次返回空内容")
            print(f"[{model_config['model']}] {last_err}", flush=True)
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read().decode()[:200]}"
            print(f"[{model_config['model']}] 第 {attempt+1} 次异常: {last_err}", flush=True)
        except Exception as e:
            last_err = e
            print(f"[{model_config['model']}] 第 {attempt+1} 次异常: {e}", flush=True)
        time.sleep(delay)
    raise RuntimeError(f"GLM 视觉描述失败({image_path}): {last_err}")
