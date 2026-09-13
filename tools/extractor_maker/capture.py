"""Playwright 攔截：載入頁面、錄下所有網路請求，過濾出媒體/API 相關請求。

設計：每個攔截到的請求包成 CapturedRequest，response 到達時回填
status / content_type / body 摘要。id(request) 作為關聯鍵——Playwright 的
sync API 會依 guid 快取同一 Request 物件，因此 on_response 裡的
response.request 與 on_request 的 request 是同一 Python 物件。
"""

import re

# 媒體相關 content-type
_MEDIA_CONTENT_TYPE = re.compile(
    r'video/|audio/|application/(?:dash\+xml|x-mpegurl|vnd\.apple\.mpegurl)'
    r'|application/octet-stream',
    re.IGNORECASE,
)

# 明顯的媒體檔副檔名
_MEDIA_EXT = re.compile(
    r'\.(?:mp4|m4v|webm|mkv|flv|mov|m3u8|mpd|ts|mp3|m4a|aac|ogg|opus|wav)'
    r'(?:$|[?#])',
    re.IGNORECASE,
)

# 路徑/網址含這些關鍵字者視為「可能與影片取檔有關」
_API_HINT = re.compile(
    r'(api|stream|play|video|media|m3u8|manifest|source|fetch|dash|hls|ticket)',
    re.IGNORECASE,
)

# 值得抓 body 摘要的 text 類 content-type（避免把二進位檔解碼成亂碼）
_TEXT_CONTENT_TYPE = re.compile(
    r'text/|json|xml|javascript|x-mpegurl|dash\+xml',
    re.IGNORECASE,
)

# 從文件 body 定位媒體線索時掃描的關鍵字（videourl/m3u8/mp4 等）。
# 只收高信號字串；「dash/hls/manifest」這類裸字會匹配 CSS 的 dashed、以及
# 大量 src="..." 等雜訊，把預算吃光而錯過真正的 videourl。
_MEDIA_SNIPPET_HINTS = re.compile(
    r'videourl|video[_-]?url|video[_-]?src|stream[_-]?url|'
    r'\.m3u8\b|\.mpd\b|\.mp4\b|\.m4v\b|\.webm\b|\.mkv\b|\.ts\b',
    re.IGNORECASE,
)


class CapturedRequest:
    """單一攔截到的請求，含其 response 摘要。"""

    def __init__(self, method, url, headers, post_data, resource_type):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self.post_data = post_data
        self.resource_type = resource_type
        self.status = None
        self.content_type = ''
        self.body_snippet = ''


def _media_snippet(text, max_chars=4000, context=180):
    """從文件 body 抽出圍繞媒體關鍵字的上下文。

    大型頁面 HTML 中，videourl/m3u8 常藏在數百 KB 之後，直接取前 N 字元會
    漏掉關鍵 token；改為掃描媒體關鍵字，把每個命中點前後各 context 字元
    剪出，直到逼近 max_chars 上限。找不到關鍵字時退回取開頭。
    """
    chunks = []
    total = 0
    for m in _MEDIA_SNIPPET_HINTS.finditer(text):
        start = max(0, m.start() - context)
        end = min(len(text), m.end() + context)
        chunk = text[start:end].replace('\n', ' ')
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    if not chunks:
        return text[:max_chars]
    return (' … '.join(chunks))[:max_chars]


def _is_media_like(req):
    ct = (req.content_type or '').lower()
    url = req.url.lower()
    if _MEDIA_CONTENT_TYPE.search(ct) or _MEDIA_EXT.search(url):
        return True
    # <video>/<audio> 元素發起的請求一定是媒體
    if req.resource_type == 'media':
        return True
    # 字型/圖片/樣式/ping 不靠 URL 關鍵字誤判（例如檔名含 "source" 的字型）
    if req.resource_type in ('font', 'image', 'stylesheet', 'ping'):
        return False
    return bool(_API_HINT.search(url))


def capture(url, headless=True, user_data_dir=None, timeout_ms=15000,
            settle_ms=3000):
    """開 chromium 錄請求，回傳 (final_url, media_requests, all_requests)。

    - headless=False：適合需要登入/人機驗證的站。
    - user_data_dir：給定時沿用該 profile 的 cookie/登入態（需 headless=False
      首次登入一次）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            '未安裝 playwright。請先執行: pip install playwright && '
            'playwright install chromium'
        ) from exc

    all_requests = []
    by_id = {}

    def on_request(request):
        try:
            post_data = request.post_data
        except Exception:
            # post_data 對 binary/non-UTF8 body（gzip、protobuf 等）會解碼失敗；
            # post body 對 digest 只是輔助資訊，解不出來就略過，別讓它炸掉攔截。
            post_data = ''
        captured = CapturedRequest(
            request.method,
            request.url,
            dict(request.headers),
            post_data,
            request.resource_type,
        )
        by_id[id(request)] = captured
        all_requests.append(captured)

    def on_response(response):
        captured = by_id.get(id(response.request))
        if captured is None:
            return
        captured.status = response.status
        try:
            captured.content_type = response.headers.get('content-type', '') or ''
        except Exception:
            captured.content_type = ''
        try:
            # 只對文字類 content-type 抓 body 摘要，二進位檔（影片/字型/圖片）跳過
            if captured.content_type and _TEXT_CONTENT_TYPE.search(captured.content_type):
                body = response.body()
                text = body.decode('utf-8', errors='replace')
                # 文件頁（HTML）很大，videourl/m3u8 常藏在深處，只取前 2048 字元
                # 會錯過 token；改為掃描媒體關鍵字截出上下文。
                if captured.resource_type == 'document':
                    captured.body_snippet = _media_snippet(text)
                else:
                    captured.body_snippet = text[:2048]
        except Exception:
            captured.body_snippet = ''

    final_url = url
    main_doc_id = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context_opts = {}
        if user_data_dir:
            context_opts['user_data_dir'] = user_data_dir
        context = browser.new_context(**context_opts)
        page = context.new_page()
        page.on('request', on_request)
        page.on('response', on_response)
        try:
            resp = page.goto(url, wait_until='domcontentloaded', timeout=timeout_ms)
            # 等 JS 後續發起的 media/API 請求；可視為一個「讓頁面跑起來」的緩衝。
            page.wait_for_timeout(settle_ms)
            final_url = page.url
            # 記錄主文件（頁面 HTML）request id：extractor 的 token 往往寫死在
            # 這份 HTML 裡，而它的 resource_type/URL 不會被 _is_media_like 命中，
            # 若不強制納入 digest，LLM 會誤判成「token 來源不明 → resolver」。
            if resp is not None:
                main_doc_id = id(resp.request)
        except Exception:
            # 頁面載入逾時/部分失敗不中斷流程——只要錄到東西就繼續。
            pass
        finally:
            browser.close()

    media_requests = [r for r in all_requests if _is_media_like(r)]
    # 把主文件強制納入 digest，做為 LLM 判斷 token 來源的依據。
    main_doc = by_id.get(main_doc_id) if main_doc_id is not None else None
    if main_doc is None:
        for r in all_requests:
            if r.resource_type == 'document' and r.url in (url, final_url):
                main_doc = r
                break
    if main_doc is not None and main_doc not in media_requests:
        media_requests.insert(0, main_doc)
    return final_url, media_requests, all_requests
