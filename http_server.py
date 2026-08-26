#!/usr/bin/env python3
"""
HTTP 伺服器 - 接收來自 Chrome 擴展程式的下載請求
"""

import os
import json
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import socket
import logging

# 配置日誌記錄
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('http_server')


class DownloadRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, download_manager, callbacks, *args, **kwargs):
        self.download_manager = download_manager
        self.callbacks = callbacks
        super().__init__(*args, **kwargs)

    def _set_response(self, status_code=200, content_type='application/json'):
        self.send_response(status_code)
        self.send_header('Content-type', content_type)
        self.send_header('Access-Control-Allow-Origin', '*')  # 允許來自任何域的請求
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_OPTIONS(self):
        """處理 CORS 預檢請求"""
        self._set_response()

    def do_GET(self):
        """處理 GET 請求"""
        parsed_path = urlparse(self.path)
        path = parsed_path.path

        # 處理 /ping 請求 (連接檢查) — 無需認證，僅回報伺服器是否在運作
        if path == '/ping':
            self._set_response()
            response = {'status': 'ok', 'message': 'Server is running'}
            self.wfile.write(json.dumps(response).encode())
            return

        # 處理 /tasks 請求 (查詢所有任務下載進度) — 需要認證
        if path == '/tasks':
            self._set_response()
            tasks = []
            for tid, task in self.download_manager.task_ids.items():
                p = task.get_progress()
                tasks.append({
                    'id': tid,
                    'filename': task.filename,
                    'status': p['status'],
                    'percentage': p['percentage'],
                    'downloaded_size': p['downloaded_size'],
                    'total_size': p['total_size'],
                    'speed': p['speed'],
                })
            response = {'status': 'ok', 'tasks': tasks}
            self.wfile.write(json.dumps(response).encode())
            return

        self._set_response(404)
        response = {'status': 'error', 'message': 'Not found'}
        self.wfile.write(json.dumps(response).encode())

    def do_POST(self):
        """處理 POST 請求"""
        content_length = int(self.headers.get('Content-Length', 0))

        if content_length > 0:
            # 讀取請求體
            post_data = self.rfile.read(content_length).decode('utf-8')
            logger.info(f"收到POST數據: {post_data}")

            try:
                # 解析 JSON 數據
                data = json.loads(post_data)
                url = data.get('url', '')

                if not url:
                    self._set_response(400)
                    self.wfile.write(json.dumps({'status': 'error', 'message': 'Missing URL'}).encode())
                    return

                # 獲取可選參數
                filename = data.get('filename', None)
                headers = data.get('headers', None)
                if headers is not None and not isinstance(headers, dict):
                    headers = None

                # 獲取分片數，0 = 自適應（依檔案大小決定）
                chunks_per_part = data.get('chunks_per_part', 0)
                try:
                    chunks_per_part = int(chunks_per_part)
                    if chunks_per_part < 0:
                        chunks_per_part = 0
                except Exception:
                    chunks_per_part = 0

                # 獲取每個代理的線程數，默認為6
                threads_per_proxy = data.get('threads_per_proxy', 6)
                try:
                    threads_per_proxy = int(threads_per_proxy)
                    if threads_per_proxy < 1:
                        threads_per_proxy = 6
                except Exception:
                    threads_per_proxy = 6

                logger.info(f"從HTTP請求獲取下載參數: URL={url}, 檔案名={filename}, 分片數={chunks_per_part}, 每代理線程數={threads_per_proxy}")

                # 添加下載任務 - 使用當前下載管理器的保存目錄
                task_id = self.download_manager.add_task(
                    url,
                    filename,
                    save_dir=self.download_manager.save_dir,
                    use_proxy=True,
                    chunks_per_part=chunks_per_part,
                    threads_per_proxy=threads_per_proxy,
                    headers=headers,
                )
                logger.info(f"HTTP 請求添加了任務 ID: {task_id}, URL: {url}")

                if task_id is None:
                    self._set_response(500)
                    response = {'status': 'error', 'message': 'Failed to add download task'}
                else:
                    # 在背景執行緒啟動任務。啟動過程會對目標網址發送探測請求
                    # (直連 + 各代理輪流嘗試)，可能耗時數十秒，不能阻塞 HTTP 回應。
                    threading.Thread(
                        target=self.download_manager.start_task,
                        args=(task_id,),
                        daemon=True
                    ).start()

                    # 通知 UI 有新任務加入
                    if task_id in self.download_manager.task_ids:
                        task = self.download_manager.task_ids[task_id]
                        logger.info(f"任務已成功添加到下載管理器，檔案名: {task.filename}")

                        # 調用任務添加回調函數
                        for callback in self.callbacks:
                            try:
                                callback(task_id, task)
                            except Exception as e:
                                logger.error(f"調用任務添加回調函數時出錯: {str(e)}")

                    self._set_response()
                    response = {
                        'status': 'success',
                        'message': 'Download task added',
                        'task_id': task_id
                    }
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析錯誤: {e}")
                self._set_response(400)
                response = {'status': 'error', 'message': f'Invalid JSON: {str(e)}'}
            except Exception as e:
                logger.error(f"處理請求時出錯: {e}")
                self._set_response(500)
                response = {'status': 'error', 'message': f'Server error: {str(e)}'}
        else:
            logger.warning("收到空的POST請求")
            self._set_response(400)
            response = {'status': 'error', 'message': 'Empty request'}

        self.wfile.write(json.dumps(response).encode())


