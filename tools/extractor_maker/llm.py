"""OpenAI 相容 chat completions 客戶端（用 requests，不加 SDK）。"""

import json
import logging
import requests

logger = logging.getLogger('extractor_maker.llm')


def chat(base_url, api_key, model, messages, temperature=0.2, timeout=180):
    """呼叫 OpenAI 相容 /chat/completions，回傳 assistant 文字內容。"""
    url = base_url.rstrip('/') + '/chat/completions'
    headers = {
        'Authorization': 'Bearer ' + api_key,
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f'LLM 回覆格式異常: {data}') from exc


def parse_json_block(text):
    """從 LLM 回覆中抽出 JSON 物件。

    容忍 markdown code fence（```json ... ```）與前後雜訊，抓第一個
    '{' 到最後一個 '}' 區段。
    """
    if not text:
        raise ValueError('LLM 回覆為空')
    text = text.strip()
    if text.startswith('```'):
        lines = text.splitlines()
        if lines and lines[0].startswith('```'):
            lines = lines[1:]
        if lines and lines[-1].startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    start = text.find('{')
    end = text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError('LLM 回覆中找不到 JSON 物件')
    return json.loads(text[start:end + 1])
