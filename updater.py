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
# 單段大小下限（不是「目標」：實際段大小是 max(此值, ceil(total/MAX_BLOCKS))）。
# 這是實測調出來的，不是拍的：
# GitHub release CDN 每次 Range 請求的「發出 -> 回應標頭」要 1.3 秒以上，
# 而 1 MB 資料在 300 Mbps 上只帶 27 毫秒 —— 1 MB 分段有 98% 的時間在等這個
# 固定延遲，段數越多等越多次。實測同一個 91 MB 資產 (2026-10-03，走 SOCKS5/5G
# 代理，只計傳輸段、交錯順序三輪)：
#     32 段 x 2.8MB  -> 5.45s (140.4 Mbps)
#      8 段 x 11.4MB -> 4.19s (182.8 Mbps)   ← 段數越少越快，區間不重疊
# 故下限設 8 MB、上限 8 段。
TARGET_BLOCK_SIZE = 8 * 1024 * 1024   # 單段大小下限（見上）
MAX_BLOCKS = 8                        # 分段數上限 (= 最大並行連線數)
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
PROXY_PROBE_TIMEOUT = 6               # 等第一個位元組的逾時（秒）
PROXY_PROBE_SECONDS = 1.5             # 首個位元組之後，最多量這麼久的吞吐
PROXY_PROBE_MAX_BYTES = 8 * 1024 * 1024   # 量測視窗的位元組上限
PROXY_PROBE_MIN_SECONDS = 0.3         # 視窗短於此值視為樣本不足，不予採用
PROXY_PROBE_TOTAL_TIMEOUT = 8         # 全部路徑量測的總逾時（秒）

# --- 自適應連線數（見 _adaptive_threads） ---
# 總開關。預設關閉：關閉時段數完全由 TARGET_BLOCK_SIZE / MAX_BLOCKS 決定，
# 行為與舊版一模一樣。也可以在 config.json 頂層寫
# "adaptive_concurrency": true 單獨打開。
ADAPTIVE_CONCURRENCY = False
ADAPTIVE_CONCURRENCY_KEY = "adaptive_concurrency"
ADAPTIVE_PROBE_THREADS = 4           # 試探時開幾條連線
ADAPTIVE_PROBE_SECONDS = 1.2         # 試探的共同視窗長度（秒）
ADAPTIVE_GAIN = 1.35                 # 聚合吞吐至少要贏基準這麼多倍才值得多開
ADAPTIVE_LATENCY_TOLERANCE = 1.25    # 連線延遲最多可漲這麼多倍（Vegas 式煞車）
# 試探本身要付 (TTFB + 視窗) ≈ 2.5 秒；多開連線能省下的時間是 est*(1-1/gain)，
# 要打平得 est > 2.5 * gain/(gain-1) ≈ 9.6 秒 (gain=1.35)。估計下載時間不到這個
# 門檻就不試探 —— 試探成本比可能省下的還多。實務上這代表：快線不會啟動，
# 慢線（數十 KB/s 起跳）才會 —— 而慢線正是這個 updater 存在的理由。
ADAPTIVE_MIN_EST_SECONDS = 10.0
ADAPTIVE_MIN_BYTES = 16 * 1024 * 1024   # 檔案太小也不值得分段試探


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


def _config_flag(key, default=False, config_paths=None):
    """從 config.json 頂層讀一個布林開關。

    任何問題（檔案不存在、JSON 破損、型別不對）都回 ``default`` —— 與
    :func:`load_proxy_urls` 同一原則：更新流程絕不因設定檔問題而中斷。
    字串寫法 ``"true"`` / ``"yes"`` / ``"on"`` / ``"1"`` 也算真。
    """
    for path in (config_paths or _config_search_paths()):
        try:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and key in data:
            value = data[key]
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
    return bool(default)


def _total_from_range_headers(headers, status_code):
    """從 (Range) 回應標頭取出檔案總大小；0 表示未知。

    ``_probe_download`` 與 ``_measure_path_detailed`` 都要這個判斷，抽出來
    避免兩份實作對「206 但沒有 content-range」之類的邊界給出不同答案。
    """
    if status_code == 206:
        m = re.search(r"/(\d+)\s*$", headers.get("content-range", "") or "")
        if m:
            return int(m.group(1))
    return int(headers.get("content-length", 0) or 0)


