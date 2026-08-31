import os
import sys
import time
import threading
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ftp_downloader
from ftp_downloader import parse_ftp_url, SocksFTP
from downloader import DownloadTask
from ftplib import error_perm

try:
    import pyftpdlib.authorizers
    import pyftpdlib.handlers
    import pyftpdlib.servers
    HAS_PYFTPDLIB = True
except ImportError:
    HAS_PYFTPDLIB = False


class TestParseFtpUrl(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            parse_ftp_url('ftp://example.com/pub/file.zip'),
            ('example.com', 21, '', '', '/pub/file.zip'))

    def test_credentials_and_port(self):
        self.assertEqual(
            parse_ftp_url('ftp://user:pass@host:2121/dir/a.bin'),
            ('host', 2121, 'user', 'pass', '/dir/a.bin'))

    def test_percent_encoded(self):
        self.assertEqual(
            parse_ftp_url('ftp://us%40er:p%40ss@host/%E6%B8%AC%E8%A9%A6.bin'),
            ('host', 21, 'us@er', 'p@ss', '/測試.bin'))

    def test_default_port(self):
        self.assertEqual(parse_ftp_url('ftp://example.com/file')[1], 21)

    def test_rejects_http(self):
        with self.assertRaises(ValueError):
            parse_ftp_url('http://example.com/file')

    def test_missing_host(self):
        with self.assertRaises(ValueError):
            parse_ftp_url('ftp:///file')


class TestDownloadTaskFtpDetection(unittest.TestCase):
    def test_ftp_scheme_detected(self):
        t = DownloadTask('ftp://example.com/a.bin', tempfile.mkdtemp())
        self.assertTrue(t.is_ftp)
        self.assertEqual(t._ftp_host, 'example.com')
        self.assertEqual(t._ftp_port, 21)
        self.assertEqual(t._ftp_user, '')
        self.assertEqual(t._ftp_pass, '')
        self.assertEqual(t._ftp_path, '/a.bin')

    def test_http_not_ftp(self):
        t = DownloadTask('http://example.com/a.bin', tempfile.mkdtemp())
        self.assertFalse(t.is_ftp)


class TestSocksFTPProxyParams(unittest.TestCase):
    def test_make_socket_direct(self):
        ftp = SocksFTP(proxy=None)
        with mock.patch('ftp_downloader.socket.create_connection',
                        return_value=mock.Mock()) as m:
            ftp._make_socket('1.2.3.4', 21)
            m.assert_called_once_with(('1.2.3.4', 21), ftp.timeout)

    def test_make_socket_via_socks(self):
        proxy = {'host': '10.0.0.5', 'port': 1080,
                 'username': 'u', 'password': 'p'}
        ftp = SocksFTP(proxy=proxy)
        with mock.patch('ftp_downloader.socks.create_connection',
                        return_value=mock.Mock()) as m:
            ftp._make_socket('ftp.example.com', 2121)
            call = m.call_args
            self.assertEqual(call[0][0], ('ftp.example.com', 2121))
            kw = call[1]
            self.assertEqual(kw['proxy_type'], ftp_downloader.socks.SOCKS5)
            self.assertEqual(kw['proxy_addr'], '10.0.0.5')
            self.assertEqual(kw['proxy_port'], 1080)
            self.assertEqual(kw['proxy_username'], 'u')
            self.assertEqual(kw['proxy_password'], 'p')
            self.assertTrue(kw['proxy_rdns'])

    def test_makepasv_epsv_preferred(self):
        ftp = SocksFTP(proxy=None)
        ftp.host = 'ftp.example.com'
        ftp.sock = mock.Mock()
        ftp.sock.getpeername.return_value = ('203.0.113.5', 21)
        resp = '229 Entering Extended Passive Mode (|||19500|).'
        with mock.patch.object(ftp, 'sendcmd', return_value=resp):
            host, port = ftp.makepasv()
        # 走 EPSV，host 仍以使用者提供的 self.host 為準
        self.assertEqual(host, 'ftp.example.com')
        self.assertEqual(port, 19500)

    def test_makepasv_falls_back_to_pasv(self):
        ftp = SocksFTP(proxy=None)
        ftp.host = 'ftp.example.com'
        ftp.sock = mock.Mock()
        ftp.sock.getpeername.return_value = ('203.0.113.5', 21)

        def _sendcmd(cmd):
            if cmd.startswith('EPSV'):
                raise error_perm('500 Unknown command')
            return '227 Entering Passive Mode (10,0,0,1,195,168).'

        with mock.patch.object(ftp, 'sendcmd', side_effect=_sendcmd):
            host, port = ftp.makepasv()
        self.assertEqual(host, 'ftp.example.com')  # 非 PASV 回報的 10.0.0.1
        self.assertEqual(port, 195 * 256 + 168)


@unittest.skipUnless(HAS_PYFTPDLIB, '需要 pyftpdlib 才能執行整合測試')
class TestFtpIntegration(unittest.TestCase):
    """以本地 loopback FTP server 驗證分段下載＋REST 續傳＋完成收尾。"""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.src = os.path.join(cls.dir, 'test.bin')
        cls.payload = bytes(range(256)) * 4096  # 1 MiB 確定性內容
        with open(cls.src, 'wb') as f:
            f.write(cls.payload)

        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer

        authorizer = DummyAuthorizer()
        authorizer.add_user('test', 'pw', cls.dir, perm='elradfmwMT')
        handler = FTPHandler
        handler.authorizer = authorizer

        cls.server = FTPServer(('127.0.0.1', 0), handler)
        cls.port = cls.server.socket.getsockname()[1]
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.close_all()

    def test_segmented_download_end_to_end(self):
        tmpdir = tempfile.mkdtemp()
        url = f'ftp://test:pw@127.0.0.1:{self.port}/test.bin'
        task = DownloadTask(
            url, tmpdir, filename='out.bin',
            proxies=[], chunks_per_part=8, threads_per_proxy=1)
        self.assertTrue(task.prepare())
        self.assertFalse(task._single_mode)
        self.assertTrue(task.supports_range)
        self.assertEqual(task.total_size, len(self.payload))

        self.assertTrue(task.start())
        deadline = time.time() + 30
        while task.status == 'downloading' and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(task.status, 'completed', task.error_message)

        out = os.path.join(tmpdir, 'out.bin')
        self.assertTrue(os.path.isfile(out))
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), self.payload)


class TestFtpMultiWorkers(unittest.TestCase):
    def test_threads_per_proxy_workers(self):
        proxy = {'host': '1.2.3.4', 'port': 1080}
        t = DownloadTask('ftp://u:p@host/f.bin', tempfile.mkdtemp(),
                         proxies=[proxy], threads_per_proxy=3)
        t.total_size = 1000
        t.supports_range = True
        t.block_size = 100
        t.block_count = 10
        t.bitmap = bytearray((10 + 7) // 8)
        t._single_mode = False
        t._rebuild_pool()
        # 用假的下載函式避免連網
        t._ftp_download_block = lambda idx, line, stop: 'ok'
        t._start_ftp(t._stop)
        try:
            # 2 條線（直連＋代理）× 每線 3 threads = 6 個 worker
            self.assertEqual(len(t._workers), 6)
        finally:
            t._stop.set()
            for w in t._workers:
                w.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
