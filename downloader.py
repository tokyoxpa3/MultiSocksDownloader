import os
import time
import json
import logging
import threading
import re
import base64
from collections import deque
from urllib.parse import urlparse, unquote, parse_qs, quote

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from ftp_downloader import SocksFTP, parse_ftp_url
from bt_downloader import BTTask, source_kind

logger = logging.getLogger('downloader')

# 區塊（切片）自適應大小：依檔案大小決定區塊數，避免大檔案切出上百 MB 的巨片。
TARGET_BLOCK_SIZE = 4 * 1024 * 1024   # 目標每片 4 MiB
MIN_BLOCKS = 1
MAX_BLOCKS = 4096


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

    def __init__(self, url, save_dir, filename=None, proxies=None,
                 chunks_per_part=100, threads_per_proxy=3, headers=None,
                 rate_limiter=None):
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
        self.start_time = None
        self.end_time = None

        self.supports_range = False
        self._single_mode = False

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

        self._workers = []
        self._completion_thread = None

        self._speed_history = deque(maxlen=30)
        self._last_time = time.time()

        self._resumed_size = 0

        # 完成回呼（由 DownloadManager 注入，供 .torrent 下載完成後自動接續 BT）
        self.on_complete = None

        self.task_id = None
        self.threads = []

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
            if info is None:
                self.status = 'error'
                self.error_message = '無法連接伺服器或取得檔案資訊'
                return False

            self.supports_range = info['supports_range']
            if info.get('total_size'):
                self.total_size = info['total_size']
            self._compute_chunk_size()

            hname = self._filename_from_headers(info['headers'])
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
            self.status = 'error'
            self.error_message = f"準備下載時出錯: {e}"
            return False

    def _probe(self, lines):
        headers = self._request_headers()
        headers['Range'] = 'bytes=0-0'
        deadline = time.time() + 15

        def _build_info(r):
            info = {'headers': r.headers}
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
            try:
                s = self._make_session(proxy)
                r = s.get(self.url, headers=headers, stream=True,
                          timeout=(5, 8), allow_redirects=True)
                info = _build_info(r)
                r.close()
                s.close()
                if info['total_size'] > 0:
                    with lock:
                        if 'info' not in result:
                            result['info'] = info
                    done.set()
            except Exception:
                pass

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

    def _worker(self, proxy, stop):
        session = self._make_session(proxy)
        try:
            while not stop.is_set():
                idx = self._pop_block()
                if idx is None:
                    break
                if self._is_block_done(idx):
                    continue
                with self._lock:
                    self._active_blocks.add(idx)
                try:
                    result = self._download_block(idx, session, stop, proxy)
                finally:
                    with self._lock:
                        self._active_blocks.discard(idx)
                if result == 'ok':
                    if self._all_blocks_done():
                        break
                elif result == 'fallback':
                    self._fallback_event.set()
                    break
        finally:
            try:
                session.close()
            except Exception:
                pass

    def _download_block(self, idx, session, stop, proxy=None):
        start, end = self._block_bounds(idx)
        end -= 1  # inclusive
        if start >= self.total_size:
            return 'ok'

        # 續傳：從區塊內已寫入的偏移繼續，重啟/重試不再整塊重抓
        with self._lock:
            off = self._partial.get(idx, 0)
        req_start = start + off
        if req_start > end:
            with self._lock:
                self._set_block_done(idx)
                self._partial.pop(idx, None)
            return 'ok'

        headers = self._request_headers()
        headers['Range'] = f"bytes={req_start}-{end}"

        try:
            r = session.get(self.url, headers=headers, stream=True,
                            timeout=(15, 60), allow_redirects=True)
        except Exception as e:
            return self._handle_block_failure(idx, f"連接失敗: {e}", stop)

        if r.status_code == 206:
            return self._write_block(idx, req_start, end, r, stop, proxy)
        elif r.status_code == 200:
            r.close()
            return 'fallback'
        elif r.status_code == 416:
            r.close()
            return self._handle_block_failure(idx, "HTTP 416：範圍請求被拒絕", stop)
        else:
            r.close()
            return self._handle_block_failure(idx, f"HTTP {r.status_code}", stop)

    def _write_block(self, idx, start, end, r, stop, proxy=None):
        need = end - start + 1
        with self._lock:
            self._partial[idx] = 0
        key = self._line_key(proxy)
        try:
            with open(self.temp_filepath, 'r+b') as f:
                f.seek(start)
                pos = start
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
                    with self._lock:
                        self._partial[idx] = pos - start
                        self._line_bytes[key] = self._line_bytes.get(key, 0) + len(data)
                r.close()
                f.flush()
                os.fsync(f.fileno())
            if stop.is_set():
                with self._lock:
                    self._partial.pop(idx, None)
                return 'fail'
            if pos - start >= need:
                with self._lock:
                    self._set_block_done(idx)
                    self._partial.pop(idx, None)
                return 'ok'
            with self._lock:
                self._partial.pop(idx, None)
            return self._handle_block_failure(
                idx, f"區塊下載不完整: {pos - start}/{need}", stop)
        except Exception as e:
            try:
                r.close()
            except Exception:
                pass
            with self._lock:
                self._partial.pop(idx, None)
            return self._handle_block_failure(idx, f"寫入失敗: {e}", stop)

    def _handle_block_failure(self, idx, reason, stop):
        if stop.is_set():
            return 'fail'
        retries = self._block_retries.get(idx, 0) + 1
        self._block_retries[idx] = retries
        logger.warning("區塊 %s %s (重試 %s/%s)", idx, reason, retries, self.MAX_RETRIES)
        if retries >= self.MAX_RETRIES:
            self.error_message = f"區塊 {idx} 下載失敗: {reason}"
            self._fatal = True
            self.status = 'error'
            stop.set()
            return 'fail'
        self._queue_block(idx)
        return 'fail'

    def _all_blocks_done(self):
        with self._lock:
            return self._popcount() == self.block_count

    def _single_worker(self, proxy, stop):
        session = self._make_session(proxy)
        try:
            headers = self._request_headers()
            r = session.get(self.url, headers=headers, stream=True,
                            timeout=(15, 60), allow_redirects=True)
            if r.status_code != 200:
                self.status = 'error'
                self.error_message = f"HTTP {r.status_code}"
                r.close()
                return
            cl = r.headers.get('content-length')
            if cl:
                self.total_size = int(cl)
            bytes_since_fsync = 0
            key = self._line_key(proxy)
            with open(self.temp_filepath, 'wb') as f:
                for chunk in r.iter_content(self.chunk_size):
                    if stop.is_set():
                        break
                    if not chunk:
                        continue
                    if self.rate_limiter is not None:
                        self.rate_limiter.acquire(len(chunk))
                    f.write(chunk)
                    with self._lock:
                        self.downloaded_size += len(chunk)
                        self._line_bytes[key] = self._line_bytes.get(key, 0) + len(chunk)
                    bytes_since_fsync += len(chunk)
                    if bytes_since_fsync >= 8 * 1024 * 1024:
                        f.flush()
                        os.fsync(f.fileno())
                        bytes_since_fsync = 0
            r.close()
        except Exception as e:
            if not stop.is_set():
                self.status = 'error'
                self.error_message = str(e)
        finally:
            try:
                session.close()
            except Exception:
                pass
        # 只有仍是當前世代（stop 未被新 start() 取代）才允許完成收尾，
        # 避免暫停/恢復後舊 worker 誤把新世代的下載標記為完成。
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
                        with self._lock:
                            self._partial[idx] = pos - start  # 累計偏移
                            self._line_bytes[key] = self._line_bytes.get(key, 0) + len(chunk)
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
                self.status = 'error'
                self.error_message = str(e)
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

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self):
        self.status = 'downloading'
        self.error_message = ''
        self._fatal = False

        if not self.prepare():
            return False

        # prepare()/load_progress() 可能把 status 改回 'paused'，必須再次確認為下載中
        self.status = 'downloading'
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

        if self._single_mode:
            proxy = self.proxies[0] if self.proxies else None
            self.threads = []
            t = threading.Thread(target=self._single_worker, args=(proxy, stop), daemon=True)
            self.threads.append(t)
            t.start()
            return True

        self._rebuild_pool()
        self._workers = []
        self.threads = []
        lines = self._build_lines()
        # 工作者數不應超過剩餘區塊數，避免空轉的線程
        max_workers = self.block_count if self.block_count > 0 else len(lines) * self.threads_per_proxy
        spawned = 0
        for line in lines:
            for _ in range(self.threads_per_proxy):
                if spawned >= max_workers:
                    break
                t = threading.Thread(target=self._worker, args=(line, stop), daemon=True)
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
        self.status = 'paused'
        for t in self._workers:
            if t.is_alive():
                t.join(timeout=1.0)
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
        self.status = 'initialized'
        return self.start()

    def cancel(self):
        self._stop.set()
        self.status = 'canceled'
        for t in self.threads:
            if t is not threading.current_thread() and t.is_alive():
                t.join(timeout=1)
        for p in (self.temp_filepath, self.progress_filepath):
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
            if self.status == 'completed':
                return
            if self._stop.is_set() and self.status != 'downloading':
                return
            self.end_time = time.time()
            self.status = 'completed'
            try:
                if os.path.exists(self.temp_filepath):
                    if os.path.exists(self.filepath):
                        os.remove(self.filepath)
                    os.replace(self.temp_filepath, self.filepath)
                if os.path.exists(self.progress_filepath):
                    os.remove(self.progress_filepath)
                succeeded = True
            except Exception as e:
                self.status = 'error'
                self.error_message = f"完成下載時出錯: {e}"
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

        self.speed_limit = 0  # bytes/sec，0 表示不限速
        self.rate_limiter = RateLimiter()
        self.custom_headers = {}

        self.history = []          # 歷史下載紀錄：list of dict
        self.next_history_id = 1

        self.config_dir = os.path.join(os.path.expanduser("~"), ".multi_socks_downloader")
        self.config_file = os.path.join(self.config_dir, "config.json")
        os.makedirs(self.config_dir, exist_ok=True)

        self.load_config()

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
            if 'custom_headers' in config and isinstance(config['custom_headers'], dict):
                self.custom_headers = config['custom_headers']
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
                'custom_headers': self.custom_headers,
                'history': self.history,
                'next_history_id': self.next_history_id,
            }
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("儲存設定失敗: %s", e)

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

    # ------------------------------------------------------------------ #
    # task management
    # ------------------------------------------------------------------ #
    def add_task(self, url, filename=None, save_dir=None,
                 use_proxy=True, chunks_per_part=None, threads_per_proxy=None,
                 headers=None, line=None, selected_files=None,
                 seed_hours=None, upload_rate_limit=None):
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
        )
        task.on_complete = self._on_download_completed
        with self._lock:
            task_id = self.next_id
            self.next_id += 1
            task.task_id = task_id
            self.tasks[url] = task
            self.task_ids[task_id] = task
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

    def _add_bt_task(self, source, filename, save_dir, line=None, use_proxy=True,
                     selected_files=None, seed_hours=None, upload_rate_limit=None):
        """建立 BT 下載任務（magnet 或 .torrent），支援自選線路（直連或指定 SOCKS5）。"""
        with self._lock:
            if source in self.tasks:
                existing = self.tasks[source]
                if existing.status in ('initialized', 'downloading', 'seeding', 'paused'):
                    return existing.task_id
                self.task_ids.pop(existing.task_id, None)

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
                      resume_interval=self.bt_resume_interval)
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
            return False
        return task.start()

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
        count = 0
        for directory in list(self.download_dirs):
            if not os.path.isdir(directory):
                continue
            try:
                entries = os.listdir(directory)
            except Exception:
                continue
            for name in entries:
                if not name.endswith('.progress'):
                    continue
                progress_file = os.path.join(directory, name)
                try:
                    with open(progress_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    url = data.get('url')
                    if not url or url in self.tasks:
                        continue
                    task_save_dir = data.get('save_dir', directory)
                    filename = data.get('filename', name[:-len('.progress')])
                    task = DownloadTask(
                        url, task_save_dir, filename,
                        proxies=data.get('proxies', []),
                        headers=data.get('headers', {}),
                        rate_limiter=self.rate_limiter,
                    )
                    if not task.load_progress():
                        continue
                    task.on_complete = self._on_download_completed
                    with self._lock:
                        task_id = self.next_id
                        self.next_id += 1
                        task.task_id = task_id
                        self.tasks[url] = task
                        self.task_ids[task_id] = task
                    count += 1
                except Exception as e:
                    logger.warning("掃描未完成任務 %s 失敗: %s", progress_file, e)

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
                    )
                    task.status = 'paused'
                    with self._lock:
                        task_id = self.next_id
                        self.next_id += 1
                        task.task_id = task_id
                        self.tasks[source] = task
                        self.task_ids[task_id] = task
                    count += 1
                except Exception as e:
                    logger.warning("掃描未完成 BT 任務 %s 失敗: %s", task_json, e)
        self.save_config()
        return count
