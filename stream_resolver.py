#!/usr/bin/env python3
"""
串流解析器：用 yt-dlp 把「影片/音訊網頁網址」解析成單檔直連 URL，
讓 MultiSocksDownloader 現有的多代理分段引擎可以接手續傳。

格式選擇分三類：progressive（影音合一）／純音訊單檔會回傳直連 URL，
走現有多代理分段引擎；DASH/HLS 分段串流則回傳 native 模式，交由下游
用 yt-dlp 原生下載並以 ffmpeg 合併視訊+音訊。DRM 加密串流不碰
（yt-dlp 本身會自動略過帶 ContentProtection/PSSH 的串流）。

對外提供兩層介面：

- `StreamResolver` 抽象基底：下載引擎只依賴此介面，測試可注入 fake。
- `YtDlpStreamResolver`：預設實作，走 yt-dlp 兩段式（匿名 → 帶 Cookie）。
- `resolve_stream(url, headers)`：相容舊呼叫的模組層級函式，內部用
  預設 resolver；新程式請改用 `StreamResolveResult` 取得結構化失敗原因。
"""

import re
import os
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# 強制走非 lazy 的 extractor 載入路徑：lazy_extractors.py 是 yt-dlp 安裝時
# 生成的單一巨型模組，Nuitka 打包會讓 MSVC 編譯器 heap 溢位（C1002），
# 故建置時以 --nofollow-import-to 排除，執行期改由 _extractors 匯入各獨立
# extractor 模組。
os.environ.setdefault('YTDLP_NO_LAZY_EXTRACTORS', '1')

# 模組層級匯入（讓 Nuitka 靜態打包能偵測到 yt-dlp）；未安裝時設為 None，
# resolver 會回傳結構化錯誤而非拋例外。
try:
    import yt_dlp
except ImportError:
    yt_dlp = None

logger = logging.getLogger('stream_resolver')

# 明顯是「直接檔案」的副檔名：這類網址本身就是完整檔案，跳過 yt-dlp 省一次解析。
_DIRECT_EXT_RE = re.compile(
    r'\.(?:mp4|m4v|webm|mkv|flv|avi|mov|mp3|m4a|aac|wav|flac|ogg|opus'
    r'|zip|rar|7z|tar|gz|bz2|xz|exe|msi|dmg|pkg|deb|rpm|apk|iso|img'
    r'|pdf|docx?|xlsx?|pptx?|torrent|jpg|jpeg|png|gif|webp|svg|txt|csv|json|xml)'
    r'(?:$|[?#])',
    re.IGNORECASE,
)


def is_direct_file(url):
    """粗判網址是否本身就是一個直接檔案（有常見媒體/檔案副檔名）。"""
    return bool(_DIRECT_EXT_RE.search(url))


@dataclass
class StreamResolveResult:
    """串流解析的結構化結果，成功與失敗都透過它回傳，避免只回 None 吞掉死因。

    - ok：是否解析成功。
    - mode：'file' 表示解析出單檔直連 URL（用現有分段引擎下載）；
            'native' 表示只有 DASH/HLS 分段串流，應交回 yt-dlp 原生下載合併。
    - direct_url / title / headers / ext：mode='file' 時的有效欄位。
    - error_kind：失敗類別（機器可讀，供程式分流處理）。
    - error：人類可讀的失敗說明（含 yt-dlp 拋出的例外訊息）。
    """
    ok: bool
    direct_url: str = ''
    title: str = ''
    headers: dict = field(default_factory=dict)
    ext: str = ''
    mode: str = 'file'
    error_kind: str = ''
    error: str = ''


class StreamResolver(ABC):
    """串流解析抽象介面。下載引擎只依賴它，測試可注入 fake。"""

    @abstractmethod
    def resolve(self, url, headers=None, max_height=None, max_fps=None,
                audio_only=False):
        """解析影音網頁網址，回傳 StreamResolveResult。

        max_height：要求的最高畫質（像素高，如 1080）；None 表示不限（最高畫質）。
        max_fps   ：要求的最高幀率（如 30）；None 表示不限（自動）。
        audio_only：True 時只要音訊，不要視訊。
        """
        raise NotImplementedError


