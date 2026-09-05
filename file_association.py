"""Windows 檔案關聯：把 *.torrent 的預設開啟程式設為本程式。

只寫入「目前使用者」的登錄檔 HKCU\\Software\\Classes，不寫 HKLM，
因此不需要系統管理員權限，且移除關聯時可完整還原、不會動到系統層級。

Windows 8 之後，Explorer 對「預設程式」另用 UserChoice 鍵（帶雜湊）保護，
程式無法靜默奪取預設開啟權（直接寫入的雜湊會被系統視為無效並忽略）。
因此本模組採合規做法：
  1. 註冊標準 Classes 關聯與 ProgID，並把 ProgID 一併寫進 FileExts 的
     OpenWithProgids，確保程式一定出現在「開啟方式」清單；
  2. 若 UserChoice 尚未指向本程式，由 trigger_system_dialog() 彈出系統
     「你要如何開啟此檔案」對話框，請使用者點一次確認，讓 Windows 寫入
     帶合法雜湊的 UserChoice。
"""

import os
import sys
import time
import tempfile
import threading

if sys.platform == "win32":
    import ctypes
    import winreg

EXTENSION = ".torrent"
PROG_ID = "MultiSocksDownloader.torrent"
FRIENDLY_NAME = "多線程下載器"

# 使用者層級的 Classes 路徑（相對於 HKCU），不需管理員權限。
_CLASSES_ROOT = r"Software\Classes"
# Win10/11 的 FileExts 路徑：Explorer 依此決定「開啟方式」清單與 UserChoice。
_FILEEXTS_ROOT = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
_HKCU = winreg.HKEY_CURRENT_USER if sys.platform == "win32" else None


def _classes_key(*parts):
    """把相對片段組合成 HKCU\\Software\\Classes 底下的完整子鍵路徑。"""
    return "\\".join((_CLASSES_ROOT,) + tuple(parts))


def _fileexts_key(*parts):
    """把相對片段組合成 HKCU\\...\\Explorer\\FileExts 底下的完整子鍵路徑。"""
    return "\\".join((_FILEEXTS_ROOT,) + tuple(parts))


def _is_frozen():
    """Nuitka standalone 打包後設為 True；原始碼執行時為 False。"""
    return bool(getattr(sys, "frozen", False))


def executable_command():
    """回傳檔案關聯使用的開啟命令（含 "%1" 佔位符）。

    打包後的獨立執行檔直接呼叫自己；原始碼執行時改用無視窗的 pythonw.exe
    執行入口腳本 MultiSocksDownloader.py，避免雙擊 .torrent 時閃出命令列視窗。
    """
    if _is_frozen():
        return '"{}" "%1"'.format(sys.executable)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    pythonw = os.path.join(exe_dir, "pythonw.exe")
    exe = pythonw if os.path.isfile(pythonw) else sys.executable
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "MultiSocksDownloader.py"
    )
    return '"{}" "{}" "%1"'.format(exe, script)


def icon_source():
    """回傳 DefaultIcon 使用的圖示來源。

    打包後圖示內嵌於執行檔（index 0）；原始碼執行時指向專案內的 app_icon.ico。
    """
    if _is_frozen():
        return '"{}",0'.format(sys.executable)
    ico = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "app_icon.ico"
    )
    return '"{}",0'.format(ico)


def _user_choice_prog_id():
    """讀取 Win10/11 真正決定雙擊開啟程式的 UserChoice ProgId；不存在回傳 None。"""
    try:
        with winreg.OpenKey(
            _HKCU, _fileexts_key(EXTENSION, "UserChoice"), 0, winreg.KEY_READ
        ) as key:
            prog_id, _ = winreg.QueryValueEx(key, "ProgId")
            return prog_id
    except OSError:
        return None


def is_registered():
    """判斷 .torrent 是否已確實預設由本程式開啟。

    優先檢查 Win10/11 的 UserChoice（真正生效的鍵）；若尚未建立
    （從未指定過預設程式），退回檢查標準 Classes 關聯。
    """
    if sys.platform != "win32":
        return False
    prog_id = _user_choice_prog_id()
    if prog_id is not None:
        return str(prog_id).lower() == PROG_ID.lower()
    try:
        with winreg.OpenKey(
            _HKCU, _classes_key(EXTENSION), 0, winreg.KEY_READ
        ) as key:
            default, _ = winreg.QueryValueEx(key, "")
            return default == PROG_ID
    except OSError:
        return False


