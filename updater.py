"""自動更新：檢查 / 下載 / SHA-256 校驗 / 原子替換 / 重啟（純邏輯，無 GUI）。

本模組把 docs/auto-update.md 的用戶端流程與踩坑紀錄落成可測試的函式，
重點對應關係：

- ``is_frozen()``          → 坑 1（Nuitka 不設 sys.frozen）
- ``frozen_exe_path()``    → 坑 2（Nuitka 的 sys.executable 指向內建 python.exe）
- ``apply_script_content()``  → 坑 3/4/5/6/8（PS 5.1 的 Start-Process、編碼、
                              日誌路徑、Start-Job、正確程序名）
- ``_extract_zip()``       → zip-slip 防護
"""

import collections
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import quote

import requests

import version

# ---------------------------------------------------------------------- #
# 下載參數（多線程分段 + 智能分流）
# ---------------------------------------------------------------------- #
_TIMEOUT_CONNECT = 15     # 建立連線逾時秒數
_TIMEOUT_READ = 60        # 單次讀取逾時秒數
TARGET_BLOCK_SIZE = 1 * 1024 * 1024   # 目標分段大小
MAX_BLOCKS = 32                       # 分段數上限
MIN_MULTIPART_SIZE = 4 * 1024 * 1024  # 小於此大小不值得分段
CHUNK_SIZE = 256 * 1024               # 每次讀取的位元組數
STALL_WINDOW = 30                     # 停滯偵測視窗（秒）
STALL_MIN_BYTES = 128 * 1024          # 視窗內至少需收到的位元組
MAX_BLOCK_RETRIES = 4                 # 單一分段最大重試次數
# GitHub release CDN 常以「突發」方式送資料：短時間灌一批、然後停頓。用
# 0.5 秒瞬時速度估算時，幾乎每個停頓窗都會顯示 0 B/s，看起來像不斷歸零；
# 改用數秒滑動視窗取平均才反映真實吞吐。
SPEED_WINDOW = 3.0
USER_AGENT = "MultiSocksDownloader-Updater"

# --- 代理 / 智能分流 ---
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".multi_socks_downloader")
PROXY_CONFIG_FILENAME = "config.json"
PROXY_PROBE_BYTES = 256 * 1024        # 量測各路徑吞吐時抓的位元組數
PROXY_PROBE_TIMEOUT = 6               # 單一路徑量測逾時（秒）
PROXY_PROBE_TOTAL_TIMEOUT = 8         # 全部路徑量測的總逾時（秒）


# ---------------------------------------------------------------------- #
# 凍結偵測與執行檔路徑
# ---------------------------------------------------------------------- #
def is_frozen():
    """回傳目前是否為打包後的獨立執行檔（涵蓋 PyInstaller / Nuitka）。"""
    if getattr(sys, "frozen", False):          # PyInstaller / cx_Freeze
        return True
    if hasattr(sys, "_MEIPASS"):               # PyInstaller onefile
        return True
    if globals().get("__compiled__", False):   # Nuitka 注入的模組全域變數
        return True
    return False


