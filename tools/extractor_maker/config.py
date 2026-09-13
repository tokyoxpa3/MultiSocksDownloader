"""LLM 連線設定：讀寫 config.json，並支援環境變數覆寫。

優先順序（後者覆蓋前者）：
  config.json 內容  <  環境變數 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL  <  CLI 參數

config.json 內含 api_key，已被 .gitignore 排除，不應入版控。
"""

import json
import os

# 預設值：base_url 留空表示尚未設定；model 用一個常見的 OpenAI 相容別名。
DEFAULT_CONFIG = {
    'base_url': '',          # 例：https://api.deepseek.com/v1（需含 /v1）
    'api_key': '',
    'model': 'gpt-4o-mini',  # 可改成 deepseek-chat / qwen-... 等
}

# 設定欄位對應的環境變數
ENV_KEYS = {
    'base_url': 'LLM_BASE_URL',
    'api_key': 'LLM_API_KEY',
    'model': 'LLM_MODEL',
}


def config_path():
    """config.json 的絕對路徑（與本模組同目錄）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')


def load_config():
    """載入設定：config.json + 環境變數覆寫。"""
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in DEFAULT_CONFIG:
                    if key in data and data[key] is not None:
                        cfg[key] = data[key]
        except (json.JSONDecodeError, OSError):
            # 設定檔損壞時退回預設，不阻斷執行
            pass
    for key, env_name in ENV_KEYS.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    return cfg


def save_config(cfg):
    """寫回 config.json（僅存已知欄位）。"""
    path = config_path()
    data = {key: cfg.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
