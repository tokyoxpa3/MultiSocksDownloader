import os
import sys
import time
import json
import logging
import threading
import re
import random
import base64
from collections import deque
from urllib.parse import urlparse, urlunparse, unquote, parse_qs, quote

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from ftp_downloader import SocksFTP, parse_ftp_url
from bt_downloader import BTTask, DHTService, DHTGovernor, source_kind, bt_info_hash
import stream_resolver
from stream_resolver import (YtDlpStreamResolver, build_native_format_selector,
                             build_socks_proxy_url)
import i18n

logger = logging.getLogger('downloader')

# 區塊（切片）自適應大小：依檔案大小決定區塊數，避免大檔案切出上百 MB 的巨片。
TARGET_BLOCK_SIZE = 4 * 1024 * 1024   # 目標每片 4 MiB
MIN_BLOCKS = 1
MAX_BLOCKS = 4096

# 前沿串流：每個 HTTP Range 請求一次涵蓋的連續位元組目標量。
# 區塊仍是進度/續傳的最小單位。握手/跳轉延遲改由預取（_prefetch_run）
# 隱藏，run 不需太大；過大的 run 會在 worker 數多時讓 run 數少於 worker 數，
# 導致分攤不均、預取失效、完成時 worker 驟減造成速率斷崖。64 MiB 為平衡值。
TARGET_RUN_BYTES = 64 * 1024 * 1024

# HTTP 分段引擎的卡死防護（BT 引擎有 _stall_rescue，這裡補上對應機制）。
# worker 在區段失敗後會自行退場；若無人補位，剩餘區塊就永遠沒人下載，
# 任務會無聲卡在 downloading（進度凍結、不報錯也不重試），故需要看門狗。
LINE_FAIL_THRESHOLD = 3           # 同一線路連續失敗達此次數即暫時隔離
LINE_QUARANTINE_SECONDS = 60.0    # 線路隔離時間（秒），期滿自動恢復使用
FAIL_RETRY_BACKOFF = (0.3, 1.0)   # 失敗後退場前的隨機退避（秒），避免緊密重試
WATCHDOG_INTERVAL = 2.0           # 看門狗掃描間隔（秒）
TASK_STALL_TIMEOUT = 60.0         # 全任務連續無進度達此秒數視為停滯一輪
MAX_STALL_ROUNDS = 3              # 連續停滯達此輪數即判定失敗，讓使用者可重試

# 哨兵：代表「沒有任何可用線路」。None 是合法線路（直連），不能拿來當哨兵。
_NO_LINE = object()


class _NativeStopped(Exception):
    """原生 yt-dlp 下載被暫停/取消時，由進度 hook 拋出以中止下載。"""


def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    names = ("B", "KB", "MB", "GB", "TB")
    i = 0
    value = float(size_bytes)
    while value >= 1024 and i < len(names) - 1:
        value /= 1024
        i += 1
    return f"{value:.2f} {names[i]}"


# 伺服器端腳本／下載端點常見的副檔名：這類路徑的 basename 通常不是真實檔名，
# 只是端點名稱（例如 getfile.jsp、download.php）。真名往往藏在 302 轉址後的網址。
_ENDPOINT_EXTS = {
    'jsp', 'jspx', 'php', 'php3', 'php4', 'php5', 'phtml',
    'asp', 'aspx', 'cgi', 'pl', 'do', 'action', 'json', 'html', 'htm',
}


def _filename_from_url(url):
    """取出網址路徑的 basename（忽略 query），無效網址回空字串。"""
    if not url:
        return ''
    try:
        return os.path.basename(unquote(urlparse(url).path))
    except Exception:
        return ''


def _looks_like_endpoint(name):
    """判斷 basename 是否像下載端點而非真實檔名（無副檔名或為腳本副檔名）。"""
    base = (name or '').rsplit('/', 1)[-1]
    if '.' not in base:
        return True
    return base.rsplit('.', 1)[-1].lower() in _ENDPOINT_EXTS


def _better_filename(current, candidate):
    """在 current 像端點、而 candidate 像真實檔名時改用 candidate，否則保留原值。

    用於伺服器未提供 Content-Disposition 時，改由跟隨轉址後的最終網址推導檔名。
    """
    if not candidate or _looks_like_endpoint(candidate):
        return current
    if not current or _looks_like_endpoint(current):
        return candidate
    return current


def probe_url_metadata(url, proxies=None, headers=None, timeout=(3, 5)):
    """對單一 HTTP(S) 連結做輕量 Range 探測，取得真實檔名與檔案大小。

    先直連、失敗再依序走 SOCKS5 代理；兩者拿到的 Content-Length /
    Content-Disposition 相同，故誰先成功就用誰。回傳 {'filename', 'size'}，
    連不上（或非 http/https）回傳 None；size 可能為 0（伺服器不提供長度）。
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ('http', 'https'):
        return None

    req_headers = {
        'User-Agent': 'Multi-Socks-Downloader/1.1',
        'Accept-Encoding': 'identity',
        'Connection': 'close',
        'Range': 'bytes=0-0',
    }
    if headers:
        req_headers.update(headers)

    def _attempt(url, proxy):
        s = requests.Session()
        try:
            s.verify = False
            if proxy:
                host, port = proxy['host'], proxy['port']
                user = proxy.get('username') or ''
                pwd = proxy.get('password') or ''
                if user or pwd:
                    auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}"
                    proxy_url = f"socks5://{auth}@{host}:{port}"
                else:
                    proxy_url = f"socks5://{host}:{port}"
                s.proxies.update({'http': proxy_url, 'https': proxy_url})
            r = s.get(url, headers=req_headers, stream=True,
                      timeout=timeout, allow_redirects=True)
            if r.status_code not in (200, 206):
                r.close()
                return None
            is_html = (r.headers.get('content-type') or '').lower().startswith('text/html')
            if is_html:
                # 串流站台 / 下載頁的 HTML 長度並非檔案大小，避免誤導使用者。
                r.close()
                return {'filename': None, 'size': 0}
            size = 0
            if r.status_code == 206:
                cr = r.headers.get('content-range', '')
                m = re.search(r'/(\d+)\s*$', cr)
                if m:
                    size = int(m.group(1))
                if not size:
                    size = int(r.headers.get('content-length', 0) or 0)
            else:
                size = int(r.headers.get('content-length', 0) or 0)
            filename = DownloadTask._filename_from_headers(r.headers)
            if not filename:
                # 伺服器未提供 Content-Disposition（例如 302 轉址到 S3/CDN 且不帶
                # 該標頭）時，改用跟隨轉址後的最終網址推導檔名。
                filename = _better_filename(None, _filename_from_url(r.url))
            r.close()
            return {'filename': filename, 'size': size}
        except Exception:
            return None
        finally:
            try:
                s.close()
            except Exception:
                pass

    for proxy in ([None] + list(proxies or [])):
        info = _attempt(url, proxy)
        if info is not None:
            return info

    # HTTP 探測失敗時升級 HTTPS 重試，與實際下載的 prepare() 行為一致：
    # 部分伺服器在 HTTP(80) 埠對 Range 請求回 416/失敗，但 HTTPS(443) 正常。
    if scheme == 'http' and urlparse(url).netloc:
        https_url = urlunparse(urlparse(url)._replace(scheme='https'))
        for proxy in ([None] + list(proxies or [])):
            info = _attempt(https_url, proxy)
            if info is not None:
                return info
    return None


# 檔名安全化：移除 Windows 不允許的字元，並裁掉尾端句點/空白。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize_filename(name):
    name = _ILLEGAL_FILENAME_CHARS.sub('_', name).strip().rstrip('. ')
    return (name or 'video')[:200]


def _find_ffmpeg():
    """定位 ffmpeg 執行檔，供原生下載合併視訊+音訊。

    打包後優先找執行檔旁的 ffmpeg\\ffmpeg.exe（或同層 ffmpeg.exe），
    其次退回系統 PATH。Nuitka standalone 的 sys.executable 指向內建
    python.exe，故凍結路徑要用 sys.argv[0]，與 updater.frozen_exe_path() 同因。
    """
    import shutil
    base_dirs = []
    if getattr(sys, 'frozen', False) or globals().get('__compiled__', False):
        argv0 = sys.argv[0] if sys.argv else ''
        p = os.path.abspath(argv0) if argv0 else ''
        if p.lower().endswith('.exe'):
            base_dirs.append(os.path.dirname(p))
    # 原始碼執行（python MultiSocksDownloader.py）時也找 __file__ 所在專案
    # 根目錄下的 ffmpeg，避免明明有 ffmpeg\ffmpeg.exe 卻因不在 PATH 而找不到。
    base_dirs.append(os.path.dirname(os.path.abspath(__file__)))
    for d in base_dirs:
        for cand in (os.path.join(d, 'ffmpeg', 'ffmpeg.exe'),
                     os.path.join(d, 'ffmpeg.exe')):
            if os.path.isfile(cand):
                return cand
    return shutil.which('ffmpeg')


class RateLimiter:
    """全局限速器（token bucket）。rate=0 表示不限速。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._rate = 0.0  # bytes/sec
        self._tokens = 0.0
        self._last = time.monotonic()

    def set_rate(self, rate):
        with self._lock:
            self._rate = max(0.0, float(rate))
            self._tokens = self._rate
            self._last = time.monotonic()

    def get_rate(self):
        with self._lock:
            return self._rate

    def acquire(self, n):
        """寫入前呼叫，n 為本次要寫入的位元組數。限速時會適度休眠。"""
        if n <= 0:
            return
        with self._lock:
            if self._rate <= 0:
                return
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            # 補充 token，並設上限避免暫停後一次爆發
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            if self._tokens >= n:
                self._tokens -= n
                return
            missing = n - self._tokens
            self._tokens = 0.0
            sleep_for = missing / self._rate
        time.sleep(sleep_for)