# 伺服器「明確拒絕」Range 請求的狀態碼。實測 http.speed.hinet.net 就是這種：
# 回應標頭宣告 ``Accept-Ranges: bytes``，但任何 Range 請求（連 ``bytes=0-0``
# 都算）都回 416 —— 宣告與行為不一致。這種伺服器只能整檔抓。
_RANGE_REJECT_CODES = (400, 416, 501)


def _range_rejected(status_code):
    """伺服器是否明確**拒絕** Range 請求（而不是忽略它、回整份 200）。"""
    return status_code in _RANGE_REJECT_CODES


def _get_allow_range_fallback(url, headers, proxy_url, timeout):
    """送出請求；若伺服器拒絕 Range，拿掉 Range 標頭重送一次。

    回傳 ``(response, used_range, t_issued)``；``t_issued`` 是最後一次請求真正
    送出的時間（供呼叫端算 TTFB），呼叫端負責關閉 response。

    沒有這層退路時，一個「宣告 Accept-Ranges 卻回 416」的伺服器會讓
    ``_probe_download`` 直接拋例外，於是 ``_download_over_path`` 根本走不到
    它自己的單線備援 —— 整個下載 0.02 秒就失敗，而 curl 抓同一個 URL 完全正常
    （實測 http.speed.hinet.net/test_040m.zip）。

    這是**通用退路**，不是針對特定主機的特例：不寫死任何 hostname、也不做
    HTTP→HTTPS 升級，任何「拒絕 Range」的伺服器都適用。
    """
    t = time.monotonic()
    r = requests.get(url, headers=headers, stream=True,
                     proxies=_proxy_map(proxy_url),
                     timeout=(_TIMEOUT_CONNECT, timeout),
                     allow_redirects=True)
    if not _range_rejected(r.status_code):
        return r, True, t
    r.close()
    plain = {k: v for k, v in headers.items() if k.lower() != "range"}
    t = time.monotonic()
    r = requests.get(url, headers=plain, stream=True,
                     proxies=_proxy_map(proxy_url),
                     timeout=(_TIMEOUT_CONNECT, timeout),
                     allow_redirects=True)
    return r, False, t


