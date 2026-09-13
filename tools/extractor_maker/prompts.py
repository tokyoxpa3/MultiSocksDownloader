"""系統提示與 few-shot 範例：教 LLM 讀請求特徵、二分類、填樣板。"""

import json

SYSTEM_PROMPT = """你是 yt-dlp extractor 逆向工程專家。你會收到一個網站影片頁的「網路請求特徵摘要」（digest），任務是判斷該站能否被靜態重播，並生成對應程式碼。

判別規則：
- 若取得影片直連所需的每個參數/token 都能從「前一個 response 的 body 或 query」找到，或 API/直連網址可用影片 id 直接代入 → mode="extractor"（生成 yt-dlp InfoExtractor）。
- 若某個 header 或 query 參數的值是頁面 JS 當場算出來的（簽章/HMAC/加密/指紋），在前面的 response 找不到來源 → mode="resolver"（生成 Playwright 腳本）。

輸出：只輸出一個 JSON 物件，不要輸出其他文字或 markdown。JSON 欄位：
{
  "mode": "extractor" 或 "resolver",
  "site_name": "小寫網站名，只能含 a-z0-9_，作檔名/類別名前綴用",
  "valid_url": "_VALID_URL 正則，含 (?P<id>...) 捕捉影片 id",
  "code": "完整可執行的 Python 程式碼",
  "reasoning": "一句話說明為何選這個 mode"
}

extractor 的 code 必須是 InfoExtractor 子類（只含類別本體，不含 import 那行）：
- 類別名 = site_name 駝峰化 + "IE" 結尾。
- 用 self._match_id(url) 取得影片 id。
- 用 self._download_webpage / self._download_json 重播關鍵請求、傳遞正確的 headers/query。
- 回傳 dict 至少含 'id' 與 'url'（或 'formats'）。
- 若切換不同影片/來源/集數時網址不變（前端用 JS 切換），完整清單通常以 JS 陣列
  （如 videourls[來源][集]['url']）靜態寫在初始 HTML 裡。此時應解析整個陣列，把每
  一條攤平成 formats，並給可辨識的 format_id（如 s<來源>e<集>），而非只抓單一預設值。
- 這類 JS 字串常把 / 轉義成 \/，解析前要還原；嵌套陣列要用括號配對法取整段，
  不要用無法處理嵌套的簡單正則。
- 多來源/多集時，用 format 的 preference 讓站方預設（通常是來源1第1集）優先。

resolver 的 code 必須是獨立腳本，用 playwright.sync_api：開頁面 → 攔截/等待媒體請求 → 抓到最終直連 URL，最後用 print(直連URL) 輸出（最後一行必須是直連 URL）。腳本必須用 if __name__ == '__main__': 守衛入口、從 sys.argv[1] 讀 URL，頂層不得有會執行的程式碼（以免被當模組 import 時炸掉）。
"""


def build_messages(digest_text):
    """組出 (system + few-shot + 本次 digest) 的 messages。"""
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    messages.extend(_few_shot_extractor())
    messages.extend(_few_shot_resolver())
    messages.append({'role': 'user', 'content': digest_text})
    return messages


def _few_shot_extractor():
    """範例一：token 在前置 response 可找到 → 靜態重播 → extractor。"""
    digest = (
        "page_url: https://demo.example.com/watch/8842\n"
        "video_id_guess: 8842\n"
        "\n[0] GET https://demo.example.com/watch/8842\n"
        "    status=200 type=document ct=text/html\n"
        "    body=...window.__cfg = {\"token\":\"a1b2c3\",\"title\":\"demo clip\"}...\n"
        "\n[1] GET https://cdn.demo.example.com/api/stream?id=8842&token=a1b2c3\n"
        "    status=200 type=xhr ct=application/json\n"
        "    hdr Referer=https://demo.example.com/watch/8842\n"
        "    body={\"url\":\"https://media.demo.example.com/v/8842.mp4\"}"
    )
    result = {
        "mode": "extractor",
        "site_name": "demo",
        "valid_url": r"https?://demo\.example\.com/watch/(?P<id>\d+)",
        "code": "class DemoIE(InfoExtractor):\n"
                "    _VALID_URL = r'https?://demo\\.example\\.com/watch/(?P<id>\\d+)'\n"
                "\n"
                "    def _real_extract(self, url):\n"
                "        video_id = self._match_id(url)\n"
                "        webpage = self._download_webpage(url, video_id)\n"
                "        token = self._search_regex(\n"
                "            r'__cfg\\s*=\\s*\\{[^}]*\"token\":\"([^\"]+)\"', webpage, 'token')\n"
                "        stream = self._download_json(\n"
                "            'https://cdn.demo.example.com/api/stream', video_id,\n"
                "            query={'id': video_id, 'token': token},\n"
                "            headers={'Referer': url})\n"
                "        return {\n"
                "            'id': video_id,\n"
                "            'title': self._html_search_meta('og:title', webpage, default=video_id),\n"
                "            'url': stream['url'],\n"
                "        }",
        "reasoning": "token 可在前一個頁面 body 找到，可靜態重播。",
    }
    return [
        {'role': 'user', 'content': 'digest:\n' + digest},
        {'role': 'assistant', 'content': json.dumps(result, ensure_ascii=False)},
    ]


def _few_shot_resolver():
    """範例二：header 值由 JS 簽章算出、無前置來源 → resolver。"""
    digest = (
        "page_url: https://secure.example.com/video/552\n"
        "video_id_guess: 552\n"
        "\n[0] GET https://secure.example.com/video/552\n"
        "    status=200 type=document ct=text/html\n"
        "    body=...signed_url() { return hmac(secret, ts) }...\n"
        "\n[1] GET https://api.secure.example.com/play/552\n"
        "    status=200 type=xhr ct=application/json\n"
        "    hdr X-Sign=9f8e7d6c5b4a（值為 JS 計算，前面 response 找不到）\n"
        "    body={\"src\":\"https://media.secure.example.com/552/index.m3u8\"}"
    )
    result = {
        "mode": "resolver",
        "site_name": "secure",
        "valid_url": r"https?://secure\.example\.com/video/(?P<id>\d+)",
        "code": "import sys\n"
                "from playwright.sync_api import sync_playwright\n"
                "\n"
                "def main():\n"
                "    url = sys.argv[1]\n"
                "    with sync_playwright() as pw:\n"
                "        browser = pw.chromium.launch(headless=True)\n"
                "        page = browser.new_page()\n"
                "        media = []\n"
                "        page.on('request', lambda r: media.append(r.url) if '.m3u8' in r.url or '.mp4' in r.url else None)\n"
                "        page.goto(url)\n"
                "        page.wait_for_timeout(3000)\n"
                "        browser.close()\n"
                "    print(media[-1] if media else '')\n"
                "\n"
                "if __name__ == '__main__':\n"
                "    main()",
        "reasoning": "X-Sign 由 JS 當場算出、無法靜態重播，需真瀏覽器。",
    }
    return [
        {'role': 'user', 'content': 'digest:\n' + digest},
        {'role': 'assistant', 'content': json.dumps(result, ensure_ascii=False)},
    ]