def frozen_exe_path():
    """取得真正執行中的 exe 路徑。

    Nuitka standalone 會把 ``sys.executable`` 設成 dist 內建的 python.exe，
    而不是真正的程式 exe；用 ``sys.argv[0]`` 才能拿到對的路徑。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    p = os.path.abspath(argv0) if argv0 else ""
    if p and p.lower().endswith(".exe"):
        return p
    return os.path.abspath(sys.executable)


def current_dist_dir():
    """回傳執行檔所在的資料夾（打包後即 .dist 目錄）。"""
    return os.path.dirname(frozen_exe_path())


# ---------------------------------------------------------------------- #
# 路徑選擇（智能分流 + 防呆）
# ---------------------------------------------------------------------- #
def _config_search_paths():
    """回傳可能存放 config.json 的路徑（依序、去重）。

    本專案的設定固定放在 ``~/.multi_socks_downloader/config.json``，另外
    也接受執行檔目錄與工作目錄的 config.json（方便可攜/測試）。
    """
    candidates = [os.path.join(CONFIG_DIR, PROXY_CONFIG_FILENAME)]
    try:
        candidates.append(os.path.join(current_dist_dir(), PROXY_CONFIG_FILENAME))
    except Exception:  # noqa: BLE001
        pass
    candidates.append(os.path.abspath(PROXY_CONFIG_FILENAME))
    seen = []
    for p in candidates:
        if p and p not in seen:
            seen.append(p)
    return seen


def _proxy_map(proxy_url):
    """把單一代理 URL 轉成 requests 的 proxies dict；無代理回 None。"""
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _proxy_url_from_entry(entry):
    """把設定檔的一筆代理轉成 requests 用的 URL；不適用則回 None。

    同時接受本專案 ``socks_proxies`` 的欄位（host/port/username/password）
    與通用寫法（ip/user/pass），兩種都吃。SOCKS5 用 ``socks5h``（由代理端
    解析 DNS），避免本機 DNS 設定影響。型別不支援或欄位缺失一律回 None。
    """
    try:
        ptype = str(entry.get("type", "SOCKS5")).upper()
        if ptype not in ("SOCKS5", "HTTP"):
            return None
        host = str(entry.get("host") or entry.get("ip") or "").strip()
        port = int(entry.get("port") or 0)
        if not host or not port:
            return None
        user = entry.get("username") or entry.get("user") or ""
        pwd = entry.get("password") or entry.get("pass") or ""
        scheme = "socks5h" if ptype == "SOCKS5" else "http"
        if user or pwd:
            auth = "{}:{}".format(quote(str(user), safe=""), quote(str(pwd), safe=""))
            return "{}://{}@{}:{}".format(scheme, auth, host, port)
        return "{}://{}:{}".format(scheme, host, port)
    except Exception:  # noqa: BLE001
        return None


def load_proxy_urls(config_paths=None):
    """從設定檔讀出可用代理，回傳 ``[{"name", "url"}]``。

    讀 ``socks_proxies``（dict: id -> 代理，本專案格式）與 ``proxies``
    （list，通用格式）。任何問題（檔案不存在、JSON 破損、欄位不合法）都
    只略過該筆；整體失敗回傳空清單 —— 更新流程絕不因設定檔問題而中斷。
    """
    for path in (config_paths or _config_search_paths()):
        data = None
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            continue
        entries = []
        raw = data.get("socks_proxies")
        if isinstance(raw, dict):
            entries.extend(v for v in raw.values() if isinstance(v, dict))
        elif isinstance(raw, list):
            entries.extend(v for v in raw if isinstance(v, dict))
        more = data.get("proxies")
        if isinstance(more, list):
            entries.extend(v for v in more if isinstance(v, dict))
        out = []
        for entry in entries:
            url = _proxy_url_from_entry(entry)
            if url:
                out.append({"name": entry.get("name") or url, "url": url})
        if out:
            return out
    return []


def _measure_path(url, proxy_url, probe_bytes=PROXY_PROBE_BYTES,
                  timeout=PROXY_PROBE_TIMEOUT):
    """量測一條路徑的吞吐（bytes/s）；失敗或無資料回 None。

    用一次小範圍 Range 請求實測，而不是只量 ping —— CDN 與代理的「連得上」
    和「跑得快」經常是兩回事，只量延遲會挑到很慢的線。
    """
    headers = {
        "Range": "bytes=0-{}".format(probe_bytes - 1),
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    try:
        t0 = time.monotonic()
        r = requests.get(url, headers=headers, stream=True,
                         proxies=_proxy_map(proxy_url),
                         timeout=(_TIMEOUT_CONNECT, timeout),
                         allow_redirects=True)
        try:
            if r.status_code not in (200, 206):
                return None
            got = 0
            for chunk in r.iter_content(CHUNK_SIZE):
                if not chunk:
                    continue
                got += len(chunk)
                if got >= probe_bytes:
                    break
            dt = time.monotonic() - t0
            if got <= 0 or dt <= 0:
                return None
            return got / dt
        finally:
            r.close()
    except Exception:  # noqa: BLE001 — 代理掛掉/不支援 SOCKS 都只代表這條不可用
        return None


def _rank_download_paths(url, use_proxy=True):
    """並行量測直連與各代理，回傳由快到慢的 ``[(proxy_url, speed, label)]``。

    防呆保證：
    - 沒有設定代理時直接回 ``[(None, 0.0, "直連")]``，不增加任何延遲。
    - 個別路徑量測失敗只會少一個候選。
    - 直連永遠保留且保底排在最後，代理全掛時行為與原本完全相同。
    """
    proxies = []
    if use_proxy:
        try:
            proxies = load_proxy_urls()
        except Exception:  # noqa: BLE001
            proxies = []
    if not proxies:
        return [(None, 0.0, "直連")]

    candidates = [(p["url"], p["name"]) for p in proxies]
    candidates.append((None, "直連"))

    results = {}
    lock = threading.Lock()

    def probe(proxy_url, label):
        speed = _measure_path(url, proxy_url)
        if speed is not None:
            with lock:
                results[proxy_url] = (speed, label)

    threads = [threading.Thread(target=probe, args=(u, n), daemon=True)
               for u, n in candidates]
    for t in threads:
        t.start()
    deadline = time.monotonic() + PROXY_PROBE_TOTAL_TIMEOUT
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))

    with lock:
        ranked = [(u, s, n) for u, (s, n) in results.items()]
    ranked.sort(key=lambda item: -item[1])
    if not ranked:
        return [(None, 0.0, "直連")]
    if not any(u is None for u, _, _ in ranked):
        ranked.append((None, 0.0, "直連"))
    return ranked


# ---------------------------------------------------------------------- #
# 版本比較
# ---------------------------------------------------------------------- #
def _parse_version(v):
    """把版本字串拆成整數 tuple；非數字後綴（如 -beta）會被忽略。"""
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_newer(latest, current):
    """回傳 latest 是否嚴格大於 current（整數 tuple 比較，避免字串比對誤判）。"""
    return _parse_version(latest) > _parse_version(current)


# ---------------------------------------------------------------------- #
# 檢查更新
# ---------------------------------------------------------------------- #
def _find_asset(release, suffix):
    """從 release 的 assets 找出第一個名稱結尾符合 suffix 的資產。"""
    for a in release.get("assets", []):
        name = a.get("name", "")
        if name.lower().endswith(suffix.lower()):
            return a
    return None


def check_update(current_version, api_url=version.UPDATE_API_URL, timeout=15):
    """查 GitHub Release API；有新版本時回傳下載資訊，否則回傳 None。

    回傳 dict: {"version", "url", "name", "checksum_url"}；
    網路錯誤或解析失敗會向上拋出，由呼叫端決定如何呈現。
    """
    resp = requests.get(api_url, timeout=timeout)
    resp.raise_for_status()
    release = resp.json()

    latest = str(release.get("tag_name", "")).lstrip("v")
    if not latest or not _is_newer(latest, current_version):
        return None

    zip_asset = _find_asset(release, ".zip")
    if not zip_asset:
        return None
    checksum_asset = _find_asset(release, "sha256sums.txt")
    return {
        "version": latest,
        "url": zip_asset["browser_download_url"],
        "name": zip_asset["name"],
        "checksum_url": checksum_asset["browser_download_url"]
        if checksum_asset else None,
    }


# ---------------------------------------------------------------------- #
# 下載 + 校驗 + 解壓
# ---------------------------------------------------------------------- #
def _parse_sha256_sums(text, name):
    """從 SHA256SUMS.txt 內容解析出指定資產的 SHA-256 雜湊（小寫 hex）。"""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0]
        if name in parts[1:] and re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            return digest.lower()
    raise ValueError("SHA256SUMS 中找不到資產 {!r} 的雜湊".format(name))


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_to(url, dest, timeout=120):
    """串流下載到 dest（僅用於 SHA256SUMS 這類小檔），避免整包塞進記憶體。"""
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


# ---------------------------------------------------------------------- #
# 更新檔下載引擎：多線程分段 + 智能分流 + 防呆退路
# ---------------------------------------------------------------------- #
class UpdateCancelled(Exception):
    """使用者取消了更新下載。"""


class _ProgressReporter(threading.Thread):
    """每 0.5 秒回報一次進度；速度取數秒滑動視窗平均，避免瞬時值顯示 0。"""

    def __init__(self, callback, total, get_downloaded, get_threads, interval=0.5):
        super().__init__(daemon=True)
        self._cb = callback
        self._total = total
        self._get_downloaded = get_downloaded
        self._get_threads = get_threads
        self._interval = interval
        self._stop = threading.Event()
        self._history = collections.deque()   # (t, bytes) 供滑動視窗計算速度

    def run(self):
        while not self._stop.wait(self._interval):
            self.emit()

    def emit(self):
        if self._cb is None:
            return
        now = time.monotonic()
        cur = self._get_downloaded()
        self._history.append((now, cur))
        while len(self._history) > 1 and now - self._history[0][0] > SPEED_WINDOW:
            self._history.popleft()
        t0, b0 = self._history[0]
        dt = now - t0
        speed = max(0.0, (cur - b0) / dt) if dt > 0 else 0.0
        try:
            self._cb({
                "downloaded": cur,
                "total": self._total,
                "speed": speed,
                "threads": self._get_threads(),
            })
        except Exception:  # noqa: BLE001 — 進度回報不應影響下載
            pass

    def stop(self):
        self._stop.set()


def _probe_download(url, proxy_url=None, timeout=_TIMEOUT_CONNECT):
    """以 Range 探測檔案大小與是否支援分段，並取得轉址後的最終 URL。

    回傳 ``(final_url, total_size, supports_range)``。final_url 是簽名 CDN
    URL，供所有分段共用，省去每段重走一次 302。
    """
    headers = {
        "Range": "bytes=0-0",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    r = requests.get(url, headers=headers, stream=True,
                     timeout=(_TIMEOUT_CONNECT, timeout),
                     allow_redirects=True, proxies=_proxy_map(proxy_url))
    try:
        r.raise_for_status()
        final_url = r.url or url
        if r.status_code == 206:
            total = 0
            m = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
            if m:
                total = int(m.group(1))
            if not total:
                total = int(r.headers.get("content-length", 0) or 0)
            return final_url, total, True
        return final_url, int(r.headers.get("content-length", 0) or 0), False
    finally:
        r.close()


def _fetch_block(session, url, start, end, dest, block_bytes, idx, lock,
                 stop, cancel_event):
    """下載 [start, end] 這段並寫入 dest 的對應偏移；失敗丟例外由 worker 重試。"""
    headers = {
        "Range": "bytes={}-{}".format(start, end),
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    r = session.get(url, headers=headers, stream=True,
                    timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
                    allow_redirects=True)
    try:
        if r.status_code != 206:
            raise RuntimeError("HTTP {}".format(r.status_code))
        pos = start
        need = end - start + 1
        win_start = time.monotonic()
        win_bytes = 0
        with open(dest, "r+b") as f:
            f.seek(start)
            for chunk in r.iter_content(CHUNK_SIZE):
                if stop.is_set():
                    raise UpdateCancelled()
                if cancel_event is not None and cancel_event.is_set():
                    raise UpdateCancelled()
                if not chunk:
                    continue
                if pos + len(chunk) > end + 1:
                    chunk = chunk[:end + 1 - pos]
                f.write(chunk)
                pos += len(chunk)
                win_bytes += len(chunk)
                with lock:
                    block_bytes[idx] += len(chunk)
                if pos > end:
                    break
                now = time.monotonic()
                if now - win_start >= STALL_WINDOW:
                    if win_bytes < STALL_MIN_BYTES:
                        raise RuntimeError(
                            "停滯: {} bytes/{:.0f}s".format(win_bytes, now - win_start))
                    win_start, win_bytes = now, 0
        if pos - start < need:
            raise RuntimeError("區段不完整: {}/{}".format(pos - start, need))
    finally:
        r.close()


def _download_multipart(final_url, dest, total, progress_cb, cancel_event,
                        proxy_url=None):
    """把檔案切成多段並行下載到 dest（dest 會先配置成 total 大小）。"""
    block_size = max(TARGET_BLOCK_SIZE, (total + MAX_BLOCKS - 1) // MAX_BLOCKS)
    bounds = []
    start = 0
    while start < total and len(bounds) < MAX_BLOCKS:
        end = min(start + block_size, total) - 1
        bounds.append((start, end))
        start = end + 1
    if not bounds:
        bounds = [(0, total - 1)]
    n = len(bounds)

    with open(dest, "wb") as f:
        f.truncate(total)

    work = queue.Queue()
    for i in range(n):
        work.put(i)

    lock = threading.Lock()
    block_bytes = [0] * n
    active = [0]
    stop = threading.Event()
    failures = []

    def downloaded():
        with lock:
            return sum(block_bytes)

    def active_count():
        with lock:
            return active[0]

    reporter = _ProgressReporter(progress_cb, total, downloaded, active_count)
    reporter.start()

    def worker():
        session = requests.Session()
        if proxy_url:
            session.proxies.update(_proxy_map(proxy_url))
        try:
            while not stop.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    stop.set()
                    return
                try:
                    idx = work.get_nowait()
                except queue.Empty:
                    return
                b_start, b_end = bounds[idx]
                with lock:
                    active[0] += 1
                try:
                    attempt = 0
                    while True:
                        attempt += 1
                        try:
                            _fetch_block(session, final_url, b_start, b_end, dest,
                                         block_bytes, idx, lock, stop, cancel_event)
                            break
                        except UpdateCancelled:
                            stop.set()
                            return
                        except Exception as e:  # noqa: BLE001
                            if cancel_event is not None and cancel_event.is_set():
                                stop.set()
                                return
                            if attempt >= MAX_BLOCK_RETRIES:
                                failures.append(
                                    "區段 {}-{} 失敗: {}".format(b_start, b_end, e))
                                stop.set()
                                return
                            # 重試前把該段已計入的位元組歸零，進度才不會虛胖
                            with lock:
                                block_bytes[idx] = 0
                            time.sleep(min(5.0, 0.5 * (2 ** (attempt - 1))))
                finally:
                    with lock:
                        active[0] -= 1
        finally:
            session.close()

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    reporter.emit()
    reporter.stop()

    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled()
    if failures:
        raise RuntimeError(failures[0])
    if os.path.getsize(dest) != total or downloaded() < total:
        raise RuntimeError("下載不完整: {}/{} 位元組".format(downloaded(), total))


def _download_single(final_url, dest, total_hint, progress_cb, cancel_event,
                     proxy_url=None):
    """單線串流下載（伺服器不支援 Range，或分段失敗時的後備路徑）。"""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    r = requests.get(final_url, headers=headers, stream=True,
                     timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
                     allow_redirects=True, proxies=_proxy_map(proxy_url))
    try:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0) or 0) or total_hint
        done = 0
        last_t = time.monotonic()
        last_b = 0
        win_start = time.monotonic()
        win_bytes = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(CHUNK_SIZE):
                if cancel_event is not None and cancel_event.is_set():
                    raise UpdateCancelled()
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                win_bytes += len(chunk)
                now = time.monotonic()
                if progress_cb is not None and now - last_t >= 0.5:
                    speed = (done - last_b) / (now - last_t) if now > last_t else 0.0
                    try:
                        progress_cb({"downloaded": done, "total": total,
                                     "speed": speed, "threads": 1})
                    except Exception:  # noqa: BLE001
                        pass
                    last_t, last_b = now, done
                if now - win_start >= STALL_WINDOW:
                    if win_bytes < STALL_MIN_BYTES:
                        raise RuntimeError(
                            "停滯: {} bytes/{:.0f}s".format(win_bytes, now - win_start))
                    win_start, win_bytes = now, 0
        if total and done < total:
            raise RuntimeError("下載不完整: {}/{} 位元組".format(done, total))
    finally:
        r.close()


def _download_over_path(url, dest, proxy_url, progress_cb, cancel_event):
    """用指定路徑（proxy_url 為 None 表示直連）完成一次下載。"""
    final_url, total, supports_range = _probe_download(url, proxy_url=proxy_url)

    if supports_range and total >= MIN_MULTIPART_SIZE:
        try:
            _download_multipart(final_url, dest, total, progress_cb,
                                cancel_event, proxy_url=proxy_url)
            return
        except UpdateCancelled:
            raise
        except Exception:  # noqa: BLE001 — 分段失敗退回單線再試
            if cancel_event is not None and cancel_event.is_set():
                raise UpdateCancelled()

    last_error = None
    for attempt in range(1, MAX_BLOCK_RETRIES + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateCancelled()
        try:
            _download_single(final_url, dest, total, progress_cb, cancel_event,
                             proxy_url=proxy_url)
            return
        except UpdateCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < MAX_BLOCK_RETRIES:
                time.sleep(min(5.0, 0.5 * (2 ** (attempt - 1))))
    raise RuntimeError("下載失敗: {}".format(last_error))


def download_file(url, dest, progress_cb=None, cancel_event=None,
                  use_proxy=True, log_cb=None):
    """下載 url 到 dest：多線程分段 + 智能分流 + 防呆退路。

    先實測「直連 + 設定中的代理」各條路徑的吞吐，挑最快的一條下載；該路徑
    中途失敗時自動改用次快的，最後保底直連。找不到代理、或代理全部不可用
    時，行為與純直連完全相同。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled()

    ranked = _rank_download_paths(url, use_proxy=use_proxy)

    last_error = None
    for proxy_url, speed, label in ranked:
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateCancelled()
        if log_cb is not None:
            try:
                log_cb("更新下載路徑: {} ({:.2f} MB/s)".format(
                    label, speed / 1048576))
            except Exception:  # noqa: BLE001 — 記錄不應影響下載
                pass
        try:
            _download_over_path(url, dest, proxy_url, progress_cb, cancel_event)
            return
        except UpdateCancelled:
            raise
        except Exception as e:  # noqa: BLE001 — 換下一條路徑再試
            last_error = e
            if log_cb is not None:
                try:
                    log_cb("路徑「{}」失敗，改用下一條: {}".format(label, e))
                except Exception:  # noqa: BLE001
                    pass
    raise RuntimeError("下載失敗: {}".format(last_error))