def _measure_path_detailed(url, proxy_url, timeout=PROXY_PROBE_TIMEOUT,
                           seconds=PROXY_PROBE_SECONDS,
                           max_bytes=PROXY_PROBE_MAX_BYTES):
    """量測一條路徑的吞吐；回傳 ``(speed_bps|None, info dict)``。

    分兩段計時，把「連線 / 302 轉址 / TTFB」排除在分母之外：

        t0 ──(握手、轉址、TTFB)──▶ 首個 chunk ──(量測視窗)──▶ 結束
                                    └─── 只有這段算吞吐 ───┘

    舊版從 t0 起算、固定抓 256 KB，於是量到的其實是 ``1 / TTFB``：
    GitHub 資產 TTFB 實測約 970 毫秒，而 256 KB 在 300 Mbps 上只帶 21 毫秒，
    回報值低估約 100 倍（119 Mbps 的線被量成 1.1 Mbps），選路等於擲骰子。

    例外是「整個檔都抓完了」的情形：那時回報的是**含 TTFB 的牆鐘**速度。
    因為完整傳輸的端到端時間才是使用者實際感受到的數字，而小檔案若用穩定態
    會反過來高估高 TTFB 的路徑。

    無法量測時回 ``(None, info)``，``info["error"]`` 說明原因。另外帶回
    ``final_url`` / ``total`` / ``supports_range``，讓同一條路徑的下載階段
    可以直接沿用這次探針的結果，不必再付一次 TTFB。
    """
    headers = {
        "Range": "bytes=0-{}".format(max_bytes - 1),
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    info = {"ttfb": None, "bytes": 0, "elapsed": 0.0, "error": None,
            "final_url": None, "total": 0, "supports_range": False,
            "speed": None}
    r = None
    try:
        r, used_range, t0 = _get_allow_range_fallback(
            url, headers, proxy_url, timeout)
        if r.status_code not in (200, 206):
            info["error"] = "HTTP {}".format(r.status_code)
            return None, info
        # 這次探針本來就會跟完轉址、也拿得到回應標頭，於是「轉址後的最終網址」
        # 與「檔案總大小」是免費的副產品。記下來給下載階段重用，同一條路徑就
        # 不必再付一次完整 TTFB（本專案 91 MB 資產實測該次探針 1.34~1.41 秒，
        # 下載段因此由 6.25 秒降到 4.83 秒）。
        info["final_url"] = r.url or url
        info["supports_range"] = bool(used_range and r.status_code == 206)
        info["total"] = _total_from_range_headers(r.headers, r.status_code)

        chunks = r.iter_content(CHUNK_SIZE)
        first = None
        for chunk in chunks:
            if chunk:
                first = chunk
                break
        if not first:
            info["error"] = "無資料"
            return None, info

        t_first = time.monotonic()
        info["ttfb"] = t_first - t0
        got = len(first)
        capped = got >= max_bytes      # 首個 chunk 就撞到上限（極快的線才可能）
        for chunk in chunks:
            if not chunk:
                continue
            got += len(chunk)
            if got >= max_bytes:
                capped = True
                break
            if time.monotonic() - t_first >= seconds:
                break
        elapsed = time.monotonic() - t_first
        info["bytes"] = got
        info["elapsed"] = elapsed
        complete = bool(info["total"]) and got >= info["total"]
        if complete:
            # 整個檔都抓完了，這是完整樣本。此時要用**含 TTFB 的牆鐘**，不能用
            # 穩定態：小檔案用穩定態會把「TTFB 高但吞吐大」的代理誤判成最快
            # （實測 0.5 MB 在 95 Mbps 直連上，穩定態算出 262 Mbps，但端到端
            # 0.33 秒其實比代理的 1.3 秒快得多）。
            wall = time.monotonic() - t0
            if wall <= 0:
                info["error"] = "無法計時"
                return None, info
            speed = got / wall
            info["speed"] = speed
            return speed, info
        # 走到這裡代表視窗是被 seconds 或位元組上限截斷的。舊版把「資料提早
        # 結束」一律當成樣本不足，於是檔案比「0.3 秒能抓完的量」還小時，明明
        # 整份都抓到了卻被丟棄。實測（95 Mbps 直連）：0.25 / 0.5 / 1 / 2 MB
        # 全被判樣本不足，4 MB 起才通過 —— 也就是任何小於約 3.5 MB 的資產，
        # 都會讓所有快路徑一起變成不可用，選路只剩 speed=0.0 的直連保底，
        # 代理永遠選不上（正是「每次都選不到快線」的成因之一）。
        if not capped and elapsed < PROXY_PROBE_MIN_SECONDS:
            info["error"] = "樣本不足 {:.2f}s".format(elapsed)
            return None, info
        if elapsed <= 0:
            info["error"] = "樣本不足 0.00s"
            return None, info
        speed = got / elapsed
        info["speed"] = speed
        return speed, info
    except Exception as e:  # noqa: BLE001 — 代理掛掉/逾時都只代表這條不可用
        info["error"] = "{}: {}".format(type(e).__name__, e)
        return None, info
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:  # noqa: BLE001
                pass


def _measure_path(url, proxy_url, timeout=PROXY_PROBE_TIMEOUT,
                  seconds=PROXY_PROBE_SECONDS,
                  max_bytes=PROXY_PROBE_MAX_BYTES):
    """量測一條路徑的吞吐 (bytes/s)；失敗或無資料回 None。"""
    speed, _info = _measure_path_detailed(url, proxy_url, timeout, seconds,
                                          max_bytes)
    return speed


def _measure_aggregate_throughput(final_url, total, proxy_url, n,
                                  seconds=ADAPTIVE_PROBE_SECONDS,
                                  max_bytes=PROXY_PROBE_MAX_BYTES):
    """同時開 ``n`` 條連線，量「同一段時間內合計抓回多少位元組」。

    回傳 ``(bytes_per_sec|None, ttfb|None, reason)``；量不到時第一個值為 None。
    三個設計要點都是實測踩出來的：

    1. **共同視窗**。每條連線要各自等回應標頭，TTFB 可以差 0.2 秒以上，所以
       不能各算各的起點（那會讓先開始的那條被算得偏快）。這裡用一道 gate：
       全部連線都拿到首個 chunk 之後，才一起開始計時。
    2. **每條連線從不同偏移起讀**（``i * total / n``），且各自有**位元組上限**
       ``min(max_bytes, total // n)``。上限一定 ≤ 每條能分到的區間，所以不會
       有哪條先撞到檔尾而提早收工、把吞吐算低。
    3. **位元組上限是必要的**。沒有它，快路徑會在視窗內把整個檔抓完，量到的
       其實是「檔案大小 ÷ 視窗」——實測就是這樣讀出一個假的 2.01×
       （34 MB ÷ 1.5 s = 182.8 Mbps，連續三輪一字不差）。

    注意這是**聚合**量測：若各連線先後撞到上限，先完成的那條會閒置，於是
    聚合值偏**低**。偏差方向是保守的（只會讓我們少開連線），可以接受。
    """
    n = max(1, int(n))
    if total <= 0 or total < n:
        return None, None, "檔案大小未知或不足以分段"
    step = total // n
    per_cap = max(1, min(int(max_bytes), step))
    if per_cap < 256 * 1024:
        return None, None, "每段可分到的區間太小"

    lock = threading.Lock()
    gate = threading.Event()
    ready = threading.Semaphore(0)
    spans = []
    agg = {"bytes": 0, "capped": False, "errors": [], "ttfb": []}

    def worker(i):
        start = i * step
        headers = {
            "Range": "bytes={}-".format(start),
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "identity",
        }
        r = None
        try:
            t_issue = time.monotonic()
            r = requests.get(final_url, headers=headers, stream=True,
                             proxies=_proxy_map(proxy_url),
                             timeout=(_TIMEOUT_CONNECT, _TIMEOUT_READ),
                             allow_redirects=True)
            if r.status_code not in (200, 206):
                raise RuntimeError("HTTP {}".format(r.status_code))
            chunks = r.iter_content(CHUNK_SIZE)
            first = None
            for chunk in chunks:
                if chunk:
                    first = chunk
                    break
            if not first:
                raise RuntimeError("無資料")
            with lock:
                agg["ttfb"].append(time.monotonic() - t_issue)
            ready.release()
            if not gate.wait(timeout=_TIMEOUT_CONNECT):
                raise RuntimeError("等待共同視窗逾時")
            t0 = time.monotonic()
            deadline = t0 + seconds
            got = len(first)
            capped = got >= per_cap
            if not capped:
                for chunk in chunks:
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got >= per_cap:
                        capped = True
                        break
                    if time.monotonic() >= deadline:
                        break
            with lock:
                agg["bytes"] += got
                agg["capped"] = agg["capped"] or capped
                spans.append((t0, time.monotonic()))
        except Exception as e:  # noqa: BLE001 — 單條連線失敗只代表這次量不到
            with lock:
                agg["errors"].append("{}: {}".format(type(e).__name__, e))
            try:
                ready.release()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass

    workers = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(n)]
    for t in workers:
        t.start()
    deadline = time.monotonic() + PROXY_PROBE_TIMEOUT + 2.0
    arrived = 0
    while arrived < n:
        if not ready.acquire(timeout=max(0.0, deadline - time.monotonic())):
            break
        arrived += 1
    gate.set()
    # gate 開了之後，就緒的連線會在 seconds 內收工。還在等標頭的那些是
    # daemon 執行緒，逾時就放生（它們自己會撞上 _TIMEOUT_READ 而結束）。
    for t in workers:
        t.join(timeout=seconds + 3.0)

    if arrived < n or not spans:
        if agg["errors"]:
            return None, None, agg["errors"][0]
        return None, None, "只有 {} 條連線就緒".format(arrived)
    elapsed = max(s[1] for s in spans) - min(s[0] for s in spans)
    if elapsed <= 0:
        return None, None, "無法計時"
    ttfb = max(agg["ttfb"]) if agg["ttfb"] else None
    return agg["bytes"] / elapsed, ttfb, None


