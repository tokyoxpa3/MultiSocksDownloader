"""把攔截到的請求結構化成「請求特徵 digest」，供 LLM 分析。

digest 是 LLM 的唯一輸入，重點不在完整重現網路內容，而在保留
「能否被靜態重播」所需的判據：依賴關係、header 靜態/動態、參數來源、
以及哪個請求最終吐出媒體。
"""

from urllib.parse import urlparse, parse_qs, parse_qsl

# 對 LLM 判斷有價值的 header 關鍵字（token / 簽章 / 來源 / 認證）
_USEFUL_HEADER_HINTS = (
    'referer', 'user-agent', 'authorization', 'cookie', 'origin',
    'accept', 'x-', 'range', 'sec-',
)

# 常見的「影片 id」query 參數名，用於猜 id
_ID_QUERY_NAMES = ('id', 'video', 'vid', 'video_id', 'v', 'watch', 'guid', 'key')


def guess_video_id(url):
    """從 URL 猜影片 id：優先看常見 query 參數，否則取路徑最後一段。"""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for name in _ID_QUERY_NAMES:
        if name in query and query[name]:
            return query[name][0]
    path = parsed.path.rstrip('/')
    if path:
        segment = path.split('/')[-1]
        if segment:
            return segment
    return ''


def build_digest(captured_requests, page_url, video_id=None):
    """把 CapturedRequest 清單轉成結構化 digest（dict）。

    captured_requests 應為 capture.capture() 回傳的媒體相關請求（已含
    status/content_type/body_snippet 等欄位）。
    """
    video_id = video_id or guess_video_id(page_url)
    requests = []
    for req in captured_requests:
        parsed = urlparse(req.url)
        requests.append({
            'method': req.method,
            'url': req.url,
            'host': parsed.hostname or '',
            'path': parsed.path or '',
            'query': parse_qsl(parsed.query),
            'headers': _summarize_headers(req.headers),
            'post_data': (req.post_data or '')[:500],
            'status': req.status,
            'content_type': req.content_type,
            'body_snippet': req.body_snippet,
            'resource_type': req.resource_type,
        })
    return {
        'page_url': page_url,
        'video_id_guess': video_id,
        'requests': requests,
    }


def _summarize_headers(headers):
    """保留對 LLM 有價值的 header；值裁到合理長度避免 token 爆量。"""
    out = {}
    for name, value in (headers or {}).items():
        lower = name.lower()
        if any(hint in lower for hint in _USEFUL_HEADER_HINTS):
            if isinstance(value, (str, bytes)):
                value = str(value)[:120]
            out[name] = value
    return out


def digest_to_text(digest):
    """把 digest 轉成可塞進 LLM prompt 的精簡純文字。"""
    lines = [
        f"page_url: {digest['page_url']}",
        f"video_id_guess: {digest['video_id_guess']}",
    ]
    for i, req in enumerate(digest['requests']):
        lines.append(f"\n[{i}] {req['method']} {req['url']}")
        meta = []
        if req.get('status') is not None:
            meta.append(f"status={req['status']}")
        if req.get('resource_type'):
            meta.append(f"type={req['resource_type']}")
        if req.get('content_type'):
            meta.append(f"ct={req['content_type']}")
        if meta:
            lines.append("    " + " ".join(meta))
        if req.get('query'):
            lines.append(f"    query={req['query']}")
        if req.get('post_data'):
            lines.append(f"    post={req['post_data']}")
        for name, value in req.get('headers', {}).items():
            lines.append(f"    hdr {name}={value}")
        if req.get('body_snippet'):
            lines.append(f"    body={req['body_snippet']}")
    return "\n".join(lines)