def register():
    """建立 .torrent 檔案關聯，並確保程式出現在「開啟方式」清單。

    注意：這只完成「清單註冊」，不寫 UserChoice（見模組 docstring）。
    若要成為雙擊預設，還需使用者經 trigger_system_dialog() 確認一次。
    回傳 True 表示成功，False 表示失敗。
    """
    if sys.platform != "win32":
        return False
    command = executable_command()
    icon = icon_source()
    try:
        # .torrent -> PROG_ID
        with _create_key(EXTENSION) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, PROG_ID)

        # 讓本程式出現在「開啟檔案 > 選擇其他應用程式」清單（標準 Classes 層）
        with _create_key(EXTENSION, "OpenWithProgids") as key:
            winreg.SetValueEx(key, PROG_ID, 0, winreg.REG_SZ, "")

        # ProgID 顯示名稱
        with _create_key(PROG_ID) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, FRIENDLY_NAME)

        # 圖示
        with _create_key(PROG_ID, "DefaultIcon") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon)

        # 開啟命令
        with _create_key(PROG_ID, "shell", "open", "command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    except OSError:
        return False

    # Win10/11：額外寫進 FileExts 的 OpenWithProgids，確保程式一定出現在
    # 「開啟方式」清單（UserChoice 已指向其他程式時，標準 Classes 層不會被列出）。
    _register_fileexts_progid()

    _notify_shell()
    return True


def _register_fileexts_progid():
    """把 ProgID 寫進 FileExts 的 OpenWithProgids，供「開啟方式」清單引用。"""
    try:
        with winreg.CreateKeyEx(
            _HKCU, _fileexts_key(EXTENSION, "OpenWithProgids"), 0,
            winreg.KEY_WRITE | winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, PROG_ID, 0, winreg.REG_SZ, "")
    except OSError:
        pass


def trigger_system_dialog():
    """自動彈出 Windows 的「你要如何開啟此檔案?」對話框。

    以 ShellExecute 的 openas 動詞對一個暫存 .torrent 檔觸發，讓使用者
    在此對話框點選本程式並勾選「永遠使用」，由系統寫入合法 UserChoice。
    回傳 True 表示已觸發，False 表示觸發失敗（含回傳錯誤碼 ≤ 32）。
    """
    if sys.platform != "win32":
        return False

    fd, temp_path = tempfile.mkstemp(suffix=EXTENSION)
    os.close(fd)

    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(None, "openas", temp_path, None, None, 1)
        # ShellExecute 回傳值 > 32 表示成功，≤ 32 為錯誤碼
        return bool(result) and int(result) > 32
    except Exception:
        return False
    finally:
        threading.Thread(
            target=_remove_later, args=(temp_path, 5.0), daemon=True
        ).start()


def _remove_later(path, delay):
    """延遲刪除暫存檔，確保系統已讀取完副檔名。"""
    time.sleep(delay)
    try:
        os.remove(path)
    except OSError:
        pass


def unregister():
    """移除本程式建立的 .torrent 檔案關聯；盡量保留其他程式的設定。

    不刪除 UserChoice（由系統管理），只移除我們寫入的 Classes 與
    FileExts/OpenWithProgids 內容。"""
    if sys.platform != "win32":
        return
    try:
        # 若 .torrent 預設值正是我們的 ProgID，清除之
        try:
            with winreg.OpenKey(
                _HKCU, _classes_key(EXTENSION), 0, winreg.KEY_SET_VALUE
            ) as key:
                default, _ = winreg.QueryValueEx(key, "")
                if default == PROG_ID:
                    winreg.DeleteValue(key, "")
        except FileNotFoundError:
            pass

        # 從兩個層級的 OpenWithProgids 移除我們的 ProgID
        for openwith in (
            _classes_key(EXTENSION, "OpenWithProgids"),
            _fileexts_key(EXTENSION, "OpenWithProgids"),
        ):
            try:
                with winreg.OpenKey(
                    _HKCU, openwith, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.DeleteValue(key, PROG_ID)
            except OSError:
                pass  # 值不存在或鍵不存在時忽略

        # 刪除整個 ProgID 子樹
        _delete_tree(PROG_ID)
    except OSError:
        return
    _notify_shell()


def _create_key(*parts):
    return winreg.CreateKeyEx(
        _HKCU, _classes_key(*parts), 0, winreg.KEY_WRITE | winreg.KEY_SET_VALUE
    )


def _delete_tree(*parts):
    """遞迴刪除某個登錄檔機碼（含子機碼）。"""
    subkey = _classes_key(*parts)
    try:
        winreg.DeleteKey(_HKCU, subkey)
    except FileNotFoundError:
        return
    except OSError as exc:
        # 可能還有子機碼，遞迴處理
        with winreg.OpenKey(_HKCU, subkey, 0, winreg.KEY_READ) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(*(parts + (child,)))
        winreg.DeleteKey(_HKCU, subkey)


def _notify_shell():
    """通知 Explorer 重新整理檔案關聯，讓圖示與預設程式立即生效。"""
    try:
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
        )
    except Exception:
        pass