class YtDlpStreamResolver(StreamResolver):
    """預設串流解析實作：兩段式 yt-dlp 解析。

    先以匿名（不帶 Cookie）嘗試，失敗再帶上 headers 裡的 Cookie/User-Agent
    重試。對公開影片，匿名解析最穩，可避免把登入態 Cookie 丟給 yt-dlp 的
    匿名 web client 而觸發 YouTube「The page needs to be reloaded」bot 檢查；
    對需登入/年齡驗證的影片，再靠 Cookie 重試補救。
    """

    def resolve(self, url, headers=None, max_height=None, max_fps=None,
                audio_only=False):
        if yt_dlp is None:
            return StreamResolveResult(
                ok=False, error_kind='not_installed',
                error='未安裝 yt-dlp，無法解析串流')

        # 先匿名解析（公開影片最穩）
        result = self._resolve_once(url, None, max_height, max_fps, audio_only)
        if result.ok:
            return result

        # 匿名失敗（多半需登入），改帶瀏覽器 Cookie 重試一次
        anon_err = result.error or result.error_kind
        if headers:
            result = self._resolve_once(url, headers, max_height, max_fps, audio_only)
            if result.ok:
                return result
        else:
            logger.warning("yt-dlp 解析失敗（無 Cookie 可供重試）: %s -> %s",
                           url, anon_err)
            return result

        logger.warning("yt-dlp 解析失敗（匿名=%s，帶 Cookie=%s）: %s",
                       anon_err, result.error or result.error_kind, url)
        return result

    def _resolve_once(self, url, headers, max_height=None, max_fps=None,
                      audio_only=False):
        """單次 yt-dlp 解析；成功與失敗都回傳結構化結果。"""
        opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'skip_download': True,
            'socket_timeout': 15,
            'retries': 1,
        }
        if headers:
            opts['http_headers'] = dict(headers)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            logger.debug("yt-dlp 解析拋出例外（帶 Cookie=%s）: %s: %s",
                         bool(headers), url, e)
            return StreamResolveResult(
                ok=False, error_kind='resolve_error',
                error=f"yt-dlp 解析拋出例外: {e}")

        if not isinstance(info, dict):
            # extract_info 回 None：典型原因是需登入、年齡限制、或網址非影片頁。
            logger.debug("yt-dlp extract_info 回 %s（帶 Cookie=%s）: %s",
                         type(info).__name__, bool(headers), url)
            return StreamResolveResult(
                ok=False, error_kind='no_info',
                error='yt-dlp 未能取得影片資訊（可能需要登入、Cookie 過期或年齡限制）')

        title = info.get('title') or ''
        ext = _norm_ext(info.get('ext'))

        # 少數站直接給出單檔網址
        direct = info.get('url')
        if direct and _is_single_file(info):
            return StreamResolveResult(
                ok=True, direct_url=direct, title=title,
                headers=_http_headers(info), ext=ext, mode='file')

        # 依 formats 判斷下載路徑：progressive/純音訊 → 單檔（現有 Range 引擎）；
        # DASH/HLS 分段 → native（yt-dlp 原生下載 + ffmpeg 合併）。
        fmts = info.get('formats') or []
        mode, chosen = _classify_formats(fmts, max_height, max_fps, audio_only)
        if mode == 'native':
            logger.debug("DASH/HLS 分段串流（帶 Cookie=%s），改走 yt-dlp 原生: %s",
                         bool(headers), url)
            return StreamResolveResult(
                ok=True, mode='native', title=title,
                ext='.m4a' if audio_only else '.mp4')
        if mode == 'none':
            logger.debug("找不到任何可下載格式（帶 Cookie=%s）: %s",
                         bool(headers), url)
            return StreamResolveResult(
                ok=False, error_kind='no_single_format',
                error='該站不提供可下載的影音格式（可能需登入或年齡限制）')

        # mode in ('progressive', 'audio_only', 'other')：單檔，走現有引擎
        direct = chosen.get('url')
        if not direct:
            return StreamResolveResult(
                ok=False, error_kind='no_direct_url',
                error='選定的格式缺少直連 URL')
        ext = _norm_ext(chosen.get('ext')) or ext
        return StreamResolveResult(
            ok=True, direct_url=direct, title=title,
            headers=_http_headers(chosen), ext=ext, mode='file')


@dataclass
class FormatListResult:
    """影片可用畫質/幀率的摘要，供 UI 在下載前顯示真實選項。

    - ok：是否成功解析。
    - heights：可用的視訊畫素高度（由高到低、去重）。
    - fps_values：可用的視訊幀率（由高到低、去重）。
    - has_audio：是否有獨立音訊軌（「僅音訊」可選）。
    - title：影片標題（供 UI 在下載前預填存檔名稱；副檔名另由下載時決定）。
    - error：失敗時的人類可讀原因。
    """
    ok: bool
    heights: list = field(default_factory=list)
    fps_values: list = field(default_factory=list)
    has_audio: bool = False
    title: str = ''
    error: str = '' 


def list_video_formats(url, headers=None):
    """解析影音網址可用的畫質（高度）與幀率，供 UI 在下載前顯示選項。

    僅讀取 formats、不下載；Cookie 會先抽掉（同原生下載，避免觸發 bot 檢查）。
    """
    if yt_dlp is None:
        return FormatListResult(ok=False, error='未安裝 yt-dlp，無法解析串流')
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'skip_download': True,
        'socket_timeout': 15,
        'retries': 1,
    }
    if headers:
        opts['http_headers'] = {k: v for k, v in headers.items()
                                if k.lower() not in ('cookie', 'cookie2')}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return FormatListResult(ok=False, error=str(e))

    if not isinstance(info, dict):
        return FormatListResult(ok=False, error='未能取得影片資訊（可能需要登入）')

    fmts = info.get('formats') or []
    heights = set()
    fpses = set()
    has_audio = False
    for f in fmts:
        has_v = f.get('vcodec') not in (None, 'none')
        has_a = f.get('acodec') not in (None, 'none')
        if has_v:
            h = f.get('height')
            if h is not None:
                heights.add(int(h))
            fps = f.get('fps')
            if fps is not None:
                fpses.add(int(round(float(fps))))
        if has_a:
            has_audio = True
    return FormatListResult(
        ok=True,
        heights=sorted(heights, reverse=True),
        fps_values=sorted(fpses, reverse=True),
        has_audio=has_audio,
        title=info.get('title') or '',
    )