def _adaptive_threads(final_url, total, proxy_url, base_threads, probe_info):
    """決定這次要用幾條連線。回傳 ``(threads, 說明)``。

    基準是**單連線**的吞吐 ``bw1``——選路階段（:func:`_rank_download_paths`）
    已經量過，免費。試探時比較 ``k`` 條連線的**聚合**吞吐，只有**吞吐明顯
    變好、而且延遲沒變差**才採用多連線。

    延遲那一條是 Vegas / FAST 那一類的做法：加連線若讓連線延遲上升，代表
    只是把資料塞進同一個佇列，並沒有真的變快。延遲的雜訊遠比吞吐小，所以
    拿它當煞車；但實測這條路徑上「吞吐有沒有漲」才是主要判準，延遲只是
    額外的否決權。

    只在**有證據**時才改變行為：

    ==========================================  ====================
    情況                                        採用的連線數
    ==========================================  ====================
    沒有基準值（沒量到 bw1）                     沿用 ``base``（無證據不動）
    檔案太小／估計下載時間太短，不試探           沿用 ``base``（尊重設定值）
    試探成功，吞吐 ≥ K 倍且延遲沒變差            用 ``k``
    試探成功，但吞吐沒明顯變好                   **退回 1 條**
    試探失敗（連不上／逾時）                     沿用 ``base``
    ==========================================  ====================
    """
    base = max(1, int(base_threads))
    info = probe_info or {}
    bw1 = info.get("speed")
    if bw1 is None and info.get("elapsed"):
        bw1 = info["bytes"] / info["elapsed"]
    if base <= 1 or not final_url or not total or not bw1:
        return base, "無基準值，沿用預設 {} 條".format(base)
    if total < ADAPTIVE_MIN_BYTES:
        return base, "檔案僅 {:.1f} MB，不值得試探".format(total / 1048576.0)
    ttfb1 = info.get("ttfb") or 0.0
    est = ttfb1 + total / bw1
    if est < ADAPTIVE_MIN_EST_SECONDS:
        return base, "估計僅 {:.1f}s，試探成本划不來".format(est)
    k = min(base, ADAPTIVE_PROBE_THREADS)
    if k <= 1:
        return base, "沒有可試探的連線數"
    bw_k, ttfb_k, reason = _measure_aggregate_throughput(
        final_url, total, proxy_url, k)
    if not bw_k:
        return base, "試探失敗（{}），沿用預設 {} 條".format(reason, base)
    gain = bw_k / bw1
    lat = (ttfb_k / ttfb1) if (ttfb_k and ttfb1) else 1.0
    if gain >= ADAPTIVE_GAIN and lat <= ADAPTIVE_LATENCY_TOLERANCE:
        return k, "試探 {} 條：吞吐 {:.2f}x、延遲 {:.2f}x → 採用".format(k, gain, lat)
    return 1, ("試探 {} 條：吞吐 {:.2f}x、延遲 {:.2f}x → 退回單連線"
               .format(k, gain, lat))


