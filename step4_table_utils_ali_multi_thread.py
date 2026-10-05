# step4_table_utils_ali.py
# 调用大模型进行图片描述——DashScope SDK
import base64
import json
import os
import random
import ssl
import time
import dashscope
from openai import OpenAI
from model_config import MODEL_CONFIG  # 引入配置文件

# 默认的大模型厂商和模型名称
DEFAULT_VENDOR = "ali"
DEFAULT_MODEL_NAME = "qwen-vl-ocr"

# ── 瞬态/永久错误分类 ──────────────────────────────
_TRANSIENT_ERROR_TYPES = {
    "BrokenPipeError", "TimeoutError", "ConnectionError", "ConnectionResetError",
    "ConnectionRefusedError", "ConnectionAbortedError",
    "RemoteDisconnected", "ChunkedEncodingError", "SSLError",
    "ProxyError", "ProtocolError", "ReadTimeoutError", "ConnectTimeoutError",
    "ReadTimeout", "ConnectTimeout", "RemoteProtocolError",
}

_PERMANENT_ERROR_MSG_PATTERNS = [
    "InvalidApiKey", "invalid_api_key",
    "401", "403", "404",
    "image decode", "decode error", "invalid image",
]


def _is_transient_error(e: Exception) -> bool:
    """Check if an exception is transient (should be retried) vs permanent (should abort)."""
    type_name = type(e).__name__
    if type_name in _TRANSIENT_ERROR_TYPES:
        return True
    msg = str(e).lower()
    for pat in ["response ended prematurely", "broken pipe", "connection reset",
                 "timed out", "timeout", "remote disconnected", "chunked"]:
        if pat in msg:
            return True
    for pat in _PERMANENT_ERROR_MSG_PATTERNS:
        if pat.lower() in msg:
            return False
    if isinstance(e, (ValueError, KeyError)):
        return False
    return True


