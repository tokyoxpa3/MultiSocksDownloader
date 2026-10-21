# -*- coding: utf-8 -*-
"""續傳正確性：.downloading 遺失時不得把已完成區塊當成已下載。

情境：下載到一半暫停後，使用者（或防毒、清理工具）刪掉了 `.downloading`，
只剩 `.progress`。過去 load_progress() 無條件信任 .progress 裡的 bitmap，
已完成的區塊被視為「已下載」，於是：
  - 續傳只補剩下的區塊；
  - 完成時把檔案 rename 成成品，大小正確但前半段全是零；
  - 狀態顯示「已完成」，使用者要到打開檔案才發現損壞。
正確做法：資料檔不存在或大小與 total_size 不符時，續傳狀態不可信，必須從頭下載。
"""

import http.server
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from downloader import DownloadTask  # noqa: E402

SIZE = 8 * 1024 * 1024
CHUNK = 64 * 1024
THROTTLE = 0.03  # 每寫 64 KiB 停 30ms，約 2 MiB/s


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    """支援 Range 的最小 HTTP 伺服器（SimpleHTTPRequestHandler 不支援 206）。"""

    payload = b''

    def log_message(self, *args):
        pass

    def do_GET(self):
        total = len(self.payload)
        rng = self.headers.get('Range')
        start, end = 0, total - 1
        partial = False
        if rng and rng.startswith('bytes='):
            spec = rng[6:].split(',')[0].strip()
            a, _, b = spec.partition('-')
            try:
                start = int(a) if a else 0
                end = int(b) if b else total - 1
            except ValueError:
                start, end = 0, total - 1
            end = min(end, total - 1)
            if start > end or start >= total:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % total)
                self.end_headers()
                return
            partial = True

        body = self.payload[start:end + 1]
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Accept-Ranges', 'bytes')
        if partial:
            self.send_header('Content-Range',
                             'bytes %d-%d/%d' % (start, end, total))
        self.end_headers()
        try:
            for i in range(0, len(body), CHUNK):
                self.wfile.write(body[i:i + CHUNK])
                time.sleep(THROTTLE)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class ResumeMissingTempTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = os.urandom(SIZE)
        handler = type('H', (_RangeHandler,), {'payload': cls.payload})
        cls.httpd = _Server(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='msd_resume_')
        self.url = 'http://127.0.0.1:%d/file.bin' % self.port

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait(self, task, predicate, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_missing_temp_file_restarts_from_scratch(self):
        task = DownloadTask(self.url, self.tmp, proxies=[], threads_per_proxy=1,
                            chunks_per_part=0)
        self.assertTrue(task.start(), task.error_message)
        self.assertGreater(task.block_count, 1)

        self.assertTrue(
            self._wait(task, lambda: task.get_progress()['percentage'] >= 20, 20),
            '下載未能到達 20%')
        self.assertTrue(task.pause())
        time.sleep(0.5)

        self.assertTrue(os.path.exists(task.temp_filepath), '暫存檔應存在')
        self.assertTrue(os.path.exists(task.progress_filepath), '進度檔應存在')
        # 模擬外部清理：只刪資料檔，進度檔還在
        os.remove(task.temp_filepath)

        self.assertTrue(task.resume(), task.error_message)
        self.assertTrue(
            self._wait(task, lambda: task.status in ('completed', 'error'), 60),
            '續傳未在時限內結束，狀態=%s' % task.status)

        self.assertEqual(task.status, 'completed', task.error_message)
        out = os.path.join(self.tmp, task.filename)
        self.assertTrue(os.path.exists(out), '應產出成品檔')
        with open(out, 'rb') as f:
            got = f.read()
        self.assertEqual(len(got), SIZE, '成品大小應與來源相同')
        self.assertEqual(got, self.payload,
                         '資料檔遺失後仍沿用舊 bitmap，成品內容損壞（前段全零）')

    def test_truncated_temp_file_restarts_from_scratch(self):
        """資料檔被截短（例如磁碟清理工具）也要從頭下載。"""
        task = DownloadTask(self.url, self.tmp, proxies=[], threads_per_proxy=1,
                            chunks_per_part=0)
        self.assertTrue(task.start(), task.error_message)
        self.assertTrue(
            self._wait(task, lambda: task.get_progress()['percentage'] >= 20, 20),
            '下載未能到達 20%')
        self.assertTrue(task.pause())
        time.sleep(0.5)

        with open(task.temp_filepath, 'r+b') as f:
            f.truncate(SIZE // 4)

        self.assertTrue(task.resume(), task.error_message)
        self.assertTrue(
            self._wait(task, lambda: task.status in ('completed', 'error'), 60),
            '續傳未在時限內結束，狀態=%s' % task.status)

        self.assertEqual(task.status, 'completed', task.error_message)
        with open(os.path.join(self.tmp, task.filename), 'rb') as f:
            got = f.read()
        self.assertEqual(len(got), SIZE)
        self.assertEqual(got, self.payload, '資料檔被截短後成品內容損壞')


if __name__ == '__main__':
    unittest.main(verbosity=2)
