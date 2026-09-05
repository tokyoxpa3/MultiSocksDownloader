"""自動更新：檢查 / 下載 / SHA-256 校驗 / 原子替換 / 重啟（純邏輯，無 GUI）。

本模組把 docs/auto-update.md 的用戶端流程與踩坑紀錄落成可測試的函式，
重點對應關係：

- ``is_frozen()``          → 坑 1（Nuitka 不設 sys.frozen）
- ``frozen_exe_path()``    → 坑 2（Nuitka 的 sys.executable 指向內建 python.exe）
- ``apply_script_content()``  → 坑 3/4/5/6/8（PS 5.1 的 Start-Process、編碼、
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
