# model_config.py
import os


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


MODEL_CONFIG = {
    "deepseek": {
         "deepseek-chat": {
                  "model": "deepseek-chat",
                  "api_key": "YOUR_DEEPSEEK_API_KEY",
                  "base_url": "https://api.deepseek.com"
         },
         "deepseek-reasoner": {
                  "model": "deepseek-reasoner",
                  "api_key": "YOUR_DEEPSEEK_API_KEY",
                  "base_url": "https://api.deepseek.com"
         },
    },
    "zhipu": {
         "GLM-4V-Flash": {
                    "model": "GLM-4V-Flash",
                    "api_key": "YOUR_ZHIPU_API_KEY",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4/"
         },
         "embedding-3": {
                    "model": "embedding-3",
                    "api_key": "YOUR_ZHIPU_API_KEY",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4/"
         }
    },
    "ali": {
         "qwen-vl-ocr-latest": {
                  "model": "qwen-vl-ocr-latest",
                  "api_key": os.environ.get("ALI_API_KEY", ""),
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
         },
         "qwen-vl-max": {
                  "model": "qwen-vl-max",
                  "api_key": os.environ.get("ALI_API_KEY", ""),
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
         },
         "text-embedding-v4": {
                  "model": "text-embedding-v4",
                  "api_key": os.environ.get("ALI_API_KEY", ""),
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
         },
         "deepseek-v3": {
                  "model": "deepseek-v3",
                  "api_key": os.environ.get("ALI_API_KEY", ""),
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
         },
         "deepseek-r1": {
                  "model": "deepseek-r1",
                  "api_key": os.environ.get("ALI_API_KEY", ""),
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
         }
    },
    "kimi": {
         "kimi-latest": {
                  "model": "kimi-latest",
                  "api_key": "",
                  "base_url": "https://api.moonshot.cn/v1"
         }
    }
}