def _load_env_file(env_path: str):
    try:
        if not os.path.exists(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and ((k not in os.environ) or (not os.environ.get(k))):
                    os.environ[k] = v
    except Exception:
        return


project_root = os.path.dirname(os.path.abspath(__file__))
_load_env_file(os.path.join(project_root, ".env"))

# ── OCR 路由器集成 ──────────────────────────────
# 支持 OCR_BACKEND=local|api|auto 环境变量切换后端
_ROUTER = None

def _get_router():
    """懒加载 OCR 路由器"""
    global _ROUTER
    if _ROUTER is None:
        try:
            from ocr_router import OCRRouter
            _ROUTER = OCRRouter(mode="auto")
        except ImportError:
            pass
    return _ROUTER

def _is_local_mode() -> bool:
    """检查是否应使用本地 OCR"""
    backend = os.environ.get("OCR_BACKEND", "api")
    return backend == "local"

# ─────────────────────────────────────────────────


def ocr_with_router(image_path: str, vendor: str = DEFAULT_VENDOR, model_name: str = DEFAULT_MODEL_NAME, retries: int = 3, delay: int = 5) -> str:
    """
    根据 OCR_BACKEND 环境变量路由到本地或 API OCR。
    用法：直接替换原有 generate_table_description 调用。
    
    环境变量:
      OCR_BACKEND=local   → 使用本地 DeepSeek OCR 2 (MPS)，忽略 vendor/model_name
      OCR_BACKEND=api     → 使用阿里云 API（默认行为）
      OCR_BACKEND=auto    → 自动判断（保留，暂未实现批量感知）
    """
    backend = os.environ.get("OCR_BACKEND", "api")
    if backend == "local":
        router = _get_router()
        if router is None:
            raise RuntimeError("OCR_BACKEND=local but ocr_router module not available")
        return router.ocr(image_path, page_type="table")
    else:
        # 默认走原有 API 路径
        return generate_table_description(image_path, vendor, model_name, retries, delay)


def get_model_config(vendor: str, model_name: str) -> dict:
    """
    获取指定厂商和模型名称的配置。
    :param vendor: 大模型厂商
    :param model_name: 大模型名称
    :return: 对应的模型配置信息字典
    """
    if vendor not in MODEL_CONFIG:
        raise ValueError(f"厂商 {vendor} 不存在于配置文件中。")

    vendor_config = MODEL_CONFIG[vendor]
    if model_name not in vendor_config:
        raise ValueError(f"模型 {model_name} 不存在于厂商 {vendor} 的配置中。")

    cfg = dict(vendor_config[model_name])
    if vendor == "ali":
        env_key = (os.environ.get("ALI_API_KEY", "") or os.environ.get("DASHSCOPE_API_KEY", "")).strip()
        env_base = os.environ.get("ALI_BASE_URL", "").strip()
        if env_key:
            cfg["api_key"] = env_key
        if env_base:
            cfg["base_url"] = env_base
    elif vendor == "kimi":
        env_key = os.environ.get("KIMI_API_KEY", "").strip()
        env_base = os.environ.get("KIMI_BASE_URL", "").strip()
        if env_key:
            cfg["api_key"] = env_key
        if env_base:
            cfg["base_url"] = env_base
    return cfg


def generate_table_description(
    image_path: str,
    vendor: str = DEFAULT_VENDOR,
    model_name: str = DEFAULT_MODEL_NAME,
    retries: int = 5,
    delay: int = 5
) -> str:
    """
    调用 DashScope SDK 以流式输出的方式对给定图片生成表格描述，
    并采用连续输出的方式构造返回值。如果请求超时，将自动重试指定次数。
    
    :param image_path: 图片路径
    :param vendor: 大模型厂商（默认使用 DEFAULT_VENDOR）
    :param model_name: 大模型名称（默认使用 DEFAULT_MODEL_NAME）
    :param retries: 重试次数
    :param delay: 每次重试间隔秒数
    :return: 表格描述文本
    """
    # ── 路由检查：如果 OCR_BACKEND=local，直接走本地 OCR ──
    if os.environ.get("OCR_BACKEND", "") == "local":
        router = _get_router()
        if router is not None:
            return router.ocr(image_path, page_type="table")
        # fallthrough to API if router not available

    # 获取模型配置
    model_config = get_model_config(vendor, model_name)

    # 读取图片并转换为 Base64 编码
    try:
        with open(image_path, "rb") as image_file:
            image_data = image_file.read()
    except Exception as e:
        raise Exception(f"读取图片失败: {e}")

    base64_image = base64.b64encode(image_data).decode("utf-8")

    # 构造文本提示
    text_prompt = (
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
        "   - 如果判断本页的首行信息是某一段落的开始（例如首个字前面有空格或缩进）、或者你判断本页的首行为标题行，则请在本页首个字的前面加上换行符“\n”。\n"
        "4. **限制条件**：\n"
        "   - 请注意！不要遗漏正文中任何信息！禁止合并相关内容！生成完毕检查是否有信息没有被描述到，如果有，请补充。\n"
        "   - 仅基于图片中的正文内容生成，禁止添加推测性信息。\n"
        "   - 严格按照图片中的原始顺序生成描述内容。\n"
    )

    # 构造消息（图片信息和文本提示）
    messages = [
        {
            "role": "user",
            "content": [
                {"image": f"data:image/png;base64,{base64_image}"},
                {"text": text_prompt}
            ]
        }
    ]

    # 重试机制（指数退避 + 错误分类）
    for attempt in range(retries):
        try:
            if vendor == "ali":
                responses = dashscope.MultiModalConversation.call(
                    api_key=model_config["api_key"],
                    model=model_config["model"],
                    messages=messages,
                    stream=True,
                    incremental_output=True,
                    vl_high_resolution_images=False
                )
                full_content = ""
                for response in responses:
                    try:
                        chunk = response["output"]["choices"][0]["message"].content[0]["text"]
                        full_content += chunk
                    except Exception:
                        continue
                if full_content:
                    return full_content
                print(f"\n第 {attempt+1} 次请求未返回有效内容。")
            else:
                client = OpenAI(api_key=model_config["api_key"], base_url=model_config["base_url"])

                candidates = [model_config["model"]]
                if vendor == "ali":
                    for m in ["qwen-vl-ocr"]:
                        if m not in candidates:
                            candidates.append(m)

                last_content = ""
                for model_id in candidates:
                    response = client.chat.completions.create(
                        model=model_id,
                        messages=[{
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}},
                                {"type": "text", "text": text_prompt}
                            ]
                        }],
                        stream=True,
                        max_tokens=8192,
                        temperature=0.3
                    )
                    full_content = ""
                    for chunk in response:
                        try:
                            part = chunk.choices[0].delta.content
                            if part:
                                full_content += part
                        except Exception:
                            continue
                    
                    content = full_content.strip()
                    if content:
                        # 如果内容比较丰富（大于10个字），直接返回
                        if len(content) > 10:
                            return content
                        # 如果是短内容（如页码），先记下来，如果后面没更好的再返回
                        last_content = content
                
                # 如果所有候选模型都运行完毕，且拿到了短内容，则返回该内容
                if last_content:
                    return last_content

                print(f"\n第 {attempt+1} 次请求未返回有效内容。")
        except Exception as e:
            if not _is_transient_error(e):
                print(f"\n第 {attempt+1} 次请求异常（永久错误，不重试）: {e}")
                break
            print(f"\n第 {attempt+1} 次请求异常（瞬态，重试）: {e}")

        if attempt < retries - 1:
            sleep_time = delay * (2 ** attempt) + random.uniform(0, 2)
            print(f"等待 {sleep_time:.1f}s 后重试...")
            time.sleep(sleep_time)

    return ""