def _rank_download_paths(url, use_proxy=True):
    """並行量測直連與各代理，回傳由快到慢的
    ``[(proxy_url, speed, label, info)]``。``info`` 是該路徑的探針細節
    （含 ``final_url`` / ``total`` / ``supports_range``），讓下載階段可以
    直接沿用這次探針，不必為了拿這三個值再付一次 TTFB。

    防呆保證：
    - 沒有設定代理時直接回 ``[(None, 0.0, "直連", {})]``，不增加任何延遲。
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
        return [(None, 0.0, "直連", {})]

    candidates = [(p["url"], p["name"]) for p in proxies]
    candidates.append((None, "直連"))

    results = {}
    lock = threading.Lock()

    def probe(proxy_url, label):
        speed, info = _measure_path_detailed(url, proxy_url)
        if speed is not None:
            with lock:
                results[proxy_url] = (speed, label, info)

    threads = [threading.Thread(target=probe, args=(u, n), daemon=True)
               for u, n in candidates]
    for t in threads:
        t.start()
    deadline = time.monotonic() + PROXY_PROBE_TOTAL_TIMEOUT
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))

    with lock:
        ranked = [(u, s, n, i) for u, (s, n, i) in results.items()]
    ranked.sort(key=lambda item: -item[1])
    if not ranked:
        return [(None, 0.0, "直連", {})]
    if not any(u is None for u, _s, _n, _i in ranked):
        ranked.append((None, 0.0, "直連", {}))
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

    伺服器若**拒絕** Range（見 ``_RANGE_REJECT_CODES``），這裡會自動改用不帶
    Range 的請求，並回報 ``supports_range=False`` —— 呼叫端因此會走單線下載，
    而不是整個失敗。
    """
    headers = {
        "Range": "bytes=0-0",
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "identity",
    }
    r, used_range, _t = _get_allow_range_fallback(url, headers, proxy_url,
                                                  timeout)
    try:
        r.raise_for_status()
        final_url = r.url or url
        total = _total_from_range_headers(r.headers, r.status_code)
        return final_url, total, bool(used_range and r.status_code == 206)
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
                        proxy_url=None, max_blocks=None):
    """把檔案切成多段並行下載到 dest（dest 會先配置成 total 大小）。

    段數 = clamp(ceil(total / TARGET_BLOCK_SIZE), 1, cap)，worker 執行緒數 =
    段數。``cap`` 預設 ``MAX_BLOCKS``；``max_blocks`` 有傳時取兩者較小值 ——
    自適應連線數（:func:`_adaptive_threads`）就是靠它把段數壓到 1。段數少了
    就是並行連線少了，舊版只能改常數，這裡改成可以逐次決定。

    TARGET_BLOCK_SIZE 是**下限**而非目標，用意是壓低段數：這條 CDN 每次 Range
    請求的回應標頭要等 1.3 秒以上，段太多等於把時間花在重複的固定延遲上
    （見 TARGET_BLOCK_SIZE 的實測註解）。
    """
    cap = MAX_BLOCKS if max_blocks is None else max(
        1, min(int(max_blocks), MAX_BLOCKS))
    block_size = max(TARGET_BLOCK_SIZE, (total + cap - 1) // cap)
    bounds = []
    start = 0
    while start < total and len(bounds) < cap:
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


def _download_over_path(url, dest, proxy_url, progress_cb,
                        cancel_event, probe_info=None, threads=None,
                        log_cb=None):
    """用指定路徑（proxy_url 為 None 表示直連）完成一次下載。

    ``probe_info`` 是選路階段對同一條路徑的探針結果。帶著它就能沿用已經付過
    的那次 TTFB（取得轉址後網址與檔案大小），省掉一次多餘的
    ``Range: bytes=0-0`` —— 本專案 91 MB 資產實測那一次要 1.34~1.41 秒，下載段
    因此由 6.25 秒降到 4.83 秒。缺漏或不可用時退回自行探測，行為與舊版完全相同。

    ``ADAPTIVE_CONCURRENCY``（或 config.json 的 ``adaptive_concurrency``）打開
    時，會先用 :func:`_adaptive_threads` 決定段數；關閉時段數照舊由
    TARGET_BLOCK_SIZE / MAX_BLOCKS 決定，行為與舊版一模一樣。
    """
    info = probe_info or {}
    final_url = info.get("final_url")
    total = int(info.get("total") or 0)
    supports_range = bool(info.get("supports_range"))
    if not final_url or not total:
        probed = _probe_download(url, proxy_url=proxy_url)
        final_url, total, supports_range = probed

    if supports_range and total >= MIN_MULTIPART_SIZE:
        eff_blocks = None if threads is None else max(1, int(threads))
        if ADAPTIVE_CONCURRENCY or _config_flag(ADAPTIVE_CONCURRENCY_KEY):
            eff_blocks, why = _adaptive_threads(
                final_url, total, proxy_url,
                eff_blocks if eff_blocks is not None else MAX_BLOCKS, info)
            if log_cb is not None:
                try:
                    log_cb("自適應連線數：{}".format(why))
                except Exception:  # noqa: BLE001 — 記錄不應影響下載
                    pass
        try:
            _download_multipart(final_url, dest, total, progress_cb,
                                cancel_event, proxy_url=proxy_url,
                                max_blocks=eff_blocks)
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
        # 每次重試都重新探測：GitHub 的簽名 URL 有時效，而上面的 final_url 可能
        # 是選路階段帶回來的（最多已過了 PROXY_PROBE_TOTAL_TIMEOUT 秒）。沿用
        # 一個已失效的 URL 只會拿到 403，重試幾次結果都一樣。
        try:
            probed = _probe_download(url, proxy_url=proxy_url)
            fresh_url, fresh_total, _ = probed
            if fresh_url:
                final_url = fresh_url
            if fresh_total:
                total = fresh_total
        except Exception:  # noqa: BLE001 — 探測失敗就沿用既有 URL 再試
            pass
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
                  use_proxy=True, log_cb=None, threads=None):
    """下載 url 到 dest：多線程分段 + 智能分流 + 防呆退路。

    先實測「直連 + 設定中的代理」各條路徑的吞吐，挑最快的一條下載；該路徑
    中途失敗時自動改用次快的，最後保底直連。找不到代理、或代理全部不可用
    時，行為與純直連完全相同。

    選路時的探針會一併帶回「轉址後網址」與「檔案大小」，下載階段直接沿用，
    因此同一條路徑不會被探測兩次（省下的那一次約 1.34~1.41 秒，下載段由
    6.25 秒降到 4.83 秒）。
    """
    if cancel_event is not None and cancel_event.is_set():
        raise UpdateCancelled()

    ranked = _rank_download_paths(url, use_proxy=use_proxy)

    last_error = None
    for proxy_url, speed, label, probe_info in ranked:
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateCancelled()
        if log_cb is not None:
            try:
                log_cb("更新下載路徑: {} ({:.2f} MB/s)".format(
                    label, speed / 1048576))
            except Exception:  # noqa: BLE001 — 記錄不應影響下載
                pass
        try:
            _download_over_path(url, dest, proxy_url, progress_cb,
                                cancel_event, probe_info=probe_info,
                                threads=threads, log_cb=log_cb)
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
