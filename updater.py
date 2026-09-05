"""自動更新：檢查 / 下載 / SHA-256 校驗 / 原子替換 / 重啟（純邏輯，無 GUI）。

本模組把 docs/auto-update.md 的用戶端流程與踩坑紀錄落成可測試的函式，
重點對應關係：

- ``is_frozen()``          → 坑 1（Nuitka 不設 sys.frozen）
- ``frozen_exe_path()``    → 坑 2（Nuitka 的 sys.executable 指向內建 python.exe）
- ``generate_apply_script()``  → 坑 3/4/5/6/8（PS 5.1 的 Start-Process、編碼、
                              日誌路徑、Start-Job、正確程序名）
- ``_extract_zip()``       → zip-slip 防護
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile

import requests

import version


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
    而不是真正的 IntegratedApp.exe；用 ``sys.argv[0]`` 才能拿到對的路徑。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    p = os.path.abspath(argv0) if argv0 else ""
    if p and p.lower().endswith(".exe"):
        return p
    return os.path.abspath(sys.executable)


def exe_name():
    """回傳執行檔名（不含 .exe），供替換腳本 Get-Process -Name 使用。"""
    base = os.path.basename(frozen_exe_path())
    if base.lower().endswith(".exe"):
        return base[:-4]
    return base


def current_dist_dir():
    """回傳執行檔所在的資料夾（打包後即 .dist 目錄）。"""
    return os.path.dirname(frozen_exe_path())


def app_data_dir():
    """回傳跨版本穩定的應用資料夾（設定、日誌、替換腳本都放這裡）。

    此目錄在 .dist 之外，因此交換資料夾時不會被刪掉。
    """
    d = os.path.join(os.path.expanduser("~"), ".multi_socks_downloader")
    os.makedirs(d, exist_ok=True)
    return d


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
    """串流下載到 dest，避免整包塞進記憶體。"""
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)


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


def stage_update(info, new_dir):
    """下載更新 zip → SHA-256 校驗 → 解壓到 new_dir（旁路目錄）。

    校驗失敗會抛 RuntimeError 並清掉暫存 zip，不留下半套狀態。
    """
    zip_path = new_dir + ".zip"
    _download_to(info["url"], zip_path)

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
def _ps_quote(s):
    """把值包成單引號 PowerShell 字串，內部單引號加倍。"""
    return "'" + str(s).replace("'", "''") + "'"


_APPLY_SCRIPT_TEMPLATE = r'''$ErrorActionPreference = "Stop"

$ExePath   = {exe_path}
$DistDir   = {dist_dir}
$NewDir    = {new_dir}
$OldDir    = {old_dir}
$LogPath   = {log_path}
$ExeName   = {exe_name}

function Write-Log {{
    $line = ("{{0}} {{1}}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $args[0])
    Add-Content -Path $LogPath -Value $line
}}

try {{
    Write-Log "start exe=$ExePath dist=$DistDir new=$NewDir"

    # 1. wait for the running app process to actually exit
    $exited = $false
    for ($i = 0; $i -lt 60 -and -not $exited; $i++) {{
        $p = Get-Process -Name $ExeName -ErrorAction SilentlyContinue
        if (-not $p) {{ $exited = $true }} else {{ Start-Sleep -Seconds 1 }}
    }}
    if (-not $exited) {{ Write-Log "timeout waiting for process exit"; exit 1 }}
    Write-Log "app exited"

    # 2. drop any leftover old dir from a previous failed attempt
    if (Test-Path $OldDir) {{
        Remove-Item $OldDir -Recurse -Force -ErrorAction SilentlyContinue
    }}

    # 3. swap: dist -> old, new -> dist (retry for file-unlock delay)
    $swapped = $false
    for ($i = 0; $i -lt 10 -and -not $swapped; $i++) {{
        try {{
            if (Test-Path $OldDir) {{
                Remove-Item $OldDir -Recurse -Force -ErrorAction SilentlyContinue
            }}
            if (Test-Path $DistDir) {{ Rename-Item $DistDir $OldDir }}
            Rename-Item $NewDir $DistDir
            $swapped = $true
        }} catch {{
            Write-Log ("swap attempt {{0}} failed: {{1}}" -f $i, $_.Exception.Message)
            Start-Sleep -Seconds 1
        }}
    }}
    if (-not $swapped) {{ Write-Log "swap failed after retries"; exit 1 }}
    Write-Log "swap ok"

    # 4. restart via CreateProcess (PS 5.1 has no Start-Process -UseShellExecute)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $ExePath
    $psi.WorkingDirectory = $DistDir
    $psi.UseShellExecute = $false
    [System.Diagnostics.Process]::Start($psi) | Out-Null
    Write-Log "restarted"

    # 5. cleanup old dir (synchronous retry, no background job)
    $cleaned = $false
    for ($i = 0; $i -lt 10 -and -not $cleaned; $i++) {{
        Remove-Item $OldDir -Recurse -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $OldDir)) {{ $cleaned = $true }} else {{ Start-Sleep -Seconds 1 }}
    }}
    Write-Log "cleanup done=$cleaned"
    exit 0
}} catch {{
    Write-Log ("fatal: " + $_.Exception.Message)
    exit 1
}}
'''


def generate_apply_script(exe_path, dist_dir, new_dir, old_dir, log_path):
    """產生背景替換腳本內容（全 ASCII，避免 PS 5.1 無 BOM UTF-8 亂碼）。"""
    name = os.path.basename(exe_path)
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return _APPLY_SCRIPT_TEMPLATE.format(
        exe_path=_ps_quote(exe_path),
        dist_dir=_ps_quote(dist_dir),
        new_dir=_ps_quote(new_dir),
        old_dir=_ps_quote(old_dir),
        log_path=_ps_quote(log_path),
        exe_name=_ps_quote(name),
    )


def pending_paths():
    """回傳本次更新的路徑資訊（dist / new / old / exe / 腳本 / 日誌）。"""
    dist = current_dist_dir()
    return {
        "dist": dist,
        "new": dist + ".new",
        "old": dist + ".old",
        "exe": frozen_exe_path(),
        "script": os.path.join(app_data_dir(), "apply_update.ps1"),
        "log": os.path.join(app_data_dir(), "update.log"),
    }


def spawn_apply_script(paths=None):
    """寫出並分離啟動背景替換腳本；回傳 True 表示已啟動（呼叫端應接著退出）。

    用 ``DETACHED_PROCESS`` 確保主程式退出後腳本仍能獨立存活。
    """
    paths = paths or pending_paths()
    script = generate_apply_script(
        exe_path=paths["exe"],
        dist_dir=paths["dist"],
        new_dir=paths["new"],
        old_dir=paths["old"],
        log_path=paths["log"],
    )
    with open(paths["script"], "w", encoding="ascii", newline="\r\n") as f:
        f.write(script)

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", paths["script"],
    ]

    stderr_log = os.path.splitext(paths["log"])[0] + ".stderr.log"
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    with open(stderr_log, "ab") as err_fh:
        kwargs["stdout"] = err_fh
        kwargs["stderr"] = subprocess.STDOUT
        subprocess.Popen(cmd, **kwargs)
    return True