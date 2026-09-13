"""Windows 開機自動啟動：把本程式登錄到「目前使用者」的 Run 機碼。

只寫入 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，不寫 HKLM，
因此不需系統管理員權限，且停用時只移除自己寫入的值、不影響其他使用者或
其他程式的啟動項目。
"""

import os
import sys

if sys.platform == "win32":
    import winreg

# 目前使用者的啟動項目路徑（相對於 HKCU）；登入時由 Explorer 逐一執行。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 登錄值名稱：固定不變，停用時才能精準移除本程式寫入的項目。
VALUE_NAME = "MultiSocksDownloader"

_HKCU = winreg.HKEY_CURRENT_USER if sys.platform == "win32" else None


def _is_frozen():
    """是否為打包後的獨立執行檔（涵蓋 PyInstaller / Nuitka）。

    Nuitka 預設「不會」設定 sys.frozen，而是注入 __compiled__ 全域變數，
    因此不能只檢查 sys.frozen，否則打包版會被誤判為原始碼模式、寫出
    指向 dist\\python.exe 與 .py 腳本的錯誤命令。
    """
    if getattr(sys, "frozen", False):          # PyInstaller / cx_Freeze
        return True
    if hasattr(sys, "_MEIPASS"):               # PyInstaller onefile
        return True
    if globals().get("__compiled__", False):   # Nuitka 注入的模組全域變數
        return True
    return False


def _frozen_exe_path():
    """取得真正執行中的程式 exe 路徑。

    Nuitka standalone 會把 sys.executable 設成 dist 內建的 python.exe
    （甚至是不存在的路徑），真正的程式 exe 要用 sys.argv[0] 才拿得到。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    p = os.path.abspath(argv0) if argv0 else ""
    if p and p.lower().endswith(".exe"):
        return p
    exe = sys.executable or ""
    if exe.lower().endswith(".exe"):
        return os.path.abspath(exe)
    return p


def executable_command():
    """回傳開機啟動使用的命令列（不含任何檔案參數）。

    打包後直接執行真正的程式 exe；原始碼執行時改用無視窗的 pythonw.exe
    執行入口腳本 MultiSocksDownloader.py，避免開機時閃出命令列視窗。
    """
    if _is_frozen():
        return '"{}"'.format(_frozen_exe_path())
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    pythonw = os.path.join(exe_dir, "pythonw.exe")
    exe = pythonw if os.path.isfile(pythonw) else sys.executable
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "MultiSocksDownloader.py"
    )
    return '"{}" "{}"'.format(exe, script)


def _command_target(value):
    """從啟動命令取出第一個（執行檔）路徑；取不到時回傳 None。"""
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith('"'):
        end = value.find('"', 1)
        if end > 1:
            return value[1:end]
    return value.split(" ", 1)[0]


def is_enabled():
    """判斷目前使用者是否已設定本程式開機自動啟動。

    除了登錄值存在，也確認命令指向的執行檔仍存在；若指向失效路徑
    （例如舊版本寫入的錯誤命令），視為未啟用，避免勾選框顯示錯誤狀態。
    """
    if sys.platform != "win32":
        return False
    try:
        with winreg.OpenKey(_HKCU, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except OSError:
        return False
    target = _command_target(value)
    return bool(target) and os.path.isfile(target)


def enable():
    """把本程式寫入目前使用者的啟動項目。回傳 True 表示成功。"""
    if sys.platform != "win32":
        return False
    try:
        with winreg.CreateKeyEx(
            _HKCU, RUN_KEY, 0, winreg.KEY_WRITE | winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(
                key, VALUE_NAME, 0, winreg.REG_SZ, executable_command())
    except OSError:
        return False
    return True


def disable():
    """移除本程式的啟動項目；未設定過時為無操作。"""
    if sys.platform != "win32":
        return
    try:
        with winreg.OpenKey(_HKCU, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except OSError:
        pass  # 機碼或值不存在時忽略
