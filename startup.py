"""Windows 開機自動啟動：把本程式登錄到「目前使用者」的 Run 機碼。

只寫入 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，不寫 HKLM，
因此不需系統管理員權限，且停用時只移除自己寫入的值、不影響其他使用者或
其他程式的啟動項目。
"""

import os
import sys

import runtime_env

if sys.platform == "win32":
    import winreg

# 目前使用者的啟動項目路徑（相對於 HKCU）；登入時由 Explorer 逐一執行。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 登錄值名稱：固定不變，停用時才能精準移除本程式寫入的項目。
VALUE_NAME = "MultiSocksDownloader"

_HKCU = winreg.HKEY_CURRENT_USER if sys.platform == "win32" else None


def executable_command():
    """回傳開機啟動使用的命令列（不含任何檔案參數）。

    打包後直接執行真正的程式 exe；原始碼執行時改用無視窗的 pythonw.exe
    執行入口腳本 MultiSocksDownloader.py，避免開機時閃出命令列視窗。
    """
    if runtime_env.is_frozen():
        return '"{}"'.format(runtime_env.frozen_exe_path())
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