def _extract_zip(zip_path, dest_dir):
    """解壓到 dest_dir，並逐一做 zip-slip 防護。"""
    os.makedirs(dest_dir, exist_ok=True)
    dest_real = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = os.path.realpath(os.path.join(dest_dir, member.filename))
            if target != dest_real and not target.startswith(dest_real + os.sep):
                raise ValueError("zip-slip 攻擊偵測：{!r}".format(member.filename))
        zf.extractall(dest_dir)


def stage_update(info, new_dir, progress_cb=None, cancel_event=None, log_cb=None):
    """下載更新 zip → SHA-256 校驗 → 解壓到 new_dir（旁路目錄）。

    下載走多線程分段 + 智能分流（直連 vs 設定的 SOCKS5 代理，實測挑最快），
    具備進度回報、取消與防呆退路。校驗失敗會抛 RuntimeError 並清掉暫存
    zip，不留下半套狀態。
    """
    zip_path = new_dir + ".zip"
    download_file(info["url"], zip_path, progress_cb=progress_cb,
                  cancel_event=cancel_event, log_cb=log_cb)

    try:
        expected = None
        if info.get("checksum_url"):
            _download_to(info["checksum_url"], zip_path + ".sums")
            with open(zip_path + ".sums", "r", encoding="ascii", errors="replace") as f:
                checksum_text = f.read()
            expected = _parse_sha256_sums(checksum_text, info["name"])
        if expected:
            actual = _sha256_file(zip_path)
            if actual != expected:
                raise RuntimeError("SHA-256 校驗失敗，中止更新")

        if os.path.exists(new_dir):
            shutil.rmtree(new_dir, ignore_errors=True)
        _extract_zip(zip_path, new_dir)
    finally:
        for p in (zip_path, zip_path + ".sums"):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------- #