def create_handler_class(download_manager, callbacks):
    """創建一個包含下載管理器引用與回呼清單的處理程序類"""
    def handler(self, *args, **kwargs):
        DownloadRequestHandler.__init__(self, download_manager, callbacks, *args, **kwargs)
    return type('CustomHandler', (DownloadRequestHandler,), {'__init__': handler})


class HttpServer:
    def __init__(self, download_manager, host='127.0.0.1', port=8765):
        self.download_manager = download_manager
        self.host = host  # 預設僅監聽本機，避免區域網路內任意主機觸發下載
        self.port = port
        self.server = None
        self.thread = None
        self.is_running = False
        self.task_added_callbacks = []

    def add_task_added_callback(self, callback):
        """添加任務添加回調函數"""
        if callback not in self.task_added_callbacks:
            self.task_added_callbacks.append(callback)
            logger.info(f"已添加任務添加回調函數")

    def remove_task_added_callback(self, callback):
        """移除任務添加回調函數"""
        if callback in self.task_added_callbacks:
            self.task_added_callbacks.remove(callback)
            logger.info(f"已移除任務添加回調函數")

    def start(self):
        """啟動 HTTP 伺服器"""
        if self.is_running:
            logger.warning("HTTP 伺服器已在運行中")
            return False

        try:
            # 創建伺服器（ThreadingHTTPServer 讓每個請求在獨立執行緒處理，
            # 避免單一慢速請求（如下載探測）阻塞後續的 ping / POST）
            handler_class = create_handler_class(self.download_manager, self.task_added_callbacks)
            self.server = ThreadingHTTPServer((self.host, self.port), handler_class)
            self.server.daemon_threads = True

            # 啟動伺服器線程
            self.thread = threading.Thread(target=self.server.serve_forever)
            self.thread.daemon = True  # 設置為守護線程，主程序結束時自動退出
            self.thread.start()

            self.is_running = True
            logger.info(f"HTTP 伺服器啟動成功，監聽於 {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"HTTP 伺服器啟動失敗: {str(e)}")
            return False

    def get_local_ip(self):
        """獲取本機 IP 地址"""
        try:
            # 創建臨時 socket 連接來獲取本機 IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # 連接到公共 DNS 伺服器（不需要真正發送數據）
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception as e:
            logger.error(f"獲取本機 IP 出錯: {str(e)}")
            return "localhost"  # 如果無法獲取，返回 localhost

    def stop(self):
        """停止 HTTP 伺服器"""
        if not self.is_running:
            logger.warning("HTTP 伺服器未運行")
            return

        try:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.is_running = False
            logger.info(f"HTTP 伺服器已停止")
        except Exception as e:
            logger.error(f"HTTP 伺服器停止失敗: {str(e)}")

    def get_server_url(self):
        """獲取伺服器 URL"""
        if not self.is_running:
            return None

        # 只有綁定所有網卡時才回傳區域網路 IP，否則僅本機可連
        urls = {
            "localhost": f"http://localhost:{self.port}",
        }
        if self.host in ('0.0.0.0', '::'):
            local_ip = self.get_local_ip()
            urls["local_ip"] = f"http://{local_ip}:{self.port}"
        return urls


# 測試代碼
if __name__ == "__main__":
    # 模擬下載管理器
    class MockDownloadManager:
        def add_task(self, url, filename=None, save_dir=None, use_proxy=True, chunks_per_part=None, threads_per_proxy=None, headers=None):
            print(f"添加下載任務: {url}, 文件名: {filename}")
            return "task-1234"

        def start_task(self, task_id):
            print(f"啟動任務: {task_id}")
            return True

    # 創建並啟動伺服器
    mock_dm = MockDownloadManager()
    server = HttpServer(mock_dm)
    if server.start():
        print(f"伺服器已啟動，URL: {server.get_server_url()}")
        print("按 Ctrl+C 停止伺服器...")
        try:
            # 保持主線程運行
            while True:
                pass
        except KeyboardInterrupt:
            server.stop()
            print("伺服器已停止") 