# 模組層級預設實例，供下游與相容函式共用。
_default_resolver = YtDlpStreamResolver()


def resolve_stream(url, headers=None, max_height=None, max_fps=None,
                   audio_only=False):
    """相容舊呼叫的解析函式：成功回傳 (direct_url, title, headers, ext)，
    失敗回傳 None。新程式請改用 `StreamResolver.resolve` 取得結構化原因。"""
    result = _default_resolver.resolve(url, headers, max_height=max_height,
                                       max_fps=max_fps, audio_only=audio_only)
    if not result.ok or result.mode != 'file':
        return None
    return result.direct_url, result.title, result.headers, result.ext


def _classify_formats(fmts, max_height=None, max_fps=None, audio_only=False):
    """依 formats 判斷下載路徑，回傳 (mode, chosen_format)。

    - 'progressive'：影音合一單檔，完整影片，可用現有 Range 分段引擎。
    - 'audio_only'  ：純音訊單檔（formats 中完全沒有視訊軌、或使用者只要音訊），
                      是完整媒體（音樂 / Podcast 等）。
    - 'native'      ：DASH/HLS 分段串流——有分離的視訊軌、或沒有任何
                      http/https 單檔（如 m3u8），需交回 yt-dlp 原生下載
                      並以 ffmpeg 合併視訊+音訊。
    - 'none'        ：連任何格式都沒有。

    畫質/FPS 篩選：yt-dlp 依慣例把 formats 由高到低排序，因此「第一個符合
    max_height / max_fps 限制」即為「該限制下的最高畫質」。audio_only=True 時
    直接回傳音訊軌，不考慮視訊（可用於從 DASH 影片只抓音訊）。

    關鍵區別：DASH 影片頁的「音訊軌」只是影片的一軌、不是完整媒體，不能像
    Podcast 那樣當單檔下載；只要存在分離視訊軌即判定為 native（除非使用者
    明確只要音訊）。
    """
    singles = [f for f in fmts
               if f.get('url') and f.get('protocol') in ('http', 'https')]

    def _fits(f):
        h = f.get('height')
        if max_height is not None and h is not None and int(h) > max_height:
            return False
        fps = f.get('fps')
        if max_fps is not None and fps is not None and float(fps) > max_fps:
            return False
        return True

    progressive = None
    audio_only_fmt = None
    has_video = False
    for f in singles:
        has_v = f.get('vcodec') not in (None, 'none')
        has_a = f.get('acodec') not in (None, 'none')
        if has_v and has_a:
            if progressive is None and _fits(f):
                progressive = f
        elif has_v:
            has_video = True
        elif has_a:
            if audio_only_fmt is None and _fits(f):
                audio_only_fmt = f

    if audio_only:
        if audio_only_fmt is not None:
            return ('audio_only', audio_only_fmt)
        # 沒有 http/https 單檔音訊（多半是 HLS-only），交給 native 由 yt-dlp 選 bestaudio。
        return ('native', None) if (has_video or fmts) else ('none', None)

    if progressive is not None:
        return ('progressive', progressive)
    if has_video:
        return ('native', None)
    if audio_only_fmt is not None:
        return ('audio_only', audio_only_fmt)
    if singles:
        # 有畫質/FPS 限制時，不應退回未受限制的第一個格式（會悄悄超過
        # 使用者選的畫質）；交給 native 讓 yt-dlp 依限制挑選，找不到就明確失敗。
        if max_height is not None or max_fps is not None:
            return ('native', None)
        return ('other', singles[0])
    return ('native', None) if fmts else ('none', None)


def build_native_format_selector(max_height=None, max_fps=None, audio_only=False):
    """產生給 yt-dlp native 下載用的 format selector 字串。

    progressive / 單檔路徑在 _classify_formats 已依畫質/FPS 篩選；native
    （DASH/HLS）分段串流則要在此把限制轉成 yt-dlp 的 format 選擇語法，例如：

        bestvideo*[height<=1080][fps<=30]+bestaudio/best[height<=1080][fps<=30]

    audio_only 時只要音訊（bestaudio 優先）。
    """
    if audio_only:
        return 'bestaudio/best'

    def _cap(base):
        s = base
        if max_height is not None:
            s += f'[height<={int(max_height)}]'
        if max_fps is not None:
            s += f'[fps<={int(max_fps)}]'
        return s

    return f"{_cap('bestvideo*')}+bestaudio/{_cap('best')}"


def _is_single_file(info):
    return info.get('protocol') in ('http', 'https', None)


def _http_headers(info):
    headers = {}
    h = info.get('http_headers')
    if isinstance(h, dict):
        headers.update(h)
    return headers


def _norm_ext(ext):
    if not ext:
        return ''
    return '.' + ext.lstrip('.')
