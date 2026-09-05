#!/usr/bin/env python3
"""
多線程下載器 - 支持斷點續傳的下載工具
"""

import os
import sys
import ctypes

from PySide6.QtNetwork import QLocalServer, QLocalSocket

from ui import QApplication, MainWindow
from http_server import HttpServer
from downloader import DownloadManager
from app_icon import load_app_icon

# 保留 mutex 控制代碼，避免程式執行期間釋放而失去鎖定
_single_instance_mutex = None

# 單一實例 IPC 服務名稱：第二個實例用它把檔案參數轉送給第一個實例
_IPC_SERVER_NAME = "MultiSocksDownloader_IPC"


def _ensure_single_instance():
    """確保同時只有一個程式實例在執行。

    回傳 True 表示本實例為主要實例；False 表示已有其他實例在執行，
    呼叫端應把參數轉送給主要實例後結束。
    """
    if sys.platform != "win32":
        return True

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.restype = ctypes.c_void_p
    create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]

    global _single_instance_mutex
    _single_instance_mutex = create_mutex(
        None, False, "MultiSocksDownloader_SingleInstance_Mutex"
    )
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _parse_input_sources(args):
    """從命令列參數取出可下載來源：.torrent 檔案路徑、magnet 連結或 HTTP/HTTPS/FTP 網址。"""
    sources = []
    for p in args:
        if not isinstance(p, str):
            continue
        s = p.strip()
        if not s:
            continue
        if s.lower().startswith(('magnet:', 'http://', 'https://', 'ftp://')):
            sources.append(s)
        elif s.lower().endswith('.torrent') and os.path.isfile(s):
            sources.append(s)
    return sources


def _forward_to_primary(args):
    """次要實例：把命令列中的下載來源（.torrent / magnet / URL）透過本地 socket 轉送給主要實例。"""
    sources = _parse_input_sources(args)
    if not sources:
        return
    socket = QLocalSocket()
    socket.connectToServer(_IPC_SERVER_NAME)
    if not socket.waitForConnected(2000):
        return  # 主要實例尚未就緒，放棄本次轉送
    payload = "\n".join(sources).encode("utf-8")
    socket.write(payload)
    socket.flush()
    socket.waitForBytesWritten(2000)
    socket.disconnectFromServer()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(load_app_icon())

    # 設定 Windows AppUserModelID，讓執行中的任務列按鈕使用自訂圖示並正確分組
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MultiSocksDownloader.1.1")
        except Exception:
            pass

    is_primary = _ensure_single_instance()
    if not is_primary:
        # 已有實例在執行：把開啟的 .torrent 檔轉送給它後結束
        _forward_to_primary(sys.argv[1:])
        sys.exit(0)

    # 盡早啟動 IPC 伺服器，避免第二個實例轉送時伺服器尚未就緒
    ipc_server = QLocalServer()
    ipc_server.listen(_IPC_SERVER_NAME)

    # 待處理路徑：啟動參數 + 啟動期間/事件迴圈內轉送進來的路徑
    pending_paths = list(sys.argv[1:])
    sock_buffer = {}

    def on_ready_read(sock):
        sock_buffer[sock] = sock_buffer.get(sock, b"") + bytes(sock.readAll())

    def on_disconnected(sock):
        data = sock_buffer.pop(sock, b"").decode("utf-8", errors="replace")
        pending_paths.extend(p for p in data.split("\n") if p)
        flush_pending()
        sock.deleteLater()

    def on_new_connection():
        while ipc_server.hasPendingConnections():
            sock = ipc_server.nextPendingConnection()
            if sock is not None:
                sock.readyRead.connect(lambda s=sock: on_ready_read(s))
                sock.disconnected.connect(lambda s=sock: on_disconnected(s))

    ipc_server.newConnection.connect(on_new_connection)

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
        # 攔截下載請求：轉交 UI 彈出「選擇儲存位置」對話框後再建立任務
        http_server.add_download_request_callback(window.download_requested.emit)
    else:
        window.update_server_status(None, False)

    def flush_pending():
        """把累積的待處理來源加入對應任務：.torrent/magnet 走種子流程，URL 走一般下載。"""
        if not pending_paths:
            return
        paths = pending_paths[:]
        del pending_paths[:]
        for s in _parse_input_sources(paths):
            if s.lower().startswith('magnet:') or s.lower().endswith('.torrent'):
                window._add_bt_interactive(s)
            else:
                window._add_urls([s], silent=True)

    # 處理啟動時直接雙擊 .torrent 檔傳入的路徑（以及啟動期間已轉送進來的路徑）
    flush_pending()

    window.show()

    # 應用結束時關閉 HTTP 伺服器，並釋放常駐 DHT session
    app.aboutToQuit.connect(http_server.stop)
    app.aboutToQuit.connect(download_manager.shutdown_dht)

    sys.exit(app.exec())
