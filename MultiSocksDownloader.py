#!/usr/bin/env python3
"""
多線程下載器 - 支持斷點續傳的下載工具
"""

import sys
import ctypes
from ui import QApplication, MainWindow
from http_server import HttpServer
from downloader import DownloadManager
from app_icon import load_app_icon

# 保留 mutex 控制代碼，避免程式執行期間釋放而失去鎖定
_single_instance_mutex = None


def _ensure_single_instance():
    """確保同時只有一個程式實例在執行；已存在時直接結束。"""
    if sys.platform != "win32":
        return

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.restype = ctypes.c_void_p
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]

    global _single_instance_mutex
    _single_instance_mutex = create_mutex(
        None, False, "MultiSocksDownloader_SingleInstance_Mutex"
    )
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        sys.exit(0)


if __name__ == "__main__":
    _ensure_single_instance()

    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())

    # 設定 Windows AppUserModelID，讓執行中的任務列按鈕使用自訂圖示並正確分組
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MultiSocksDownloader.1.1")
        except Exception:
            pass

    # 創建下載管理器實例（以後將通過共享單例模式優化）
    download_manager = DownloadManager()

    # 啟動 HTTP 伺服器
    http_server = HttpServer(download_manager)
    server_started = http_server.start()

    # 創建主窗口，傳入已有的下載管理器
    window = MainWindow(download_manager)

    # 更新 UI 上的伺服器狀態
    if server_started:
        server_urls = http_server.get_server_url()
        # 顯示本地 IP 的 URL，這樣其他設備可以訪問
        if "local_ip" in server_urls:
            window.update_server_status(server_urls["local_ip"], True)
        else:
            window.update_server_status(server_urls["localhost"], True)

        # 註冊回調函數，讓 HTTP 伺服器可以通知 UI 有新任務添加
        http_server.add_task_added_callback(window.on_task_added)
    else:
        window.update_server_status(None, False)

    window.show()

    # 應用結束時關閉 HTTP 伺服器
    app.aboutToQuit.connect(http_server.stop)

    sys.exit(app.exec()) 