# ─── 结构化可比租金提取（OCR阶段集成，非事后补救）───

# 关键词检测：判断一段表格描述是否属于「可比实例详情」
_COMP_RENT_KEYWORDS = [
    "可比实例", "可比项目", "比准租金", "市场租金",
    "可比案例", "市场比较法", "周边可比",
]

_STRUCTURED_PROMPT = """你是一个数据提取专家。以下是中国REITs招募说明书评估报告章节中，由视觉模型生成的自然语言表格描述。

请提取可比实例（周边类似项目）的租金信息，包括：
- 每个可比实例的租金数值（保留原始数字）
- 租金单位（元/平方米/月、元/平方米/天 等）
- 估价对象的租金（target_rent，可能是"评估单价"或"比准租金"）
- 资产名称

如果描述文本中确实不包含任何租金数字，返回 {"error": "no_rent_in_text"}。

如果有租金数据，返回 JSON：
{
  "asset_name": "资产名称",
  "comp_rents": [数值列表],
  "target_rent": 数值或null,
  "rent_unit": "单位"
}

注意：只提取文本中已存在的数字，严禁编造。

描述文本：
{description}"""


def is_comparable_rent_table(description: str) -> bool:
    """检测表格描述是否属于可比实例租金表"""
    if not description:
        return False
    desc_lower = description
    # 必须同时命中「可比」类和「租金/元」类
    has_comp = any(kw in desc_lower for kw in ["可比实例", "可比项目", "可比案例", "市场比较法"])
    has_rent = any(kw in desc_lower for kw in ["元/平方米", "元/㎡", "元/月/平方米", "元/m²", "元/平方", "租金"])
    return has_comp and has_rent


def parse_comparable_rent_json(description: str) -> dict:
    """
    从自然语言表格描述中提取结构化可比租金 JSON。
    使用 DashScope qwen-turbo（纯文本，零编造风险）。
    
    Args:
        description: generate_table_description() 产出的自然语言描述
    
    Returns:
        dict: 结构化租金数据，或 {"error": "..."}
    """
    import re
    import requests
    
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        api_key = os.environ.get("ALI_API_KEY", "").strip()
    if not api_key:
        return {"error": "no_dashscope_api_key"}
    
    prompt = _STRUCTURED_PROMPT.replace("{description}", description[:3000])
    
    for attempt in range(2):
        try:
            resp = requests.post(
                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "qwen-turbo",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 1000,
                },
                timeout=45,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                json_match = re.search(r'\{[\s\S]*\}', content)
                if json_match:
                    return json.loads(json_match.group())
                return {"error": "no_json_in_response", "raw": content[:200]}
            elif resp.status_code == 429:
                time.sleep((attempt + 1) * 5)
            else:
                if attempt == 0:
                    time.sleep(2)
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
    
    return {"error": "api_failed"}


if __name__ == "__main__":
    image_path = r"***.png"
    desc = generate_table_description(image_path)
    print("描述生成完毕。\n")
    if is_comparable_rent_table(desc):
        print("检测到可比实例表，提取结构化JSON...")
        result = parse_comparable_rent_json(desc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