# 背景替換腳本（apply_update.ps1）
# ---------------------------------------------------------------------- #
def apply_script_content():
    """產生背景替換腳本（PowerShell，純 ASCII，參數由命令列傳入）。

    內容對照 NetRedirector 實測成功的版本：以 param 接收路徑、逐步行寫 log、
    等主程式退出 → 清理舊目錄 → 原子交換 → 重啟 → 清理。
    """
    return r'''param(
    [string]$Dist,
    [string]$NewDir,
    [string]$OldDir,
    [string]$Exe,
    [string]$ExeName,
    [string]$Log
)
$ErrorActionPreference = 'SilentlyContinue'
function L([string]$m) { Add-Content -Path $Log -Value ((Get-Date -Format o) + "  " + $m) -ErrorAction SilentlyContinue }

L ("apply start  Dist=[" + $Dist + "] NewDir=[" + $NewDir + "] OldDir=[" + $OldDir + "] Exe=[" + $Exe + "] ExeName=[" + $ExeName + "]")

# 1. wait for the app to fully exit (process name without .exe)
while (Get-Process -Name $ExeName -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 1 }
L "app exited"

# 2. remove leftover old version
if (Test-Path $OldDir) { Remove-Item $OldDir -Recurse -Force -ErrorAction SilentlyContinue }
L "old cleared"

# 3. atomic swap: Dist -> OldDir, NewDir -> Dist (retry for file unlock)
$ok = $false
for ($i = 0; $i -lt 15 -and -not $ok; $i++) {
    if ((Test-Path $Dist) -and (Test-Path $NewDir)) { Rename-Item $Dist $OldDir -Force -ErrorAction SilentlyContinue }
    if ((Test-Path $NewDir) -and -not (Test-Path $Dist)) { Rename-Item $NewDir $Dist -Force -ErrorAction SilentlyContinue }
    if (Test-Path $Exe) { $ok = $true } else { Start-Sleep -Seconds 1 }
}
L ("swap ok=" + $ok)

# 4. relaunch the app
if ($ok) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.WorkingDirectory = $Dist
        $psi.UseShellExecute = $false
        [System.Diagnostics.Process]::Start($psi) | Out-Null
        L "restart OK"
    } catch {
        L ("restart FAIL: " + $_)
    }
} else {
    L "restart skipped (swap failed)"
}

# 5. cleanup old version (sync retry; the new app is already relaunching)
if (Test-Path $OldDir) {
    $cleaned = $false
    for ($i = 0; $i -lt 10 -and -not $cleaned; $i++) {
        Remove-Item $OldDir -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $OldDir)) { $cleaned = $true } else { Start-Sleep -Seconds 1 }
    }
    L ("cleanup done=" + $cleaned)
}

L "apply done"
'''