class DownloadTask:
    MAX_RETRIES = 3
    CHUNK_SIZE = 64 * 1024
    CHUNK_SIZE_LARGE = 256 * 1024
    CHUNK_SIZE_XLARGE = 1024 * 1024
    # 線路停滯判定：在 STALL_WINDOW 秒的滾動窗內收到的位元組少於
    # STALL_MIN_BYTES，即視為該線路卡住（慢速滴流），主動中斷本次 run
    # 交由其他線路接手。設有全域限速時不啟用，避免低速但正常的下載被誤判。
    STALL_WINDOW = 30.0
    STALL_MIN_BYTES = 8 * 1024
    # 單線模式失敗重試的退避基準（秒）：base * 2^(n-1)，上限 10 秒。
    RETRY_BACKOFF = 1.0

    def __init__(self, url, save_dir, filename=None, proxies=None,
                 chunks_per_part=100, threads_per_proxy=3, headers=None,
                 rate_limiter=None, resolve_stream=False, stream_resolver=None,
                 stream_max_height=None, stream_max_fps=None,
                 stream_audio_only=False, stream_format_id=None):
        self.url = url
        self.save_dir = save_dir

        # FTP 相關欄位：is_ftp 由 URL scheme 判定；FTP 的連線參數在 __init__
        # 解析一次，load_progress 不會覆寫它們（url 不變即保持）。
        self.is_ftp = urlparse(self.url).scheme.lower() == 'ftp'
        self._ftp_host = ''
        self._ftp_port = 21
        self._ftp_user = ''
        self._ftp_pass = ''
        self._ftp_path = '/'
        if self.is_ftp:
            (self._ftp_host, self._ftp_port, self._ftp_user,
             self._ftp_pass, self._ftp_path) = parse_ftp_url(self.url)
        self.proxies = proxies or []
        # 分線位元組計數（line_key -> 累計位元組），供 UI 顯示各線速度
        self._line_bytes = {}
        self.threads_per_proxy = max(1, int(threads_per_proxy))
        self.chunks_per_part = int(chunks_per_part or 0)
        self.headers = dict(headers or {})
        self.rate_limiter = rate_limiter
        # 是否在 prepare() 時先用 yt-dlp 解析出單檔直連 URL。三態：
        #   False   = 把網址當一般檔案直接下載；
        #   True    = 先解析串流再下載，解析失敗即中止任務（避免抓成網頁 HTML）；
        #   'auto'  = 先試解析，失敗自動退回一般下載（不中止）。
        self._resolve_stream = resolve_stream
        # 串流解析器（可注入）：預設 yt-dlp 實作；測試可替換成 fake。
        self._stream_resolver = stream_resolver or YtDlpStreamResolver()
        # 原生下載模式：解析器判定只有 DASH/HLS 分段串流時設為 True，
        # 由 yt-dlp 原生下載（視訊+音訊合併）取代 Range 分段引擎。
        self._native_mode = False
        # 畫質/FPS 選擇（僅在 resolve_stream=True 時生效）：
        # max_height 為最高畫素高度（None=最高畫質）、max_fps 為最高幀率
        # （None=自動）、audio_only=True 只要音訊。三者都交由串流解析器
        # 在挑選格式時套用，並持久化以供續傳還原。
        self._stream_max_height = stream_max_height
        self._stream_max_fps = stream_max_fps
        self._stream_audio_only = bool(stream_audio_only)
        # 指定要下載的來源/集數（yt-dlp format_id，如 s3e05）。空字串＝不指定，
        # 走通用 bestvideo+bestaudio 選擇。用於一劇一網址、切來源/集數不改網址
        # 的站（由解析器提供 s<來源>e<集> 形式的 formats）。
        self._stream_format_id = stream_format_id or ''
        self.chunk_size = self.CHUNK_SIZE

        self.filename = filename
        if not self.filename:
            self.filename = self._extract_filename_from_url()
            if not self.filename:
                self.filename = 'download_file'

        self.filepath = os.path.join(save_dir, self.filename)
        self.temp_filepath = f"{self.filepath}.downloading"
        self.progress_filepath = f"{self.filepath}.progress"

        self.total_size = 0
        self.downloaded_size = 0
        self.block_size = 0
        self.block_count = 0
        self.bitmap = bytearray()
        self._completed_bytes = 0
        self._partial = {}  # block_idx -> 區塊內已寫入的位元組數（含續傳偏移，持久化用）
        self.status = 'initialized'
        self.error_message = ''
        # 機器可讀的失敗類別（狀態轉換時的結構化原因），便於跨邊界定位。
        self.error_reason = ''
        self.start_time = None
        self.end_time = None

        self.supports_range = False
        self._single_mode = False
        # 跳轉後的最終 CDN 簽名 URL 快取：後續 run 直接打它、跳過每次的 302 跳轉，
        # 避免每個 64 MiB run 都重新付一次 HuggingFace 簽章/跳轉延遲。
        self._resolved_url = None

        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._stop = threading.Event()
        self._completed = threading.Event()
        self._completion_lock = threading.Lock()
        self._fallback_event = threading.Event()
        self._fatal = False

        self._pool = []
        self._pool_lock = threading.Lock()
        self._block_retries = {}
        self._active_blocks = set()
        # 前沿串流認領位圖：位元 = 1 表示該區塊已被某 worker 佔用（下載中）。
        # 與 bitmap（已完成）分開，讓 _pop_run 能一次認領一段連續區塊。
        self._claimed = bytearray()

        self._workers = []
        self._completion_thread = None
        # HTTP 分段引擎看門狗狀態：目標 worker 數、線路輪派索引、線路健康度。
        self._target_workers = 0
        self._spawn_index = 0
        self._lines = []
        self._line_fail = {}                 # line_key -> 連續失敗次數
        self._line_quarantine_until = {}     # line_key -> 隔離到期（monotonic）

        self._speed_history = deque(maxlen=30)
        self._last_time = time.time()

        self._resumed_size = 0

        # 完成回呼（由 DownloadManager 注入，供 .torrent 下載完成後自動接續 BT）
        self.on_complete = None

        self.task_id = None
        self.threads = []

    # ------------------------------------------------------------------ #
    # 狀態機 + 事件 log
    # ------------------------------------------------------------------ #
    def _log_event(self, event, **ctx):
        """記錄一個統一格式的任務事件，帶 task_id 與階段脈絡，方便串起整條鏈。"""
        extra = " ".join(f"{k}={v!r}" for k, v in ctx.items())
        logger.info("event=%s task_id=%s %s", event, self.task_id, extra)

    def _set_status(self, new_status, reason=''):
        """設定任務狀態並記錄轉換；reason 為機器可讀的結構化失敗類別。"""
        old = self.status
        if old == new_status and not reason:
            return
        if reason:
            self.error_reason = reason
        self.status = new_status
        logger.info("event=status_changed task_id=%s phase=%s->%s reason=%s",
                    self.task_id, old, new_status, reason or '')

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _extract_filename_from_url(self):
        parsed = urlparse(self.url)
        path = unquote(parsed.path)
        name = os.path.basename(path)
        if name:
            return name
        query = parse_qs(parsed.query)
        for key in ('filename', 'name', 'file', 'title', 'download'):
            if key in query and query[key]:
                candidate = query[key][0]
                if candidate:
                    return candidate
        return ''

    @staticmethod
    def _filename_from_headers(headers):
        cd = headers.get('content-disposition', '')
        if not cd:
            return None
        m = re.search(r'filename="([^"]+)"', cd)
        if m:
            return m.group(1)
        m = re.search(r'filename=([^;,\s]+)', cd)
        if m:
            return m.group(1).strip('"')
        m = re.search(r"filename\*=UTF-8''([^;,\s]+)", cd)
        if m:
            return unquote(m.group(1))
        return None

    def _base_headers(self):
        return {
            'User-Agent': 'Multi-Socks-Downloader/1.1',
            'Accept-Encoding': 'identity',
            'Connection': 'keep-alive',
        }

    def _request_headers(self, **extra):
        """合併基礎表頭、使用者自訂表頭（Cookie/Referer/UA 等）與額外表頭。"""
        headers = self._base_headers()
        headers.update(self.headers)
        headers.update(extra)
        return headers

    def _make_session(self, proxy):
        s = requests.Session()
        s.headers.update(self._request_headers())
        if proxy:
            host, port = proxy['host'], proxy['port']
            user = proxy.get('username') or ''
            pwd = proxy.get('password') or ''
            if user or pwd:
                auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}"
                proxy_url = f"socks5://{auth}@{host}:{port}"
            else:
                proxy_url = f"socks5://{host}:{port}"
            s.proxies.update({'http': proxy_url, 'https': proxy_url})
        s.verify = False
        return s

    def _build_lines(self):
        lines = [None]
        for p in self.proxies:
            lines.append(p)
        return lines

    def _line_key(self, line):
        """線路識別鍵：直連為 'direct'，代理為 'proxy:host:port'。"""
        if line is None:
            return 'direct'
        return f"proxy:{line.get('host')}:{line.get('port')}"

    def _line_available(self, line, now):
        """該線路目前是否可用（未被暫時隔離）。"""
        with self._lock:
            until = self._line_quarantine_until.get(self._line_key(line), 0.0)
        return now >= until

    def _next_line(self, now):
        """輪詢挑一條未隔離的線路；全部都在隔離中則回傳 _NO_LINE。"""
        lines = self._lines or [None]
        for _ in range(len(lines)):
            line = lines[self._spawn_index % len(lines)]
            self._spawn_index += 1
            if self._line_available(line, now):
                return line
        return _NO_LINE

    def _note_line_failure(self, proxy):
        """記錄線路失敗；連續失敗達門檻即暫時隔離。

        沒有這層隔離時，一條壞線會不斷被重新派工、反覆失敗同一批區塊，
        最終把無辜區塊的重試次數推到 MAX_RETRIES 而讓整個任務失敗——即使
        其他線路都正常。
        """
        key = self._line_key(proxy)
        with self._lock:
            n = self._line_fail.get(key, 0) + 1
            if n >= LINE_FAIL_THRESHOLD:
                self._line_fail[key] = 0
                self._line_quarantine_until[key] = (
                    time.monotonic() + LINE_QUARANTINE_SECONDS)
                logger.warning(
                    "event=line_quarantined task_id=%s line=%s failures=%s "
                    "cooldown=%.0fs", self.task_id, key, n,
                    LINE_QUARANTINE_SECONDS)
            else:
                self._line_fail[key] = n

    def _note_line_success(self, proxy):
        """線路成功：清掉連續失敗計數與隔離狀態。"""
        key = self._line_key(proxy)
        with self._lock:
            self._line_fail.pop(key, None)
            self._line_quarantine_until.pop(key, None)

    def _stall_check_enabled(self):
        """是否啟用線路停滯判定。設有全域限速時停用，避免低速但正常的下載
        （每個 worker 分到的頻寬本來就少）被誤判為卡住。"""
        return self.rate_limiter is None or self.rate_limiter.get_rate() <= 0

    # ------------------------------------------------------------------ #
    # 進度單一資料源：bitmap，每一位元代表一個區塊，1=已完成（且已 fsync 落盤）。
    # ------------------------------------------------------------------ #
    def _is_block_done(self, idx):
        return bool(self.bitmap[idx >> 3] & (1 << (idx & 7)))

    def _set_block_done(self, idx):
        if self._is_block_done(idx):
            return
        start, end = self._block_bounds(idx)
        self.bitmap[idx >> 3] |= (1 << (idx & 7))
        self._completed_bytes += (end - start)
        # 區塊已完成，清掉它的重試計數：讓「先在壞線上失敗、之後在好線上成功」
        # 的區塊不會把累計失敗帶到 fatal 而誤殺整個任務。
        self._block_retries.pop(idx, None)

    def _block_bounds(self, idx):
        start = idx * self.block_size
        end = min(start + self.block_size, self.total_size)
        return start, end

    def _popcount(self):
        return sum(bin(b).count('1') for b in self.bitmap)

    def _completed_bytes_locked(self):
        return self._completed_bytes

    def _downloaded(self):
        with self._lock:
            if self._single_mode or self.block_count == 0:
                return self.downloaded_size
            # 已完成區塊 + 各進行中區塊已寫入的位元組，讓進度平滑前進
            return self._completed_bytes_locked() + sum(self._partial.values())

    # ------------------------------------------------------------------ #
    # speed
    # ------------------------------------------------------------------ #
    def _record_speed(self):
        now = time.time()
        cur = self._downloaded()
        with self._lock:
            self._speed_history.append((now, cur))
            if now - self._last_time >= 1.0 and len(self._speed_history) >= 2:
                t0, b0 = self._speed_history[0]
                dt = now - t0
                self._speed_history.popleft()
                self._last_time = now
                return ((cur - b0) / dt) if dt > 0 else 0.0
        if len(self._speed_history) >= 2:
            t0, b0 = self._speed_history[0]
            dt = now - t0
            return ((cur - b0) / dt) if dt > 0 else 0.0
        return 0.0

    def get_current_speed(self):
        if self.status != 'downloading':
            return 0.0
        return self._record_speed()

    # ------------------------------------------------------------------ #
    # progress persistence
    # ------------------------------------------------------------------ #
    def _flush_temp_file(self):
        try:
            if os.path.exists(self.temp_filepath):
                with open(self.temp_filepath, 'rb+') as f:
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as e:
            logger.warning("flush_temp_file 失敗: %s", e)

    def save_progress(self):
        try:
            with self._save_lock:
                # 關鍵：先讓資料檔落盤，再記錄 bitmap 與區塊內偏移。
                # 保證進度檔標記的位元組一定真實躺在磁碟上；fsync 統一在此做，
                # 不再於每個區塊完成時逐片 fsync，避免高速下載時磁碟 I/O 成為瓶頸。
                self._flush_temp_file()
                with self._lock:
                    if self._single_mode or self.block_count == 0:
                        bitmap_b64 = ''
                        partials = {}
                        downloaded = self.downloaded_size
                    else:
                        bitmap_b64 = base64.b64encode(bytes(self.bitmap)).decode('ascii')
                        downloaded = self._completed_bytes_locked()
                        partials = {
                            str(i): n for i, n in self._partial.items()
                            if n > 0 and not self._is_block_done(i)
                        }

                    data = {
                        'url': self.url,
                        'save_dir': self.save_dir,
                        'filename': self.filename,
                        'total_size': self.total_size,
                        'downloaded_size': downloaded,
                        'block_size': self.block_size,
                        'block_count': self.block_count,
                        'bitmap': bitmap_b64,
                        'partials': partials,
                        'proxies': self.proxies,
                        'headers': self.headers,
                        'threads_per_proxy': self.threads_per_proxy,
                        'chunks_per_part': self.chunks_per_part,
                        'supports_range': self.supports_range,
                        'status': self.status,
                        'single_mode': self._single_mode,
                        'resolve_stream': self._resolve_stream,
                        'stream_max_height': self._stream_max_height,
                        'stream_max_fps': self._stream_max_fps,
                        'stream_audio_only': self._stream_audio_only,
                        'stream_format_id': self._stream_format_id,
                    }

                tmp = f"{self.progress_filepath}.tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(data, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.progress_filepath)
        except Exception as e:
            logger.warning("儲存進度失敗: %s", e)

    def load_progress(self):
        if not os.path.exists(self.progress_filepath):
            return False
        try:
            with open(self.progress_filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('url') != self.url:
                return False
            if data.get('status') == 'completed':
                return False
            self.save_dir = data.get('save_dir', self.save_dir)
            self.filename = data.get('filename', self.filename)
            self.total_size = data.get('total_size', 0)
            self._compute_chunk_size()
            self.proxies = data.get('proxies', self.proxies)
            self.headers = data.get('headers', self.headers)
            self.threads_per_proxy = data.get('threads_per_proxy', self.threads_per_proxy)
            self.chunks_per_part = data.get('chunks_per_part', self.chunks_per_part)
            self.supports_range = data.get('supports_range', self.supports_range)
            self._single_mode = data.get('single_mode', False)
            self._resolve_stream = data.get('resolve_stream', False)
            self._stream_max_height = data.get('stream_max_height')
            self._stream_max_fps = data.get('stream_max_fps')
            self._stream_audio_only = data.get('stream_audio_only', False)
            self._stream_format_id = (data.get('stream_format_id')
                                      or self._stream_format_id)

            # 舊格式（segments）不支援，視為新下載
            if 'bitmap' not in data:
                return False

            self.block_size = data.get('block_size', 0)
            self.block_count = data.get('block_count', 0)
            b64 = data.get('bitmap', '')
            self.bitmap = bytearray(base64.b64decode(b64)) if b64 else bytearray()

            self.filepath = os.path.join(self.save_dir, self.filename)
            self.temp_filepath = f"{self.filepath}.downloading"
            self.progress_filepath = f"{self.filepath}.progress"

            self.downloaded_size = data.get('downloaded_size', 0)
            self._completed_bytes = self.downloaded_size if not self._single_mode else 0

            # 恢復進行中區塊的區塊內偏移，續傳時從斷點繼續而非整塊重抓
            partials = data.get('partials', {})
            self._partial = {
                int(k): int(v) for k, v in partials.items()
                if 0 <= int(k) < self.block_count and int(v) > 0
            }

            self.status = 'paused'
            return True
        except Exception as e:
            logger.warning("載入進度失敗: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # prepare + probe
    # ------------------------------------------------------------------ #
    def prepare(self):
        if self._resolve_stream:
            if not self._maybe_resolve_stream():
                if self._resolve_stream is True:
                    # 明確要求解析：失敗即中止，避免把網頁 HTML 當成檔案下載。
                    reason = self.error_reason or 'stream_resolve_failed'
                    self._set_status('error', reason=reason)
                    self.error_message = (
                        '無法解析影片串流：' + (self.error_reason or
                         '可能需要登入、Cookie 已過期，或該站不提供單檔直連'))
                    logger.error("event=resolve_failed task_id=%s url=%s reason=%s error=%s",
                                 self.task_id, self.url, reason, self.error_message)
                    return False
                # 'auto' 模式：解析失敗自動退回一般下載（不中止）。
                logger.warning("event=resolve_fallback task_id=%s url=%s reason=%s",
                               self.task_id, self.url,
                               self.error_reason or 'stream_resolve_failed')
        if self._native_mode:
            # 原生 yt-dlp 下載：不需 probe/分段，備妥目錄與唯一檔名即可。
            try:
                os.makedirs(self.save_dir, exist_ok=True)
                self._ensure_unique_filepath()
            except Exception as e:
                logger.exception("準備原生下載時出錯: task_id=%s url=%s",
                                 self.task_id, self.url)
                self._set_status('error', reason='prepare_exception')
                self.error_message = f"準備下載時出錯: {e}"
                return False
            return True
        if self.load_progress():
            self._ensure_temp_file()
            self._rebuild_pool()
            return True

        try:
            os.makedirs(self.save_dir, exist_ok=True)
            if self.is_ftp:
                info = self._probe_ftp()
            else:
                info = self._probe(self._build_lines())
                if info is None and self._upgrade_http_to_https():
                    logger.warning("HTTP 探測失敗，改用 HTTPS 重試: %s", self.url)
                    info = self._probe(self._build_lines())
            if info is None:
                self._set_status('error', reason='probe_failed')
                self.error_message = '無法連接伺服器或取得檔案資訊'
                logger.error("event=probe_failed task_id=%s url=%s",
                             self.task_id, self.url)
                return False

            self.supports_range = info['supports_range']
            # 探測時已跟隨 302，記下最終 CDN 簽名 URL，讓 worker 首次請求就跳過跳轉、
            # 縮短開場的首次握手（從跳轉+TLS 降到只剩 TLS）。
            self._resolved_url = info.get('resolved_url') or None
            if info.get('total_size'):
                self.total_size = info['total_size']
            self._compute_chunk_size()

            hname = self._filename_from_headers(info['headers'])
            if not hname:
                # 無 Content-Disposition 時，改用轉址後最終網址推導檔名：
                # 例如 getfile.jsp 302 到 postgresql-18.6-3-windows-x64.exe。
                hname = _better_filename(
                    self.filename, _filename_from_url(info.get('resolved_url')))
            if hname:
                self.filename = hname
                self.filepath = os.path.join(self.save_dir, self.filename)
                self.temp_filepath = f"{self.filepath}.downloading"
                self.progress_filepath = f"{self.filepath}.progress"

            self._ensure_unique_filepath()

            if self.supports_range and self.total_size > 0:
                self._build_blocks()
                self._ensure_temp_file()
                self._single_mode = False
            else:
                self._single_mode = True
                self.block_count = 0
                self.block_size = 0
                self.bitmap = bytearray()

            self.save_progress()
            return True
        except Exception as e:
            logger.exception("準備下載時出錯: task_id=%s url=%s", self.task_id, self.url)
            self._set_status('error', reason='prepare_exception')
            self.error_message = f"準備下載時出錯: {e}"
            return False

    def _upgrade_http_to_https(self):
        """HTTP 探測失敗時的 HTTPS 升級兜底。

        部分伺服器（如 HiNet 測速檔）在 HTTP(80) 埠對 Range 請求一律回 416，
        但 HTTPS(443) 埠正常支援分段。此方法僅在 http:// 網址下首次嘗試升級，
        回傳 True 表示網址已改為 https://，呼叫端可重新探測；非 http 網址或
        無效網址回傳 False。
        """
        parts = urlparse(self.url)
        if parts.scheme.lower() != 'http' or not parts.netloc:
            return False
        self.url = urlunparse(parts._replace(scheme='https'))
        self._resolved_url = None
        return True

    def _maybe_resolve_stream(self):
        """嘗試把影音網頁網址解析成單檔直連 URL（走可注入的 StreamResolver）。

        只對 http/https、且非明顯直接檔案的網址生效。解析成功時：
        - self._resolved_url 設為直連網址，之後的探測與下載都改用它；
          簽名 URL 會過期，故每次 prepare()（含續傳）都重新解析一次。
        - 合併 resolver 提供的請求標頭（Referer/User-Agent 等，部分 CDN 必填）。
        - 若檔名仍是依 URL 自動推導的值，改用影片標題＋正確副檔名。

        回傳 True 表示可繼續原本的下載流程（解析成功，或網址本就無需解析）；
        回傳 False 表示使用者要求解析但失敗——呼叫端應中止任務，避免把網頁
        HTML 當成檔案下載（例如檔名變成「watch」）。
        """
        self._log_event('resolve_begin', url=self.url)
        if urlparse(self.url).scheme.lower() not in ('http', 'https'):
            return True
        if stream_resolver.is_direct_file(self.url):
            self._log_event('resolve_skip_direct_file', url=self.url)
            return True
        # 把瀏覽器送來的 Cookie/User-Agent 一起帶給 resolver：需登入的影片少了
        # Cookie 時 extract_info 會直接回 None。
        # 解析也帶上代理（socks5h＝遠端 DNS）：本機 DNS 若把影音站台以 RPZ
        # 封鎖/導向廣告頁，Python 直連會拿到自簽憑證而 SSL 失敗；走代理可繞過。
        resolve_proxy = build_socks_proxy_url(
            self.proxies[0] if self.proxies else None)
        result = self._stream_resolver.resolve(
            self.url, headers=self.headers,
            max_height=self._stream_max_height,
            max_fps=self._stream_max_fps,
            audio_only=self._stream_audio_only,
            proxy=resolve_proxy)
        if not result.ok:
            self.error_reason = result.error_kind or 'stream_resolve_failed'
            logger.error("event=resolve_failed task_id=%s url=%s reason=%s error=%s",
                         self.task_id, self.url, self.error_reason, result.error)
            return False

        # DASH/HLS 分段串流：改走 yt-dlp 原生下載（視訊+音訊合併）。
        # 不設 _resolved_url，原生下載仍以原始 URL 交給 yt-dlp 全權處理。
        if result.mode == 'native':
            self._native_mode = True
            title = result.title
            ext = result.ext or '.mp4'
            auto_name = self._extract_filename_from_url() or 'download_file'
            if title and self.filename in (auto_name, _sanitize_filename(title)):
                new_name = _sanitize_filename(title) + ext
                if new_name != self.filename:
                    self.filename = new_name
                    self.filepath = os.path.join(self.save_dir, self.filename)
                    self.temp_filepath = f"{self.filepath}.downloading"
                    self.progress_filepath = f"{self.filepath}.progress"
            self._log_event('resolve_native', url=self.url, title=title)
            return True

        direct_url = result.direct_url
        title = result.title
        ext = result.ext
        self._resolved_url = direct_url
        if result.headers:
            # 解析標頭不覆蓋使用者自訂的同一標頭。
            merged = dict(result.headers)
            merged.update(self.headers)
            self.headers = merged

        # 檔名仍是依 URL 自動推導、或已由對話框用標題預填（無副檔名）時，
        # 才補上副檔名；避免覆蓋使用者自訂的完整檔名。
        auto_name = self._extract_filename_from_url() or 'download_file'
        if title and self.filename in (auto_name, _sanitize_filename(title)):
            new_name = _sanitize_filename(title) + ext
            if new_name != self.filename:
                self.filename = new_name
                self.filepath = os.path.join(self.save_dir, self.filename)
                self.temp_filepath = f"{self.filepath}.downloading"
                self.progress_filepath = f"{self.filepath}.progress"
        self._log_event('resolve_ok', direct_url=direct_url, title=title)
        return True

    def _probe(self, lines):
        headers = self._request_headers()
        headers['Range'] = 'bytes=0-0'
        deadline = time.time() + 15

        def _build_info(r):
            info = {'headers': r.headers,
                    'resolved_url': r.url}
            if r.status_code == 206:
                info['supports_range'] = True
                cr = r.headers.get('content-range', '')
                m = re.search(r'/(\d+)\s*$', cr)
                if m:
                    info['total_size'] = int(m.group(1))
                if not info.get('total_size'):
                    info['total_size'] = int(r.headers.get('content-length', 0) or 0)
            else:
                info['supports_range'] = False
                info['total_size'] = int(r.headers.get('content-length', 0) or 0)
            return info

        # 並行探測所有線路（直連排最前），任一成功即返回，避免逐線路等逾時。
        result = {}
        done = threading.Event()
        lock = threading.Lock()

        def _attempt(proxy):
            if done.is_set():
                return  # 已有線路探測成功，不再發起無謂請求
            try:
                s = self._make_session(proxy)
                if done.is_set():
                    s.close()
                    return
                r = s.get(self._resolved_url or self.url, headers=headers, stream=True,
                          timeout=(5, 8), allow_redirects=True)
                ok = r.status_code in (200, 206)
                info = _build_info(r)
                r.close()
                s.close()
                # 只要拿到 200/206 就算探測成功，不能要求 total_size > 0：
                # 有些伺服器（如 GitHub 的 codeload）回應 chunked、不帶
                # Content-Length，此時 total_size=0 但仍可正常下載（單線串流到 EOF）。
                if ok:
                    with lock:
                        if 'info' not in result:
                            result['info'] = info
                    done.set()
            except Exception as e:
                logger.debug("HTTP 探測線路失敗（line=%s）: %s",
                             self._line_key(proxy), e)

        threads = [threading.Thread(target=_attempt, args=(p,), daemon=True)
                   for p in lines]
        for t in threads:
            t.start()
        done.wait(timeout=max(0.0, deadline - time.time()))

        with lock:
            if 'info' in result:
                info = result['info']
                logger.debug("探測成功: range=%s, total=%s",
                             info['supports_range'], info['total_size'])
                return info
        logger.warning("所有線路探測失敗或逾時")
        return None

    def _probe_ftp(self):
        """並行探測所有線路的 FTP 連線，回傳 {'supports_range','total_size','headers'}。

        與 HTTP 的 _probe 不同：FTP 只要成功登入就視為探測成功，
        即使取不到檔案大小（total_size=0）也回傳，交由 prepare() 依
        supports_range 與 total_size 決定分段或單一模式。
        """
        lines = self._build_lines()
        result = {}
        done = threading.Event()
        lock = threading.Lock()
        deadline = time.time() + 15

        def _attempt(line):
            if done.is_set():
                return  # 已有線路探測成功，不再發起無謂請求
            ftp = None
            try:
                ftp = SocksFTP(line)
                ftp.connect(self._ftp_host, self._ftp_port)
                self._ftp_login(ftp)
                supports_range = True
                try:
                    # REST 0 成功回 350，不支援的伺服器會回 5xx 拋例外
                    ftp.sendcmd('REST 0')
                except Exception:
                    supports_range = False
                total_size = 0
                try:
                    total_size = ftp.size(self._ftp_path) or 0
                except Exception:
                    total_size = 0
                with lock:
                    if 'info' not in result:
                        result['info'] = {
                            'supports_range': supports_range,
                            'total_size': total_size,
                            'headers': {},
                        }
                done.set()
            except Exception as e:
                logger.debug("FTP 探測線路失敗: %s", e)
            finally:
                if ftp is not None:
                    try:
                        ftp.close()
                    except Exception:
                        pass

        threads = [threading.Thread(target=_attempt, args=(p,), daemon=True)
                   for p in lines]
        for t in threads:
            t.start()
        done.wait(timeout=max(0.0, deadline - time.time()))

        with lock:
            info = result.get('info')
        if info is not None:
            logger.debug("FTP 探測成功: range=%s, total=%s",
                         info['supports_range'], info['total_size'])
            return info
        logger.warning("所有 FTP 線路探測失敗或逾時")
        return None

    def _compute_chunk_size(self):
        """依檔案大小挑選讀取區塊大小，減少高速下載時的讀取呼叫開銷。"""
        if self.total_size <= 0:
            self.chunk_size = self.CHUNK_SIZE
        elif self.total_size < 10 * 1024 * 1024:
            self.chunk_size = self.CHUNK_SIZE
        elif self.total_size < 100 * 1024 * 1024:
            self.chunk_size = self.CHUNK_SIZE_LARGE
        else:
            self.chunk_size = self.CHUNK_SIZE_XLARGE

    def _build_blocks(self):
        total = self.total_size
        count = self.chunks_per_part
        if count <= 0:
            # 自適應：依檔案大小定出目標每片 4 MiB，避免大檔案切出上百 MB 的巨片
            count = (total + TARGET_BLOCK_SIZE - 1) // TARGET_BLOCK_SIZE
            count = max(MIN_BLOCKS, min(MAX_BLOCKS, count))
        if count > total:
            count = total
        if count < 1:
            count = 1
        self.block_count = int(count)
        self.block_size = (total + self.block_count - 1) // self.block_count
        self.bitmap = bytearray((self.block_count + 7) // 8)

    def _ensure_temp_file(self):
        try:
            if not os.path.exists(self.temp_filepath):
                with open(self.temp_filepath, 'wb') as f:
                    if self.total_size > 0:
                        f.truncate(self.total_size)
            else:
                if self.total_size > 0 and os.path.getsize(self.temp_filepath) < self.total_size:
                    with open(self.temp_filepath, 'r+b') as f:
                        f.truncate(self.total_size)
        except Exception as e:
            logger.warning("建立暫存檔失敗: %s", e)

    def _ensure_unique_filepath(self):
        if not os.path.exists(self.filepath):
            return
        base, ext = os.path.splitext(self.filename)
        counter = 1
        while True:
            new_name = f"{base}_{counter}{ext}"
            new_path = os.path.join(self.save_dir, new_name)
            if not os.path.exists(new_path):
                self.filename = new_name
                self.filepath = new_path
                self.temp_filepath = f"{self.filepath}.downloading"
                self.progress_filepath = f"{self.filepath}.progress"
                return
            counter += 1

    def _rebuild_pool(self):
        with self._pool_lock:
            self._pool = [i for i in range(self.block_count)
                          if not self._is_block_done(i)]

    # ------------------------------------------------------------------ #
    # download workers
    # ------------------------------------------------------------------ #
    def _pop_block(self):
        with self._pool_lock:
            if not self._pool:
                return None
            # 從頭取出（pop(0)），讓下載由檔案開頭往尾端進行，
            # 進度條才能由左往右填滿，而非由右往左。
            return self._pool.pop(0)

    def _queue_block(self, idx):
        with self._pool_lock:
            self._pool.append(idx)

    def _is_block_claimed(self, idx):
        if not self._claimed:
            return False
        return bool(self._claimed[idx >> 3] & (1 << (idx & 7)))

    def _set_block_claimed(self, idx):
        if len(self._claimed) == 0:
            self._claimed = bytearray((self.block_count + 7) // 8)
        self._claimed[idx >> 3] |= (1 << (idx & 7))

    def _clear_block_claimed(self, idx):
        if not self._claimed:
            return
        self._claimed[idx >> 3] &= ~(1 << (idx & 7))

    def _reset_claims(self):
        """新世代開始前清空所有認領位（進行中區塊一律視為未佔用）。"""
        with self._lock:
            self._claimed = bytearray((self.block_count + 7) // 8)

    def _pop_run(self):
        """認領一段連續、尚未完成且未被佔用的區塊，回傳 (start, end) 或 None。

        與逐塊 _pop_block 的差異：一次抓一大段連續區塊（約 TARGET_RUN_BYTES），
        讓 worker 用單一 Range 請求串流整段，避免每塊都付一次簽章/跳轉延遲。
        """
        max_blocks = max(1, TARGET_RUN_BYTES // self.block_size) if self.block_size else 1
        with self._lock:
            n = self.block_count
            start = 0
            while start < n and (self._is_block_done(start) or self._is_block_claimed(start)):
                start += 1
            if start >= n:
                return None
            end = start
            while end + 1 < n and (end - start + 1) < max_blocks:
                nxt = end + 1
                if self._is_block_done(nxt) or self._is_block_claimed(nxt):
                    break
                end = nxt
            for b in range(start, end + 1):
                self._set_block_claimed(b)
            return (start, end)

    def _worker(self, proxy, stop):
        # 初始去同步：隨機延遲一小段，避免多線同時首次握手形成併發共振。
        time.sleep(random.uniform(0, 0.2))
        session = self._make_session(proxy)
        try:
            run = self._pop_run()
            if run is None:
                return
            state = self._request_run(run[0], run[1], session, stop, proxy)
            while not stop.is_set():
                if state in ('done', 'fail'):
                    if state == 'fail' and not stop.is_set():
                        # 失敗退避：worker 退場後由看門狗補位；退避可避免瞬斷
                        # 的線路被反覆補位時緊密重試，瞬間把區塊重試次數推滿。
                        time.sleep(random.uniform(*FAIL_RETRY_BACKOFF))
                    break
                if state == 'fallback':
                    self._fallback_event.set()
                    break
                _, start_idx, end_idx, req_start, end, r = state

                # 認領下一個 run 並在背景預取：把握手/跳轉延遲藏進本次串流。
                nxt = self._pop_run()
                box = {}
                thr = None
                if nxt is not None:
                    thr = threading.Thread(
                        target=self._prefetch_run,
                        args=(nxt[0], nxt[1], proxy, stop, box), daemon=True)
                    thr.start()

                # 寫入當前 run
                with self._lock:
                    for b in range(start_idx, end_idx + 1):
                        self._active_blocks.add(b)
                try:
                    result = self._write_run(
                        start_idx, end_idx, req_start, end, r, stop, proxy)
                finally:
                    with self._lock:
                        for b in range(start_idx, end_idx + 1):
                            self._active_blocks.discard(b)
                if result == 'ok':
                    self._note_line_success(proxy)
                try:
                    session.close()
                except Exception:
                    pass
                session = None

                if result == 'ok' and self._all_blocks_done():
                    if thr is not None:
                        thr.join(timeout=1)
                    break

                # 取用預取的下一個 run；無下一個或已被取消則結束。
                if thr is None or stop.is_set():
                    break
                thr.join()
                got = box.get('result')
                if got is None:
                    break
                session, state = got
        finally:
            try:
                if session is not None:
                    session.close()
            except Exception:
                pass

    def _request_run(self, start_idx, end_idx, session, stop, proxy=None):
        """發起一個 run 的 Range 請求。回傳：
        ('stream', start_idx, end_idx, req_start, end, r) 可開始串流；
        'fallback'（伺服器忽略 Range）、'done'（已完成）、'fail'（失敗已處理）。
        """
        start, _ = self._block_bounds(start_idx)
        _, end_excl = self._block_bounds(end_idx)
        end = end_excl - 1  # inclusive
        if start >= self.total_size:
            return 'done'

        # 續傳：run 的第一個區塊從區塊內已寫入的偏移繼續，其餘區塊均為全新。
        with self._lock:
            off = self._partial.get(start_idx, 0)
        req_start = start + off
        if req_start > end:
            with self._lock:
                for b in range(start_idx, end_idx + 1):
                    if not self._is_block_done(b):
                        self._set_block_done(b)
                        self._partial.pop(b, None)
            return 'done'

        headers = self._request_headers()
        headers['Range'] = f"bytes={req_start}-{end}"

        # 優先用已解析的 CDN 簽名 URL：跳過每個 run 都要重走一次的 302 跳轉。
        url = self._resolved_url or self.url
        try:
            r = session.get(url, headers=headers, stream=True,
                            timeout=(15, 60), allow_redirects=True)
        except Exception as e:
            self._handle_run_failure(
                start_idx, end_idx, start_idx, f"連接失敗: {e}", stop, proxy)
            return 'fail'

        if r.status_code == 206:
            # 記住最終 URL（簽名 CDN），供後續 run 直接複用、省去跳轉延遲。
            if r.url and r.url != self.url:
                self._resolved_url = r.url
            return ('stream', start_idx, end_idx, req_start, end, r)
        if r.status_code == 200:
            r.close()
            self._handle_run_fallback(start_idx, end_idx)
            return 'fallback'
        if r.status_code == 416:
            r.close()
            self._handle_run_failure(
                start_idx, end_idx, start_idx, "HTTP 416：範圍請求被拒絕",
                stop, proxy, line_fault=False)
            return 'fail'
        r.close()
        # 403/401 可能是簽名 URL 已過期：清掉快取後用原始 URL 重解析一次。
        if self._resolved_url and r.status_code in (401, 403):
            self._resolved_url = None
            return self._request_run(start_idx, end_idx, session, stop, proxy)
        self._handle_run_failure(
            start_idx, end_idx, start_idx, f"HTTP {r.status_code}",
            stop, proxy, line_fault=False)
        return 'fail'

    def _prefetch_run(self, start_idx, end_idx, proxy, stop, box):
        """背景預取下一個 run：用獨立 session 發起請求，把握手延遲藏進串流。"""
        session = self._make_session(proxy)
        if stop.is_set():
            box['result'] = (session, 'fail')
            return
        state = self._request_run(start_idx, end_idx, session, stop, proxy)
        box['result'] = (session, state)

    def _write_run(self, start_idx, end_idx, req_start, end, r, stop, proxy=None):
        need = end - req_start + 1
        key = self._line_key(proxy)
        pos = req_start
        cur_idx = start_idx
        # 停滯偵測窗：窗內收到的位元組過少即判定這條線卡住（例如慢速滴流），
        # 主動中斷本次 run、釋放認領交由其他線路接手，而不是無限期佔著區塊。
        win_start = time.monotonic()
        win_bytes = 0
        try:
            with open(self.temp_filepath, 'r+b') as f:
                f.seek(req_start)
                for chunk in r.iter_content(self.chunk_size):
                    if stop.is_set():
                        break
                    if not chunk:
                        continue
                    remaining = end - pos + 1
                    if remaining <= 0:
                        break
                    data = chunk[:remaining]
                    if self.rate_limiter is not None:
                        self.rate_limiter.acquire(len(data))
                    f.write(data)
                    pos += len(data)
                    win_bytes += len(data)
                    with self._lock:
                        self._line_bytes[key] = self._line_bytes.get(key, 0) + len(data)
                        # 邊跨過區塊邊界就標記該區塊完成，直到停在目前這個未完區塊
                        while cur_idx <= end_idx:
                            bstart, bend = self._block_bounds(cur_idx)
                            if pos >= bend:
                                if not self._is_block_done(cur_idx):
                                    self._set_block_done(cur_idx)
                                    self._partial.pop(cur_idx, None)
                                cur_idx += 1
                            else:
                                self._partial[cur_idx] = pos - bstart
                                break
                    if self._stall_check_enabled():
                        now = time.monotonic()
                        if now - win_start >= self.STALL_WINDOW:
                            if win_bytes < self.STALL_MIN_BYTES:
                                r.close()
                                return self._handle_run_failure(
                                    start_idx, end_idx, cur_idx,
                                    f"線路停滯: {win_bytes} bytes/"
                                    f"{now - win_start:.0f}s", stop, proxy)
                            win_start = now
                            win_bytes = 0
                r.close()
                f.flush()
            # 不再逐片 fsync：落盤統一由 save_progress()（每 5 秒）與
            # complete_download() 負責，避免大頻寬下磁碟 I/O 成為瓶頸。
            if stop.is_set():
                # 保留 _partial 讓續傳能從區塊內斷點繼續，不重抓整段。
                return 'fail'
            if pos - req_start >= need:
                return 'ok'
            return self._handle_run_failure(
                start_idx, end_idx, cur_idx,
                f"區段下載不完整: {pos - req_start}/{need}", stop, proxy)
        except Exception as e:
            try:
                r.close()
            except Exception:
                pass
            return self._handle_run_failure(
                start_idx, end_idx, cur_idx, f"寫入失敗: {e}", stop, proxy)

    def _handle_run_fallback(self, start_idx, end_idx):
        """伺服器忽略 Range（回 200）：釋放本段認領，交由呼叫端切換單線模式。"""
        with self._lock:
            for b in range(start_idx, end_idx + 1):
                if not self._is_block_done(b):
                    self._clear_block_claimed(b)
        return 'fallback'

    def _handle_run_failure(self, start_idx, end_idx, failed_idx, reason, stop,
                            proxy=None, line_fault=True):
        """段下載失敗：釋放尚未完成區塊的認領，並對失敗所在區塊計一次重試。

        line_fault=False 表示失敗與線路無關（例如伺服器回 4xx/5xx、416），
        不列入線路健康計數，避免把正常線路誤判成壞線而隔離。
        """
        with self._lock:
            for b in range(start_idx, end_idx + 1):
                if not self._is_block_done(b):
                    self._clear_block_claimed(b)
        if line_fault:
            self._note_line_failure(proxy)
        if stop.is_set():
            return 'fail'
        with self._lock:
            retries = self._block_retries.get(failed_idx, 0) + 1
            self._block_retries[failed_idx] = retries
        logger.warning("區段 %s-%s 失敗於區塊 %s: %s (重試 %s/%s)",
                       start_idx, end_idx, failed_idx, reason, retries, self.MAX_RETRIES)
        if retries >= self.MAX_RETRIES:
            self.error_message = f"區塊 {failed_idx} 下載失敗: {reason}"
            self._fatal = True
            self._set_status('error', reason='run_failed')
            stop.set()
            return 'fail'
        return 'fail'

    def _handle_block_failure(self, idx, reason, stop):
        if stop.is_set():
            return 'fail'
        with self._lock:
            retries = self._block_retries.get(idx, 0) + 1
            self._block_retries[idx] = retries
        logger.warning("區塊 %s %s (重試 %s/%s)", idx, reason, retries, self.MAX_RETRIES)
        if retries >= self.MAX_RETRIES:
            self.error_message = f"區塊 {idx} 下載失敗: {reason}"
            self._fatal = True
            self._set_status('error', reason='block_failed')
            stop.set()
            return 'fail'
        self._queue_block(idx)
        return 'fail'

    def _all_blocks_done(self):
        with self._lock:
            return self._popcount() == self.block_count

    def _single_worker(self, proxy, stop):
        """單線模式（伺服器不支援 Range）：整檔下載，失敗自動重試。

        單線模式無法續傳，每次重試都從頭寫入暫存檔；重試上限沿用
        MAX_RETRIES，並以指數退避避免對故障線路緊密重試。只有成功才收尾，
        失敗保留 error 狀態（不再把半成品誤標為完成）。
        """
        succeeded = False
        attempt = 0
        while not stop.is_set() and attempt < self.MAX_RETRIES:
            attempt += 1
            if self._single_attempt(proxy, stop):
                succeeded = True
                break
            if stop.is_set():
                break
            delay = min(10.0, self.RETRY_BACKOFF * (2 ** (attempt - 1)))
            logger.warning("event=single_retry task_id=%s attempt=%s/%s "
                           "delay=%.1fs error=%s",
                           self.task_id, attempt, self.MAX_RETRIES, delay,
                           self.error_message)
            if stop.wait(delay):
                break
        if succeeded:
            # 只有仍是當前世代（stop 未被新 start() 取代）才允許完成收尾，
            # 避免暫停/恢復後舊 worker 誤把新世代的下載標記為完成。
            if stop is self._stop:
                self.complete_download()
        elif not stop.is_set():
            self._set_status('error', reason='single_failed')

    def _single_attempt(self, proxy, stop):
        """單線模式的一次完整嘗試；成功回 True，失敗回 False 並設 error_message。"""
        session = self._make_session(proxy)
        r = None
        try:
            headers = self._request_headers()
            r = session.get(self._resolved_url or self.url, headers=headers,
                            stream=True, timeout=(15, 60), allow_redirects=True)
            if r.status_code != 200:
                self.error_message = f"HTTP {r.status_code}"
                r.close()
                return False
            cl = r.headers.get('content-length')
            with self._lock:
                if cl:
                    self.total_size = int(cl)
                # 每次重試都從頭寫入，位元組計數歸零避免進度虛胖。
                self.downloaded_size = 0
            bytes_since_fsync = 0
            key = self._line_key(proxy)
            win_start = time.monotonic()
            win_bytes = 0
            with open(self.temp_filepath, 'wb') as f:
                for chunk in r.iter_content(self.chunk_size):
                    if stop.is_set():
                        return False
                    if not chunk:
                        continue
                    if self.rate_limiter is not None:
                        self.rate_limiter.acquire(len(chunk))
                    f.write(chunk)
                    with self._lock:
                        self.downloaded_size += len(chunk)
                        self._line_bytes[key] = self._line_bytes.get(key, 0) + len(chunk)
                    bytes_since_fsync += len(chunk)
                    win_bytes += len(chunk)
                    if bytes_since_fsync >= 8 * 1024 * 1024:
                        f.flush()
                        os.fsync(f.fileno())
                        bytes_since_fsync = 0
                    if self._stall_check_enabled():
                        now = time.monotonic()
                        if now - win_start >= self.STALL_WINDOW:
                            if win_bytes < self.STALL_MIN_BYTES:
                                self.error_message = (
                                    f"傳輸停滯: {win_bytes} bytes/"
                                    f"{now - win_start:.0f}s")
                                r.close()
                                return False
                            win_start = now
                            win_bytes = 0
            r.close()
            return True
        except Exception as e:
            if r is not None:
                try:
                    r.close()
                except Exception:
                    pass
            self.error_message = str(e)
            logger.error("event=single_attempt_failed task_id=%s error=%s",
                         self.task_id, e)
            return False
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _native_worker(self, stop):
        """DASH/HLS 串流的原生下載：整段交給 yt-dlp（單一 SOCKS5 代理 + ffmpeg 合併）。

        路徑 A：只走單一代理（優先第一條可用 SOCKS5，無代理則直連），
        由 yt-dlp 依 bestvideo*+bestaudio 選軌並用 ffmpeg 合併成 MP4。
        進度透過 progress_hook 對映回 task 的 total_size / downloaded_size，
        讓 UI 的 get_progress() 持續更新；pause/cancel 靠 stop 事件中止。
        """
        try:
            import yt_dlp
        except ImportError:
            self._set_status('error', reason='ytdlp_missing')
            self.error_message = '未安裝 yt-dlp，無法原生下載影音串流'
            return

        proxy = self.proxies[0] if self.proxies else None
        proxy_url = None
        if proxy:
            host = proxy.get('host')
            port = proxy.get('port')
            user = proxy.get('username') or ''
            pwd = proxy.get('password') or ''
            if user or pwd:
                proxy_url = f"socks5://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
            else:
                proxy_url = f"socks5://{host}:{port}"

        ffmpeg = _find_ffmpeg()

        def _hook(d):
            if stop.is_set():
                raise _NativeStopped()
            if d.get('status') == 'downloading':
                downloaded = d.get('downloaded_bytes')
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                with self._lock:
                    if downloaded is not None:
                        self.downloaded_size = int(downloaded)
                    if total:
                        self.total_size = int(total)
                    self._single_mode = True

        if self._stream_format_id:
            # 使用者指定了來源/集數（如 s3e05）：直接用該 format_id，不再套用
            # 通用 bestvideo+bestaudio 選擇器。
            fmt = self._stream_format_id
        else:
            fmt = build_native_format_selector(
                max_height=self._stream_max_height,
                max_fps=self._stream_max_fps,
                audio_only=self._stream_audio_only)
        opts = {
            'format': fmt,
            'merge_output_format': 'm4a' if self._stream_audio_only else 'mp4',
            'outtmpl': self.filepath,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'progress_hooks': [_hook],
            'socket_timeout': 15,
            'retries': 3,
            'continuedl': True,
        }
        if proxy_url:
            opts['proxy'] = proxy_url
        if ffmpeg:
            opts['ffmpeg_location'] = ffmpeg
        else:
            logger.warning("未找到 ffmpeg，DASH/HLS 合併將失敗: task_id=%s", self.task_id)
        if self.headers:
            # 帶上瀏覽器送來的 UA/Referer（部分 CDN 必填），但不要把 Cookie
            # 當 header 傳——yt-dlp 會警告安全風險，YouTube 偵測到這類 Cookie
            # header 還會觸發 bot 檢查（The page needs to be reloaded）。
            hdrs = {k: v for k, v in self.headers.items()
                    if k.lower() not in ('cookie', 'cookie2')}
            if hdrs:
                opts['http_headers'] = hdrs

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([self.url])
        except _NativeStopped:
            return
        except Exception as e:
            if not stop.is_set():
                self._set_status('error', reason='native_failed')
                self.error_message = str(e)
                logger.error("event=native_failed task_id=%s url=%s error=%s",
                             self.task_id, self.url, e)
            return

        if stop is self._stop:
            self.complete_download()

    # ------------------------------------------------------------------ #
    # FTP workers
    # ------------------------------------------------------------------ #
    def _ftp_login(self, ftp):
        """登入 FTP：URL 有帳密則用帳密，否則匿名登入；一律切到二進位模式。

        REST/RETR 都需要二進位模式（ASCII 模式會拒絕 REST 且會轉譯換行），
        而直接使用 transfercmd 不會像 retrbinary 那樣自動送 TYPE I，故在此統一送出。
        """
        if self._ftp_user or self._ftp_pass:
            ftp.login(self._ftp_user, self._ftp_pass)
        else:
            ftp.login()
        ftp.voidcmd('TYPE I')

    def _ftp_worker(self, line, stop):
        """單一線路的 FTP worker：反覆 pop 區塊，每個區塊以獨立連線下載。"""
        while not stop.is_set():
            idx = self._pop_block()
            if idx is None:
                break
            if self._is_block_done(idx):
                continue
            with self._lock:
                self._active_blocks.add(idx)
            try:
                result = self._ftp_download_block(idx, line, stop)
            finally:
                with self._lock:
                    self._active_blocks.discard(idx)
            if result == 'ok':
                if self._all_blocks_done():
                    break
            elif result == 'fail' and stop.is_set():
                break

    def _ftp_download_block(self, idx, line, stop):
        """下載單一 FTP 區塊：REST 定位到區塊內偏移、RETR 後只讀取本區塊位元組。

        每個區塊都以獨立 FTP 連線抓取（控制+資料用完即丟），避免 ftplib
        控制連線在中途中斷資料傳輸後、回應串流狀態難以同步的問題。
        """
        start, end = self._block_bounds(idx)
        end -= 1  # inclusive
        full_len = end - start + 1
        if start >= self.total_size:
            return 'ok'

        with self._lock:
            off = self._partial.get(idx, 0)
        req_start = start + off
        if req_start > end:
            with self._lock:
                self._set_block_done(idx)
                self._partial.pop(idx, None)
            return 'ok'

        need = full_len - off  # 本輪要抓取的位元組數
        ftp = None
        try:
            ftp = SocksFTP(line)
            ftp.connect(self._ftp_host, self._ftp_port)
            self._ftp_login(ftp)
            conn = ftp.transfercmd('RETR ' + self._ftp_path, rest=req_start)
            fp = conn.makefile('rb')
            pos = req_start
            key = self._line_key(line)
            # 累計本區塊已寫入但尚未回報全域狀態的位元組，降低多執行緒下的鎖搶佔。
            acc_bytes = 0
            try:
                with open(self.temp_filepath, 'r+b') as f:
                    f.seek(req_start)
                    remaining = need
                    while remaining > 0 and not stop.is_set():
                        chunk = fp.read(min(self.chunk_size, remaining))
                        if not chunk:
                            break
                        if self.rate_limiter is not None:
                            self.rate_limiter.acquire(len(chunk))
                        f.write(chunk)
                        pos += len(chunk)
                        remaining -= len(chunk)
                        acc_bytes += len(chunk)
                        if acc_bytes >= 1024 * 1024:
                            with self._lock:
                                self._partial[idx] = pos - start  # 累計偏移
                                self._line_bytes[key] = self._line_bytes.get(key, 0) + acc_bytes
                            acc_bytes = 0
                if acc_bytes > 0:
                    with self._lock:
                        self._partial[idx] = pos - start
                        self._line_bytes[key] = self._line_bytes.get(key, 0) + acc_bytes
            finally:
                fp.close()
                conn.close()
        except Exception as e:
            with self._lock:
                self._partial.pop(idx, None)
            return self._handle_block_failure(
                idx, f"FTP 區塊下載失敗: {e}", stop)
        finally:
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass

        if stop.is_set():
            with self._lock:
                self._partial.pop(idx, None)
            return 'fail'

        if pos - start >= full_len:
            with self._lock:
                self._set_block_done(idx)
                self._partial.pop(idx, None)
            return 'ok'

        with self._lock:
            self._partial.pop(idx, None)
        return self._handle_block_failure(
            idx, f"FTP 區塊下載不完整: {pos - start}/{full_len}", stop)

    def _ftp_single_worker(self, line, stop):
        """FTP 單一模式：整檔串流下載（不支援 REST 或取不到大小時）。"""
        class _Stopped(Exception):
            pass

        ftp = None
        try:
            ftp = SocksFTP(line)
            ftp.connect(self._ftp_host, self._ftp_port)
            self._ftp_login(ftp)
            try:
                size = ftp.size(self._ftp_path)
                if size:
                    with self._lock:
                        self.total_size = size
            except Exception:
                pass

            key = self._line_key(line)
            with open(self.temp_filepath, 'wb') as f:
                def _write(chunk):
                    if stop.is_set():
                        raise _Stopped()
                    if self.rate_limiter is not None:
                        self.rate_limiter.acquire(len(chunk))
                    f.write(chunk)
                    with self._lock:
                        self.downloaded_size += len(chunk)
                        self._line_bytes[key] = self._line_bytes.get(key, 0) + len(chunk)

                ftp.retrbinary('RETR ' + self._ftp_path, _write,
                               blocksize=self.chunk_size)
        except _Stopped:
            pass
        except Exception as e:
            if not stop.is_set():
                self._set_status('error', reason='ftp_single_worker_exception')
                self.error_message = str(e)
                logger.error("event=ftp_single_worker_failed task_id=%s error=%s",
                             self.task_id, e)
        finally:
            if ftp is not None:
                try:
                    ftp.close()
                except Exception:
                    pass
        # 未中斷（未被暫停）且仍是當前世代才收尾，避免把半成品當完成
        if not stop.is_set() and stop is self._stop:
            self.complete_download()

    def _completion_loop(self, stop):
        last_save = time.time()
        while not stop.is_set() and self.status == 'downloading':
            time.sleep(0.5)
            if self._all_blocks_done():
                break
            if time.time() - last_save >= 5:
                self.save_progress()
                last_save = time.time()
        if stop is self._stop:
            self.complete_download()

    def _spawn_http_workers(self, stop, count):
        """建立 count 個 HTTP worker，線路依序輪流分配（供啟動與看門狗補開）。"""
        lines = self._build_lines() or [None]
        for _ in range(count):
            line = self._next_line(time.monotonic())
            if line is _NO_LINE:
                # 所有線路都在隔離中：不派工，等隔離期滿由看門狗再試。
                return
            t = threading.Thread(target=self._worker, args=(line, stop), daemon=True)
            self._workers.append(t)
            self.threads.append(t)
            t.start()

    def _maintain_workers(self, stop):
        """回收已結束的 worker；若一個都不剩且仍有未完成區塊，補開新 worker。

        回傳 True 表示這次有補開。補開前先清空認領位，清掉可能由已退場
        worker 或孤兒預取執行緒殘留的認領，讓新 worker 能重新認領區塊。
        """
        alive = [t for t in self._workers if t.is_alive()]
        if len(alive) != len(self._workers):
            self._workers = alive
            self.threads = [t for t in self.threads if t.is_alive()]
        if alive:
            return False
        self._reset_claims()
        self._spawn_http_workers(stop, self._target_workers)
        logger.warning("event=workers_respawned task_id=%s count=%s",
                       self.task_id, self._target_workers)
        return True

    def _supervisor_loop(self, stop):
        """HTTP 分段引擎的看門狗。

        worker 連續失敗後會退場；若全部 worker 都結束而仍有未完成區塊，
        就再也沒人會下載，任務會永遠停在 downloading（進度凍結、不報錯也
        不重試）。此迴圈定期把 worker 補回目標數，並在整條任務連續多輪
        完全沒有位元組進展時判定停滯失敗，讓使用者能重試而不是無聲卡住。
        """
        last_save = time.time()
        last_progress = self._downloaded()
        last_change = time.time()
        stall_rounds = 0
        while not stop.is_set() and self.status == 'downloading':
            time.sleep(0.5)
            if self._all_blocks_done():
                break
            now = time.time()
            if now - last_save >= 5:
                self.save_progress()
                last_save = now
            self._maintain_workers(stop)
            cur = self._downloaded()
            if cur > last_progress:
                last_progress = cur
                last_change = now
                stall_rounds = 0
                continue
            if now - last_change < TASK_STALL_TIMEOUT:
                continue
            stall_rounds += 1
            last_change = now
            logger.warning(
                "event=task_stall task_id=%s round=%s/%s downloaded=%s/%s",
                self.task_id, stall_rounds, MAX_STALL_ROUNDS, cur,
                self.total_size)
            if stall_rounds >= MAX_STALL_ROUNDS:
                self.error_message = f"下載停滯：連續 {stall_rounds} 輪無進度"
                self._fatal = True
                self._set_status('error', reason='stalled')
                stop.set()
                break
        if stop is self._stop:
            self.complete_download()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        self._log_event('start_begin', url=self.url)
        self._set_status('downloading')
        self.error_message = ''
        self.error_reason = ''
        self._fatal = False

        if not self.prepare():
            logger.error("event=prepare_failed task_id=%s url=%s status=%s error=%s",
                         self.task_id, self.url, self.status, self.error_message)
            return False

        # prepare()/load_progress() 可能把 status 改回 'paused'，必須再次確認為下載中
        self._set_status('downloading')
        # 每次 start() 建立新的停止事件（世代分離）：舊 worker 捕捉的是上一代的
        # 已 set 的事件，會自行退出，不會因 clear() 而復活造成重複下載。
        self._stop = threading.Event()
        stop = self._stop
        self._completed.clear()
        self._fallback_event.clear()
        self._resumed_size = self._downloaded()
        self.start_time = time.time()
        self._last_time = time.time()
        self._speed_history.clear()

        if self.is_ftp:
            return self._start_ftp(stop)

        if self._native_mode:
            self.threads = []
            t = threading.Thread(target=self._native_worker, args=(stop,), daemon=True)
            self.threads.append(t)
            t.start()
            return True

        if self._single_mode:
            proxy = self.proxies[0] if self.proxies else None
            self.threads = []
            t = threading.Thread(target=self._single_worker, args=(proxy, stop), daemon=True)
            self.threads.append(t)
            t.start()
            return True

        self._reset_claims()
        self._workers = []
        self.threads = []
        self._lines = self._build_lines()
        self._spawn_index = 0
        self._line_fail = {}
        self._line_quarantine_until = {}
        # 工作者數不應超過剩餘區塊數，避免空轉的線程
        max_workers = len(self._lines) * self.threads_per_proxy
        if self.block_count > 0:
            max_workers = min(max_workers, self.block_count)
        self._target_workers = max(1, max_workers)
        # 起始即補滿 worker；之後 worker 失敗退場時由看門狗補位。
        self._spawn_http_workers(stop, self._target_workers)

        # 看門狗：定期存進度、補開已結束的 worker，並在全任務長期零進度時
        # 判定停滯失敗。沒有它，worker 全數退場後任務會無聲卡在 downloading。
        self._completion_thread = threading.Thread(
            target=self._supervisor_loop, args=(stop,), daemon=True)
        self.threads.append(self._completion_thread)
        self._completion_thread.start()
        return True

    def _start_ftp(self, stop):
        """啟動 FTP 下載：分段模式每條線路多個 worker，單一模式一條連線。"""
        lines = self._build_lines()
        self.threads = []
        self._workers = []

        if self._single_mode:
            line = lines[0] if lines else None
            t = threading.Thread(
                target=self._ftp_single_worker, args=(line, stop), daemon=True)
            self.threads.append(t)
            t.start()
            return True

        self._rebuild_pool()
        # 每條線路開 threads_per_proxy 個 worker，各自建立獨立 FTP 連線抓不同
        # 區塊，以多連線並行榨取單一線路（如 5G）的頻寬。工作者數不超過剩餘
        # 區塊數，避免空轉的線程。
        max_workers = self.block_count if self.block_count > 0 else len(lines) * self.threads_per_proxy
        spawned = 0
        for line in lines:
            for _ in range(self.threads_per_proxy):
                if spawned >= max_workers:
                    break
                t = threading.Thread(
                    target=self._ftp_worker, args=(line, stop), daemon=True)
                self._workers.append(t)
                self.threads.append(t)
                t.start()
                spawned += 1
            if spawned >= max_workers:
                break

        self._completion_thread = threading.Thread(
            target=self._completion_loop, args=(stop,), daemon=True)
        self.threads.append(self._completion_thread)
        self._completion_thread.start()
        return True

    def pause(self):
        if self.status != 'downloading':
            return False
        self._stop.set()
        self._set_status('paused')
        for t in self._workers:
            if t.is_alive():
                t.join(timeout=1.0)
        # 原生下載不走 .progress 續傳（由 yt-dlp .part 接手），勿寫出誤導的進度檔
        if not self._native_mode:
            self.save_progress()
        return True

    def resume(self):
        if self.status != 'paused':
            return False
        return self.start()

    def retry(self):
        """從錯誤/暫停狀態重試：清空區塊重試計數與 fatal 標記後重新啟動。"""
        if self.status not in ('error', 'paused'):
            return False
        self._fatal = False
        self._block_retries = {}
        self.error_message = ''
        self.error_reason = ''
        self._set_status('initialized')
        return self.start()

    def cancel(self):
        self._stop.set()
        self._set_status('canceled')
        for t in self.threads:
            if t is not threading.current_thread() and t.is_alive():
                t.join(timeout=1)
        for p in (self.temp_filepath, self.progress_filepath):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        # 原生下載的 yt-dlp 中繼檔（.part / .ytdl）一併清掉，避免殘留半成品
        if self._native_mode:
            for suffix in ('.part', '.ytdl'):
                p = self.filepath + suffix
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        return True

    def complete_download(self):
        if not self._completion_lock.acquire(blocking=False):
            return
        succeeded = False
        try:
            # 只有「下載中」才允許收尾：避免 error 狀態的半成品被 rename 成
            # 完成檔（單線模式失敗時曾發生此誤標）。
            if self.status != 'downloading':
                return
            self.end_time = time.time()
            try:
                # 落盤最後一批資料：移除逐片 fsync 後，完成時必須確保 temp 檔內容
                # 已寫入磁碟，否則 os.replace 只改檔名、資料可能仍留在 OS 快取。
                self._flush_temp_file()
                if os.path.exists(self.temp_filepath):
                    if os.path.exists(self.filepath):
                        os.remove(self.filepath)
                    os.replace(self.temp_filepath, self.filepath)
                if os.path.exists(self.progress_filepath):
                    os.remove(self.progress_filepath)
                succeeded = True
            except Exception as e:
                self._set_status('error', reason='complete_exception')
                self.error_message = f"完成下載時出錯: {e}"
                logger.exception("完成下載時出錯: task_id=%s", self.task_id)
            # 檔案確實就位後才標記完成：狀態是外部（UI、測試、on_complete）
            # 判斷完成的依據。先標記再 rename 會讓觀察者在檔案還沒出現時就
            # 看到「已完成」；程序結束時 daemon 收尾執行緒甚至會被直接砍掉，
            # 導致檔案永遠沒被 rename（FTP 整合測試即因此失敗）。
            if succeeded:
                self._set_status('completed')
        finally:
            self._completion_lock.release()
        if succeeded and self.on_complete is not None:
            try:
                self.on_complete(self)
            except Exception as e:
                logger.warning("下載完成回呼失敗: %s", e)

    def is_running(self):
        return self.status == 'downloading'

    def is_completed(self):
        return self.status == 'completed'

    def _get_blocks_state(self):
        with self._lock:
            if self._single_mode or self.block_count == 0:
                return []
            active = self._active_blocks
            states = []
            for i in range(self.block_count):
                done = self._is_block_done(i)
                frac = 1.0 if done else 0.0
                if not done:
                    off = self._partial.get(i, 0)
                    if off:
                        start, end = self._block_bounds(i)
                        frac = min(1.0, off / max(1, end - start))
                states.append({'frac': frac, 'active': i in active})
            return states

    def get_progress(self):
        downloaded = self._downloaded()
        total = self.total_size
        percentage = (downloaded / total * 100) if total > 0 else 0.0
        elapsed = 0
        if self.start_time:
            elapsed = (self.end_time or time.time()) - self.start_time
        speed = self.get_current_speed()
        with self._lock:
            line_bytes = dict(self._line_bytes)
        line_labels = {
            key: ('直連' if key == 'direct' else key[6:])
            for key in line_bytes
        }
        return {
            'total_size': total,
            'downloaded_size': downloaded,
            'percentage': percentage,
            'speed': speed,
            'status': self.status,
            'error_message': self.error_message,
            'elapsed_time': elapsed,
            'thread_count': max(1, len(self._workers or [])),
            'block_count': self.block_count,
            'blocks': self._get_blocks_state(),
            'line_bytes': line_bytes,
            'line_labels': line_labels,
        }


class DownloadManager:
    def __init__(self):
        self.tasks = {}
        self.task_ids = {}
        self._lock = threading.RLock()
        self.next_id = 1
        self.save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.download_dirs = {self.save_dir}
        self.socks_proxies = {}
        self.next_proxy_id = 1

        self.default_chunks_per_part = 0   # 0 = 自適應切片
        self.default_threads_per_proxy = 6

        # BT / PT 防封號與做種設定
        self.bt_seed_hours = 0.0      # 下載完成後繼續做種時數，0 表示不做種
        self.bt_upload_rate = 0       # BT 上傳限速（bytes/sec），0 表示不限速
        self.bt_resume_interval = 10  # BT resume 自動保存間隔（秒），最小 1 秒
        self.bt_max_connections = 200  # BT 直連線最大連線數，0 表示用 libtorrent 預設
        self.bt_proxy_max_connections = 30  # BT SOCKS5 代理線最大連線數（CGNAT 下可達 peer 少，壓低避免死連線 churn）
        self.bt_max_tasks_per_line = 5  # 每條 SOCKS5 線路同時進行的 BT 任務數上限（5G-Proxy-Pro 握手執行緒有限；0 = 不提醒）
        self.bt_force_tcp = False     # 僅用 TCP（停用 uTP/UDP），UDP 被封的環境適用
        self.bt_listen_port = 6881    # BT 直連監聽埠，0 = 動態埠（配合 UPnP/NAT-PMP）

        self.speed_limit = 0  # bytes/sec，0 表示不限速
        self.rate_limiter = RateLimiter()
        self.custom_headers = {}
        self.auto_check_update = True  # 啟動時是否自動檢查更新
        self.language = i18n.DEFAULT_LANG  # 介面語系（locale/<code>.json）
        self._language_saved = False       # 設定檔是否已記錄語系（用來判斷首次啟動）

        self.history = []          # 歷史下載紀錄：list of dict
        self.next_history_id = 1

        self.config_dir = os.path.join(os.path.expanduser("~"), ".multi_socks_downloader")
        self.config_file = os.path.join(self.config_dir, "config.json")
        os.makedirs(self.config_dir, exist_ok=True)

        # 常駐 DHT 服務：改為「按需」——由 DHTGovernor 依下載需求動態開/關/限速，
        # 避免無 BT 任務時 24/7 消耗上行小封包拖慢其他下載。
        self.bt_dht_autotune = True
        self.dht_service = DHTService()

        self.load_config()

        if not self._language_saved:
            # 首次啟動（設定檔尚無語系）：跟隨系統語系並立刻寫回，之後即記憶使用者選擇。
            self.language = i18n.detect_system_lang()
            self.save_config()

        if self.bt_dht_autotune:
            self.dht_governor = DHTGovernor(self._dht_demand, self._apply_dht_policy)
            self.dht_governor.start()
        else:
            # 關閉自適應時退回舊行為：應用啟動即常駐 DHT 全速累積節點。
            self.dht_governor = None
            self.dht_service.start()

    def get_dht_node_count(self):
        """回傳常駐 DHT 的節點數（供 UI 顯示，無任務時也持續更新）。"""
        return self.dht_service.node_count()

    def _dht_demand(self):
        """彙整當前 DHT 需求快照（供 DHTGovernor 決策）。"""
        with self._lock:
            bt_tasks = [t for t in self.tasks.values() if isinstance(t, BTTask)]
        bt_active = False
        magnet_waiting = False
        total_rate = 0.0
        for t in bt_tasks:
            if not t.is_running():
                continue
            bt_active = True
            prog = t.get_progress()
            if prog.get('waiting_metadata'):
                magnet_waiting = True
            total_rate += prog.get('speed', 0.0)
        return {
            'bt_active': bt_active,
            'magnet_waiting': magnet_waiting,
            'total_download_rate': total_rate,
            'saturated': total_rate >= 1024 * 1024,
            'node_count': self.dht_service.node_count(),
        }

    def _apply_dht_policy(self, mode):
        """把 governor 算出的 policy 套到常駐 DHT session。"""
        self.dht_service.apply_policy(mode)

    def shutdown_dht(self):
        """停止 DHT governor 並釋放常駐 DHT session（應用退出時呼叫）。"""
        if self.dht_governor is not None:
            self.dht_governor.stop()
        self.dht_service.shutdown()

    # ------------------------------------------------------------------ #
    # config
    # ------------------------------------------------------------------ #
    def load_config(self):
        try:
            if not os.path.exists(self.config_file):
                return
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if 'save_dir' in config and os.path.exists(config['save_dir']):
                self.save_dir = config['save_dir']
            if 'download_dirs' in config:
                for d in config['download_dirs']:
                    if os.path.isdir(d):
                        self.download_dirs.add(d)
            if 'socks_proxies' in config:
                self.socks_proxies = config['socks_proxies']
                if self.socks_proxies:
                    self.next_proxy_id = max(
                        int(k) for k in self.socks_proxies.keys()) + 1
            if 'speed_limit' in config:
                self.set_speed_limit(int(config['speed_limit']))
            if 'bt_seed_hours' in config:
                self.bt_seed_hours = max(0.0, float(config['bt_seed_hours']))
            if 'bt_upload_rate' in config:
                self.bt_upload_rate = max(0, int(config['bt_upload_rate']))
            if 'bt_resume_interval' in config:
                self.bt_resume_interval = max(1.0, float(config['bt_resume_interval']))
            if 'bt_max_connections' in config:
                self.bt_max_connections = max(0, int(config['bt_max_connections']))
            if 'bt_proxy_max_connections' in config:
                self.bt_proxy_max_connections = max(0, int(config['bt_proxy_max_connections']))
            if 'bt_max_tasks_per_line' in config:
                self.bt_max_tasks_per_line = max(0, int(config['bt_max_tasks_per_line']))
            if 'bt_force_tcp' in config:
                self.bt_force_tcp = bool(config['bt_force_tcp'])
            if 'bt_listen_port' in config:
                self.bt_listen_port = max(0, int(config['bt_listen_port']))
            if 'bt_dht_autotune' in config:
                self.bt_dht_autotune = bool(config['bt_dht_autotune'])
            if 'custom_headers' in config and isinstance(config['custom_headers'], dict):
                self.custom_headers = config['custom_headers']
            if 'auto_check_update' in config:
                self.auto_check_update = bool(config['auto_check_update'])
            if 'language' in config and config['language'] in i18n.SUPPORTED_LANGS:
                self.language = config['language']
                self._language_saved = True
            if 'history' in config and isinstance(config['history'], list):
                self.history = config['history']
            if 'next_history_id' in config:
                self.next_history_id = int(config['next_history_id'])
        except Exception as e:
            logger.warning("載入設定失敗: %s", e)

    def save_config(self):
        try:
            config = {
                'save_dir': self.save_dir,
                'download_dirs': list(self.download_dirs),
                'socks_proxies': self.socks_proxies,
                'speed_limit': self.speed_limit,
                'bt_seed_hours': self.bt_seed_hours,
                'bt_upload_rate': self.bt_upload_rate,
                'bt_resume_interval': self.bt_resume_interval,
                'bt_max_connections': self.bt_max_connections,
                'bt_proxy_max_connections': self.bt_proxy_max_connections,
                'bt_max_tasks_per_line': self.bt_max_tasks_per_line,
                'bt_force_tcp': self.bt_force_tcp,
                'bt_listen_port': self.bt_listen_port,
                'bt_dht_autotune': self.bt_dht_autotune,
                'custom_headers': self.custom_headers,
                'auto_check_update': self.auto_check_update,
                'language': self.language,
                'history': self.history,
                'next_history_id': self.next_history_id,
            }
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("儲存設定失敗: %s", e)

    def reset_preferences(self):
        """還原所有偏好設定到預設值，並寫回設定檔。

        僅動可調參數；SOCKS5 代理（socks_proxies / next_proxy_id）與下載歷史
        紀錄（history / next_history_id）均保留。bt_dht_autotune 不在設置頁
        暴露、且變更需要重啟 DHT governor，故一併維持不變。
        """
        self.save_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self.download_dirs = {self.save_dir}
        self.default_chunks_per_part = 0   # 0 = 自適應切片
        self.default_threads_per_proxy = 6

        self.set_speed_limit(0)
        self.set_bt_seed_hours(0)
        self.set_bt_upload_rate(0)
        self.set_bt_resume_interval(10)
        self.set_bt_max_connections(200)
        self.set_bt_proxy_max_connections(30)
        self.set_bt_max_tasks_per_line(5)
        self.set_bt_force_tcp(False)
        self.set_bt_listen_port(6881)
        self.set_custom_headers({})
        self.auto_check_update = True

        self.save_config()

    # ------------------------------------------------------------------ #
    # history（歷史下載紀錄）
    # ------------------------------------------------------------------ #
    def add_history(self, filename, filepath, size, url):
        """新增一筆歷史下載紀錄，回傳紀錄 ID。"""
        with self._lock:
            entry = {
                'id': self.next_history_id,
                'filename': filename,
                'filepath': filepath,
                'size': int(size or 0),
                'url': url,
                'completed_time': time.time(),
            }
            self.next_history_id += 1
            self.history.append(entry)
        self.save_config()
        return entry['id']

    def remove_history(self, history_id):
        """移除一筆歷史下載紀錄，回傳被移除的紀錄（不存在則回傳 None）。"""
        with self._lock:
            removed = None
            for i, e in enumerate(self.history):
                if e.get('id') == history_id:
                    removed = self.history.pop(i)
                    break
        if removed is not None:
            self.save_config()
        return removed

    def get_history(self):
        """回傳歷史下載紀錄（複本）。"""
        with self._lock:
            return list(self.history)

    # ------------------------------------------------------------------ #
    # proxy management
    # ------------------------------------------------------------------ #
    def add_socks_proxy(self, name, host, port, username=None, password=None):
        for p in self.socks_proxies.values():
            if p['name'] == name:
                return None
        proxy_id = str(self.next_proxy_id)
        self.next_proxy_id += 1
        self.socks_proxies[proxy_id] = {
            'name': name, 'host': host, 'port': int(port),
            'username': username or '', 'password': password or '',
            'status': '未測試',
        }
        self.save_config()
        return proxy_id

    def delete_socks_proxy(self, proxy_id):
        if proxy_id not in self.socks_proxies:
            return False
        del self.socks_proxies[proxy_id]
        self.save_config()
        return True

    def test_socks_proxy(self, proxy_id):
        proxy = self.socks_proxies.get(proxy_id)
        if not proxy:
            return (False, "代理不存在")
        host, port = proxy['host'], proxy['port']
        user = proxy.get('username') or ''
        pwd = proxy.get('password') or ''
        self.socks_proxies[proxy_id]['status'] = '測試中...'
        self.save_config()

        start = time.time()
        try:
            import socks
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, host, int(port),
                        username=user if user else None,
                        password=pwd if pwd else None)
            s.settimeout(10)
            s.connect(("8.8.8.8", 53))
            s.close()
            tcp_ok = True
            tcp_err = ''
        except Exception as e:
            tcp_ok = False
            tcp_err = str(e)

        if not tcp_ok:
            self.socks_proxies[proxy_id]['status'] = f'不可用: {tcp_err}'
            self.save_config()
            return (False, tcp_err)

        try:
            if user or pwd:
                auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}"
                proxy_url = f"socks5://{auth}@{host}:{port}"
            else:
                proxy_url = f"socks5://{host}:{port}"
            r = requests.get('http://httpbin.org/ip',
                             proxies={'http': proxy_url, 'https': proxy_url},
                             timeout=15)
            elapsed = time.time() - start
            if r.status_code == 200:
                data = r.json()
                ip = data.get('origin', '未知')
                status = f"可用 ({elapsed:.1f}秒) - IP: {ip}"
                self.socks_proxies[proxy_id]['status'] = status
                self.save_config()
                return (True, f"延遲: {elapsed:.1f}秒，IP: {ip}")
            raise Exception(f"HTTP {r.status_code}")
        except Exception as e:
            elapsed = time.time() - start
            status = f"有限可用 ({elapsed:.1f}秒) - TCP正常，HTTP失敗: {e}"
            self.socks_proxies[proxy_id]['status'] = status
            self.save_config()
            return (True, status)

    def get_all_proxies(self):
        return self.socks_proxies

    def get_available_proxies(self):
        return [
            {
                'host': p['host'],
                'port': int(p['port']),
                'username': p.get('username') or '',
                'password': p.get('password') or '',
            }
            for p in self.socks_proxies.values()
            if p['status'].startswith('可用') or p['status'].startswith('有限可用')
        ]

    def _proxy_line_key(self, proxy):
        """SOCKS5 線路的身分鍵（host:port）；直連（None）回傳 None 不列入計數。"""
        if not proxy:
            return None
        return f"{proxy.get('host')}:{int(proxy.get('port') or 0)}"

    def bt_active_tasks_per_line(self):
        """計算每條 SOCKS5 線路目前進行中的 BT 任務數（直連不計）。"""
        counts = {}
        with self._lock:
            tasks = list(self.tasks.values())
        for t in tasks:
            if not isinstance(t, BTTask) or not t.is_running():
                continue
            for p in getattr(t, '_line_proxies', []):
                key = self._proxy_line_key(p)
                if key is None:
                    continue
                counts[key] = counts.get(key, 0) + 1
        return counts

    def bt_overloaded_lines(self, line_proxies):
        """回傳新 BT 任務會超出「每線同時任務上限」的 SOCKS5 線路鍵清單（軟提醒用）。

        上限為 0（不提醒）或 line_proxies 為空時回傳空清單。
        """
        if not self.bt_max_tasks_per_line or not line_proxies:
            return []
        counts = self.bt_active_tasks_per_line()
        overloaded = []
        seen = set()
        for p in line_proxies:
            key = self._proxy_line_key(p)
            if key is None or key in seen:
                continue
            seen.add(key)
            if counts.get(key, 0) >= self.bt_max_tasks_per_line:
                overloaded.append(key)
        return overloaded

    # ------------------------------------------------------------------ #
    # task management
    # ------------------------------------------------------------------ #
    def add_task(self, url, filename=None, save_dir=None,
                 use_proxy=True, chunks_per_part=None, threads_per_proxy=None,
                 headers=None, line=None, selected_files=None,
                 seed_hours=None, upload_rate_limit=None, resolve_stream=False,
                 stream_resolver=None, stream_max_height=None,
                 stream_max_fps=None, stream_audio_only=False,
                 stream_format_id=None):
        # BT 來源（magnet 連結或 .torrent 檔路徑）走獨立任務類別
        if source_kind(url) is not None:
            return self._add_bt_task(url, filename, save_dir, line=line,
                                     use_proxy=use_proxy, selected_files=selected_files,
                                     seed_hours=seed_hours,
                                     upload_rate_limit=upload_rate_limit)

        with self._lock:
            if url in self.tasks:
                existing = self.tasks[url]
                # 只有進行中/暫停/初始化的任務才視為重複；
                # 已完成或錯誤的任務允許重新下載，移除其舊 ID 後新建任務。
                if existing.status in ('initialized', 'downloading', 'paused'):
                    return existing.task_id
                self.task_ids.pop(existing.task_id, None)

        if chunks_per_part is None:
            chunks_per_part = self.default_chunks_per_part
        if threads_per_proxy is None:
            threads_per_proxy = self.default_threads_per_proxy

        save_dir = save_dir or self.save_dir
        os.makedirs(save_dir, exist_ok=True)
        with self._lock:
            self.download_dirs.add(save_dir)
        self.save_config()

        proxies = None
        if use_proxy:
            proxies = self.get_available_proxies()

        merged_headers = dict(self.custom_headers)
        merged_headers.update(dict(headers or {}))

        task = DownloadTask(
            url, save_dir, filename, proxies,
            chunks_per_part=chunks_per_part,
            threads_per_proxy=threads_per_proxy,
            headers=merged_headers,
            rate_limiter=self.rate_limiter,
            resolve_stream=resolve_stream,
            stream_resolver=stream_resolver,
            stream_max_height=stream_max_height,
            stream_max_fps=stream_max_fps,
            stream_audio_only=stream_audio_only,
            stream_format_id=stream_format_id,
        )
        task.on_complete = self._on_download_completed
        with self._lock:
            task_id = self.next_id
            self.next_id += 1
            task.task_id = task_id
            self.tasks[url] = task
            self.task_ids[task_id] = task
        logger.info("event=task_added task_id=%s url=%s resolve=%s",
                    task_id, url, resolve_stream)
        return task_id

    def _on_download_completed(self, task):
        """一般下載任務完成後的回呼：若下載的是 .torrent 檔，自動接續建立並啟動 BT 任務。"""
        if not task.is_completed():
            return
        path = getattr(task, 'filepath', '')
        if not (isinstance(path, str) and path.lower().endswith('.torrent')
                and os.path.isfile(path)):
            return
        try:
            bt_id = self._add_bt_task(path, None, task.save_dir, line=None, use_proxy=True)
            self.start_task(bt_id)
            logger.info("種子下載完成，自動啟動 BT 任務: %s -> %s", path, bt_id)
        except Exception as e:
            logger.warning("自動啟動 BT 任務失敗: %s", e)

    def _find_bt_task_by_hash(self, info_hash):
        """依 info hash 找既有的 BT 任務（跨來源字串去重用）。"""
        if not info_hash:
            return None
        for t in self.tasks.values():
            if isinstance(t, BTTask) and getattr(t, 'bt_info_hash', None) == info_hash:
                return t
        return None

    def _add_bt_task(self, source, filename, save_dir, line=None, use_proxy=True,
                     selected_files=None, seed_hours=None, upload_rate_limit=None):
        """建立 BT 下載任務（magnet 或 .torrent），支援自選線路（直連或指定 SOCKS5）。"""
        ih = bt_info_hash(source)
        with self._lock:
            if source in self.tasks:
                existing = self.tasks[source]
                if existing.status in ('initialized', 'downloading', 'seeding', 'paused'):
                    return existing.task_id
                self.task_ids.pop(existing.task_id, None)
            elif ih:
                # 同一顆種子可能以不同來源字串加入（magnet vs .torrent、不同路徑），
                # 用 info hash 再查一次，避免建立重複任務。
                existing = self._find_bt_task_by_hash(ih)
                if existing is not None and existing.status in (
                        'initialized', 'downloading', 'seeding', 'paused'):
                    return existing.task_id

        save_dir = save_dir or self.save_dir
        os.makedirs(save_dir, exist_ok=True)
        with self._lock:
            self.download_dirs.add(save_dir)
        self.save_config()

        # 解析指定線路：'auto' 代表多線聚合（直連 + 所有可用 SOCKS5），其餘為單線；
        # 未指定線路（None）且有多個可用代理時，預設聚合理。
        if line == 'auto' and use_proxy:
            proxies = [None] + self.get_available_proxies()
        elif isinstance(line, dict):
            proxies = [line]
        elif line == 'direct':
            proxies = [None]
        elif use_proxy:
            available = self.get_available_proxies()
            proxies = [None] + available if available else [None]
        else:
            proxies = [None]

        actual_seed_hours = self.bt_seed_hours if seed_hours is None else seed_hours
        actual_upload_rate = self.bt_upload_rate if upload_rate_limit is None else upload_rate_limit

        task = BTTask(source, save_dir, filename=filename, proxies=proxies,
                      selected_files=selected_files,
                      seed_hours=actual_seed_hours,
                      upload_rate_limit=actual_upload_rate,
                      resume_interval=self.bt_resume_interval,
                      max_connections=self.bt_max_connections,
                      proxy_max_connections=self.bt_proxy_max_connections,
                      force_tcp=self.bt_force_tcp,
                      listen_port=self.bt_listen_port)
        with self._lock:
            task_id = self.next_id
            self.next_id += 1
            task.task_id = task_id
            self.tasks[source] = task
            self.task_ids[task_id] = task
        return task_id

    def start_task(self, task_id):
        task = self.task_ids.get(task_id)
        if not task:
            logger.warning("event=start_task_missing task_id=%s", task_id)
            return False
        # 已在執行中的任務不再重啟：重複拖入同種子/URL 時 add_task 會去重返還同一個
        # task_id，若此處再呼叫 start() 會把進行中的校驗/下載中斷並從頭重來。
        if getattr(task, 'status', None) in ('downloading', 'seeding'):
            return True
        result = task.start()
        logger.info("event=start_task_result task_id=%s ok=%s status=%s error=%s",
                    task_id, result, task.status, task.error_message)
        return result

    def pause_task(self, task_id):
        task = self.task_ids.get(task_id)
        if not task:
            return False
        return task.pause()

    def resume_task(self, task_id):
        task = self.task_ids.get(task_id)
        if not task:
            return False
        return task.resume()

    def retry_task(self, task_id):
        task = self.task_ids.get(task_id)
        if not task:
            return False
        return task.retry()

    def cancel_task(self, task_id):
        task = self.task_ids.get(task_id)
        if not task:
            return False
        result = task.cancel()
        if result:
            with self._lock:
                self.tasks.pop(task.url, None)
                self.task_ids.pop(task_id, None)
        return result

    def get_task_progress(self, task_id):
        task = self.task_ids.get(task_id)
        if not task:
            return None
        return task.get_progress()

    def get_all_tasks(self):
        with self._lock:
            items = list(self.task_ids.items())
        return [
            {
                'id': tid,
                'url': task.url,
                'filename': task.filename,
                'status': task.status,
                'progress': task.get_progress(),
            }
            for tid, task in items
        ]

    def set_speed_limit(self, speed_limit):
        """設定全局限速（bytes/sec），0 表示不限速。"""
        self.speed_limit = max(0, int(speed_limit))
        self.rate_limiter.set_rate(self.speed_limit)

    def get_speed_limit(self):
        return self.speed_limit

    def set_bt_seed_hours(self, hours):
        """設定 BT 下載完成後預設繼續做種時數（小時），0 表示不做種。"""
        self.bt_seed_hours = max(0.0, float(hours or 0))

    def set_bt_upload_rate(self, rate_bytes_per_sec):
        """設定 BT 上傳限速（bytes/sec），0 表示不限速。"""
        self.bt_upload_rate = max(0, int(rate_bytes_per_sec or 0))

    def set_bt_resume_interval(self, seconds):
        """設定 BT resume 自動保存間隔（秒），最小 1 秒。"""
        self.bt_resume_interval = max(1.0, float(seconds or 10))

    def set_bt_max_connections(self, value):
        """設定 BT 直連線最大連線數，0 表示用 libtorrent 預設（不限）。"""
        self.bt_max_connections = max(0, int(value or 0))

    def set_bt_proxy_max_connections(self, value):
        """設定 BT SOCKS5 代理線最大連線數，0 表示用 libtorrent 預設（不限）。"""
        self.bt_proxy_max_connections = max(0, int(value or 0))

    def set_bt_max_tasks_per_line(self, value):
        """設定每條 SOCKS5 線路同時進行的 BT 任務數上限（0 = 不提醒）。"""
        self.bt_max_tasks_per_line = max(0, int(value or 0))

    def set_bt_force_tcp(self, enabled):
        """設定是否僅用 TCP（停用 uTP/UDP），UDP 被封的環境建議啟用。"""
        self.bt_force_tcp = bool(enabled)

    def set_bt_listen_port(self, port):
        """設定 BT 直連監聽埠，0 表示動態埠。"""
        self.bt_listen_port = max(0, int(port or 0))

    def set_custom_headers(self, headers):
        """設定全域預設自訂表頭（dict）。"""
        self.custom_headers = dict(headers or {})

    def set_save_dir(self, directory):
        if not directory or not isinstance(directory, str):
            return False
        try:
            os.makedirs(directory, exist_ok=True)
            if not os.path.isdir(directory):
                return False
            test = os.path.join(directory, '.download_test')
            with open(test, 'w') as f:
                f.write('test')
            os.remove(test)
        except Exception as e:
            logger.warning("設定儲存目錄失敗: %s", e)
            return False
        self.save_dir = directory
        self.download_dirs.add(directory)
        self.save_config()
        return True

    def scan_unfinished_tasks(self):
        scanned = 0
        restored = 0
        skipped = 0
        skip_reasons = []
        for directory in list(self.download_dirs):
            if not os.path.isdir(directory):
                continue
            try:
                entries = os.listdir(directory)
            except Exception as e:
                logger.warning("掃描下載目錄 %s 失敗: %s", directory, e)
                continue
            for name in entries:
                if not name.endswith('.progress'):
                    continue
                scanned += 1
                progress_file = os.path.join(directory, name)
                try:
                    with open(progress_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    url = data.get('url')
                    if not url:
                        skipped += 1
                        skip_reasons.append(f"{name}: 缺少 url")
                        logger.warning("跳過無效進度檔（缺少 url）: %s", progress_file)
                        continue
                    if url in self.tasks:
                        skipped += 1
                        skip_reasons.append(f"{name}: url 已有既有任務")
                        continue
                    task_save_dir = data.get('save_dir', directory)
                    filename = data.get('filename', name[:-len('.progress')])
                    task = DownloadTask(
                        url, task_save_dir, filename,
                        proxies=data.get('proxies', []),
                        headers=data.get('headers', {}),
                        rate_limiter=self.rate_limiter,
                        resolve_stream=data.get('resolve_stream', False),
                        stream_max_height=data.get('stream_max_height'),
                        stream_max_fps=data.get('stream_max_fps'),
                        stream_audio_only=data.get('stream_audio_only', False),
                        stream_format_id=data.get('stream_format_id'),
                    )
                    if not task.load_progress():
                        skipped += 1
                        skip_reasons.append(f"{name}: load_progress 失敗")
                        logger.warning("跳過無法載入的進度檔: %s", progress_file)
                        continue
                    task.on_complete = self._on_download_completed
                    with self._lock:
                        task_id = self.next_id
                        self.next_id += 1
                        task.task_id = task_id
                        self.tasks[url] = task
                        self.task_ids[task_id] = task
                    restored += 1
                    logger.info("event=task_restored task_id=%s url=%s progress=%s",
                                task_id, url, progress_file)
                except Exception as e:
                    skipped += 1
                    skip_reasons.append(f"{name}: {e}")
                    logger.warning("掃描未完成任務 %s 失敗: %s", progress_file, e)

        bt_restored = 0
        # BT 任務掃描：<save_dir>/.bt_tmp/<infohash>/task.json
        for directory in list(self.download_dirs):
            bt_root = os.path.join(directory, '.bt_tmp')
            if not os.path.isdir(bt_root):
                continue
            try:
                hh_entries = os.listdir(bt_root)
            except Exception:
                continue
            for hh in hh_entries:
                task_json = os.path.join(bt_root, hh, 'task.json')
                if not os.path.isfile(task_json):
                    continue
                try:
                    with open(task_json, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    source = state.get('source')
                    if not source or source in self.tasks:
                        continue
                    if self._find_bt_task_by_hash(bt_info_hash(source)) is not None:
                        continue
                    proxies = state.get('proxies')
                    if not proxies:
                        p = state.get('proxy')
                        proxies = [p] if p else [None]
                    task = BTTask(
                        source,
                        state.get('save_dir') or directory,
                        filename=state.get('filename'),
                        proxies=proxies,
                        selected_files=state.get('selected_files'),
                        seed_hours=state.get('seed_hours', self.bt_seed_hours),
                        upload_rate_limit=state.get('upload_rate_limit', self.bt_upload_rate),
                        resume_interval=self.bt_resume_interval,
                        max_connections=self.bt_max_connections,
                        proxy_max_connections=self.bt_proxy_max_connections,
                        force_tcp=self.bt_force_tcp,
                        listen_port=self.bt_listen_port,
                    )
                    # 記錄實際掃描到的 .bt_tmp 目錄，刪除時才能精準清掉；目錄名可能是
                    # 舊版留下的哨兵值（如 'nohash'），無法靠 source 的 info hash 推導。
                    task._work_root = os.path.dirname(task_json)
                    task.status = 'paused'
                    with self._lock:
                        task_id = self.next_id
                        self.next_id += 1
                        task.task_id = task_id
                        self.tasks[source] = task
                        self.task_ids[task_id] = task
                    bt_restored += 1
                except Exception as e:
                    logger.warning("掃描未完成 BT 任務 %s 失敗: %s", task_json, e)

        if scanned or skipped:
            logger.info(
                "event=scan_unfinished 掃到 %s 個進度檔，恢復 %s 個，跳過 %s 個",
                scanned, restored, skipped)
            for reason in skip_reasons:
                logger.debug("scan_skip_reason: %s", reason)
        if bt_restored:
            logger.info("event=scan_unfinished 恢復 BT 任務 %s 個", bt_restored)

        self.save_config()
        return restored + bt_restored