def pending_paths():
    """回傳本次更新的路徑資訊（dist / new / old / exe / 腳本 / 日誌）。"""
    dist = current_dist_dir()
    install_dir = os.path.dirname(dist)
    return {
        "dist": dist,
        "new": dist + ".new",
        "old": dist + ".old",
        "exe": frozen_exe_path(),
        "install_dir": install_dir,
        "script": os.path.join(install_dir, "apply_update.ps1"),
        "log": os.path.join(install_dir, "update_apply.log"),
        "err": os.path.join(install_dir, "update_apply_err.log"),
    }


def spawn_apply_script(paths=None):
    """寫出並啟動背景替換腳本；回傳 True 表示已啟動（呼叫端應接著退出）。

    用 ``CREATE_NO_WINDOW`` 啟動 PowerShell（不能用 DETACHED_PROCESS，
    否則子程序可能未執行就消失）。參數走命令列，腳本以 utf-8-sig（BOM）寫入。
    """
    paths = paths or pending_paths()
    exe_base = os.path.basename(paths["exe"])
    exe_name = os.path.splitext(exe_base)[0]

    with open(paths["script"], "w", encoding="utf-8-sig") as f:
        f.write(apply_script_content())

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(paths["err"], "ab") as err_fd:
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", paths["script"],
                "-Dist", paths["dist"],
                "-NewDir", paths["new"],
                "-OldDir", paths["old"],
                "-Exe", paths["exe"],
                "-ExeName", exe_name,
                "-Log", paths["log"],
            ],
            cwd=paths["install_dir"],
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=err_fd,
        )
    return True
