import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from downloader import (DownloadTask, DownloadManager, format_size,
                        _better_filename, _filename_from_url,
                        _looks_like_endpoint,
                        LINE_FAIL_THRESHOLD, LINE_QUARANTINE_SECONDS)
from bt_downloader import BTTask


class TestFormatSize(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_size(0), "0 B")

    def test_bytes(self):
        self.assertEqual(format_size(512), "512.00 B")

    def test_kilobytes(self):
        self.assertEqual(format_size(1024), "1.00 KB")

    def test_megabytes(self):
        self.assertEqual(format_size(1024 * 1024), "1.00 MB")


class TestFilenameExtraction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_from_path(self):
        t = DownloadTask("http://example.com/dir/file.zip", self.tmpdir)
        self.assertEqual(t._extract_filename_from_url(), "file.zip")

    def test_from_query(self):
        t = DownloadTask("http://example.com/?filename=report.pdf", self.tmpdir)
        self.assertEqual(t._extract_filename_from_url(), "report.pdf")

    def test_empty(self):
        t = DownloadTask("http://example.com/", self.tmpdir)
        self.assertEqual(t._extract_filename_from_url(), "")


class TestFilenameFromHeaders(unittest.TestCase):
    def test_quoted(self):
        headers = {"content-disposition": 'attachment; filename="report.pdf"'}
        self.assertEqual(DownloadTask._filename_from_headers(headers), "report.pdf")

    def test_unquoted(self):
        headers = {"content-disposition": "attachment; filename=data.txt"}
        self.assertEqual(DownloadTask._filename_from_headers(headers), "data.txt")

    def test_rfc5987(self):
        headers = {
            "content-disposition": "attachment; filename*=UTF-8''%E6%B8%AC%E8%A9%A6.txt"
        }
        self.assertEqual(DownloadTask._filename_from_headers(headers), "測試.txt")

    def test_missing(self):
        self.assertIsNone(DownloadTask._filename_from_headers({}))


class TestBlockBitmap(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.task = DownloadTask(
            "http://example.com/bigfile.bin", self.tmpdir, filename="bigfile.bin"
        )
        self.task.total_size = 1000
        self.task.block_size = 100
        self.task.block_count = 10
        self.task.bitmap = bytearray((10 + 7) // 8)
        self.task._single_mode = False

    def test_block_bounds(self):
        self.assertEqual(self.task._block_bounds(0), (0, 100))
        self.assertEqual(self.task._block_bounds(9), (900, 1000))

    def test_set_and_check_done(self):
        self.assertFalse(self.task._is_block_done(0))
        self.task._set_block_done(0)
        self.task._set_block_done(9)
        self.assertTrue(self.task._is_block_done(0))
        self.assertTrue(self.task._is_block_done(9))
        self.assertFalse(self.task._is_block_done(1))

    def test_popcount(self):
        self.task._set_block_done(0)
        self.task._set_block_done(9)
        self.assertEqual(self.task._popcount(), 2)

    def test_completed_bytes(self):
        # 區塊 0 佔 0-100，區塊 9 佔 900-1000，合計 200 bytes
        self.task._set_block_done(0)
        self.task._set_block_done(9)
        self.assertEqual(self.task._completed_bytes_locked(), 200)


class TestProgressPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.url = "http://example.com/bigfile.bin"

    def _make_task(self):
        t = DownloadTask(self.url, self.tmpdir, filename="bigfile.bin")
        t.total_size = 1000
        t.block_size = 100
        t.block_count = 10
        t.bitmap = bytearray((10 + 7) // 8)
        t._single_mode = False
        t.supports_range = True
        t.proxies = [{"host": "127.0.0.1", "port": 1080}]
        # 真實流程裡存進度時資料檔一定存在（save_progress 會先 flush 它）。
        # 少了它，load_progress() 會判定續傳狀態失去依據而拒絕載入
        # （見 _resume_data_usable：避免 .downloading 被清掉後拼出全零檔案）。
        t._ensure_temp_file()
        return t

    def test_roundtrip(self):
        t = self._make_task()
        t._set_block_done(0)
        t._set_block_done(3)
        t.save_progress()

        t2 = DownloadTask(self.url, self.tmpdir, filename="bigfile.bin")
        self.assertTrue(t2.load_progress())
        self.assertEqual(t2.block_count, 10)
        self.assertEqual(t2.block_size, 100)
        self.assertEqual(t2.total_size, 1000)
        self.assertTrue(t2.supports_range)
        self.assertEqual(t2.proxies, [{"host": "127.0.0.1", "port": 1080}])
        self.assertTrue(t2._is_block_done(0))
        self.assertTrue(t2._is_block_done(3))
        self.assertFalse(t2._is_block_done(1))
        self.assertEqual(t2.status, "paused")

    def test_load_missing_file(self):
        t = DownloadTask(self.url, self.tmpdir, filename="nonexistent.bin")
        self.assertFalse(t.load_progress())


class TestUniqueFilepath(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_suffix_increments(self):
        open(os.path.join(self.tmpdir, "file.bin"), "w").close()
        t = DownloadTask("http://example.com/f", self.tmpdir, filename="file.bin")
        t._ensure_unique_filepath()
        self.assertEqual(t.filename, "file_1.bin")


class TestSetBlockDoneIdempotent(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.task = DownloadTask(
            "http://example.com/bigfile.bin", self.tmpdir, filename="bigfile.bin"
        )
        self.task.total_size = 1000
        self.task.block_size = 100
        self.task.block_count = 10
        self.task.bitmap = bytearray((10 + 7) // 8)
        self.task._single_mode = False

    def test_no_double_count(self):
        self.task._set_block_done(0)
        self.task._set_block_done(0)  # 重複標記不應重複累計
        self.assertEqual(self.task._completed_bytes_locked(), 100)


class TestTaskDedup(unittest.TestCase):
    def setUp(self):
        self.manager = DownloadManager()
        # 隔離設定檔，避免污染使用者真實設定
        self.manager.config_file = os.path.join(tempfile.mkdtemp(), "config.json")
        self.url = "http://example.com/dedup_test.bin"
        self.save_dir = tempfile.mkdtemp()

    def test_active_task_dedup(self):
        tid1 = self.manager.add_task(self.url, save_dir=self.save_dir, use_proxy=False)
        tid2 = self.manager.add_task(self.url, save_dir=self.save_dir, use_proxy=False)
        self.assertEqual(tid1, tid2)

    def test_completed_task_readd(self):
        tid1 = self.manager.add_task(self.url, save_dir=self.save_dir, use_proxy=False)
        self.manager.task_ids[tid1].status = "completed"
        tid2 = self.manager.add_task(self.url, save_dir=self.save_dir, use_proxy=False)
        self.assertNotEqual(tid1, tid2)
        self.assertNotIn(tid1, self.manager.task_ids)


class TestRetry(unittest.TestCase):
    def test_retry_rejects_active(self):
        t = DownloadTask("http://example.com/f", tempfile.mkdtemp(), filename="f.bin")
        t.status = "downloading"
        self.assertFalse(t.retry())

    def test_retry_resets_and_restarts(self):
        t = DownloadTask("http://example.com/f", tempfile.mkdtemp(), filename="f.bin")
        t.status = "error"
        t._fatal = True
        t._block_retries = {0: 3, 1: 3}
        t.error_message = "boom"
        called = []
        t.start = lambda: called.append(True) or True
        self.assertTrue(t.retry())
        self.assertFalse(t._fatal)
        self.assertEqual(t._block_retries, {})
        self.assertEqual(t.error_message, "")
        self.assertTrue(called)


class TestAdaptiveBlockCount(unittest.TestCase):
    def _make(self, total):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(), filename="f.bin")
        t.total_size = total
        t.chunks_per_part = 0   # 0 = 自適應
        t._build_blocks()
        return t

    def test_small_file_single_block(self):
        t = self._make(1024)
        self.assertEqual(t.block_count, 1)
        self.assertEqual(t.block_size, 1024)

    def test_4mib_single_block(self):
        t = self._make(4 * 1024 * 1024)
        self.assertEqual(t.block_count, 1)
        self.assertEqual(t.block_size, 4 * 1024 * 1024)

    def test_large_file_target_size(self):
        # 5 GiB → 每片約 4 MiB
        t = self._make(5 * 1024 * 1024 * 1024)
        self.assertEqual(t.block_count, 1280)
        self.assertEqual(t.block_size, 4 * 1024 * 1024)

    def test_max_blocks_cap(self):
        # 100 GiB → 切片數被上限 4096 限制
        t = self._make(100 * 1024 * 1024 * 1024)
        self.assertEqual(t.block_count, 4096)


class TestPartialPersistence(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.url = "http://example.com/big.bin"

    def _make(self):
        t = DownloadTask(self.url, self.tmpdir, filename="big.bin")
        t.total_size = 1000
        t.block_size = 100
        t.block_count = 10
        t.bitmap = bytearray((10 + 7) // 8)
        t._single_mode = False
        t.supports_range = True
        t.proxies = []
        # 同上：續傳狀態必須有實際的資料檔才有意義
        t._ensure_temp_file()
        return t

    def test_partial_roundtrip(self):
        t = self._make()
        t._set_block_done(0)
        t._partial[3] = 60   # 區塊 3 已寫入 60 bytes
        t.save_progress()

        t2 = DownloadTask(self.url, self.tmpdir, filename="big.bin")
        self.assertTrue(t2.load_progress())
        self.assertEqual(t2._partial, {3: 60})

    def test_partial_ignored_for_done_block(self):
        t = self._make()
        t._set_block_done(0)
        t._partial[0] = 100   # 已完成區塊不應再記錄 partial
        t.save_progress()

        t2 = DownloadTask(self.url, self.tmpdir, filename="big.bin")
        self.assertTrue(t2.load_progress())
        self.assertEqual(t2._partial, {})


class TestBlocksStateFormat(unittest.TestCase):
    def test_dict_format_with_frac(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(), filename="f.bin")
        t.total_size = 1000
        t.block_size = 100
        t.block_count = 10
        t.bitmap = bytearray((10 + 7) // 8)
        t._single_mode = False
        t._set_block_done(0)
        t._partial[2] = 50
        t._active_blocks.add(2)
        states = t._get_blocks_state()
        self.assertEqual(len(states), 10)
        self.assertEqual(states[0], {'frac': 1.0, 'active': False})
        self.assertEqual(states[2], {'frac': 0.5, 'active': True})
        self.assertEqual(states[5], {'frac': 0.0, 'active': False})


class TestLineTracking(unittest.TestCase):
    def test_line_key(self):
        t = DownloadTask("http://example.com/f", tempfile.mkdtemp())
        self.assertEqual(t._line_key(None), "direct")
        self.assertEqual(
            t._line_key({"host": "1.2.3.4", "port": 1080}),
            "proxy:1.2.3.4:1080")

    def test_progress_exposes_line_data(self):
        t = DownloadTask("http://example.com/f", tempfile.mkdtemp(),
                         proxies=[{"host": "1.2.3.4", "port": 1080}])
        t._line_bytes["direct"] = 100
        t._line_bytes["proxy:1.2.3.4:1080"] = 50
        p = t.get_progress()
        self.assertEqual(p["line_bytes"]["direct"], 100)
        self.assertEqual(p["line_bytes"]["proxy:1.2.3.4:1080"], 50)
        self.assertEqual(p["line_labels"]["direct"], "直連")
        self.assertEqual(p["line_labels"]["proxy:1.2.3.4:1080"], "1.2.3.4:1080")


class _FakeResp:
    """可控制的假 HTTP 串流回應，供 _write_run 單元測試使用。"""

    def __init__(self, data, chunk_size, stop=None):
        self._data = data
        self._cs = chunk_size
        self._stop = stop
        self.closed = False

    def iter_content(self, chunk_size):
        d = self._data
        i = 0
        while i < len(d):
            yield d[i:i + chunk_size]
            i += chunk_size
            if self._stop is not None:
                # 第一次 yield 後立刻觸發 stop，讓迴圈在下一輪頂部 break，
                # 模擬「下載到一半被暫停」的場景。
                self._stop.set()
                break

    def close(self):
        self.closed = True


class _StopFlag:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


class TestPopRun(unittest.TestCase):
    """前沿串流：_pop_run 應認領一段連續區塊並標記 claimed。"""

    def _make(self, block_size=100, block_count=10):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(), filename="f.bin")
        t.total_size = block_size * block_count
        t.block_size = block_size
        t.block_count = block_count
        t.bitmap = bytearray((block_count + 7) // 8)
        t._claimed = bytearray((block_count + 7) // 8)
        t._single_mode = False
        return t

    def test_claims_contiguous_span(self):
        # 顆粒改成「剩餘未認領 / (worker × RUN_SPLIT_FACTOR)」：400 塊、1 個
        # worker → 一次認領 100 塊。這裡驗的仍是「連續」而非逐塊散著拿。
        t = self._make(block_size=100, block_count=400)
        t._target_workers = 1
        run = t._pop_run()
        self.assertEqual(run, (0, 99))
        for i in range(100):
            self.assertTrue(t._is_block_claimed(i))
        self.assertFalse(t._is_block_claimed(100))

    def test_stops_at_done_block(self):
        t = self._make(block_size=100, block_count=400)
        t._target_workers = 1
        t._set_block_done(3)
        run = t._pop_run()
        self.assertEqual(run, (0, 2))
        self.assertTrue(t._is_block_claimed(0))
        self.assertFalse(t._is_block_claimed(3))

    def test_caps_run_size(self):
        # block_size 32 MiB → TARGET_RUN_BYTES//block_size == 2，一次最多認領 2 塊
        t = self._make(block_size=32 * 1024 * 1024, block_count=10)
        run = t._pop_run()
        self.assertEqual(run, (0, 1))

    def test_none_when_all_done(self):
        t = self._make(block_count=3)
        for i in range(3):
            t._set_block_done(i)
        self.assertIsNone(t._pop_run())


class TestWriteRun(unittest.TestCase):
    """_write_run 應跨區塊標記 done，並在暫停時保留 partial 供續傳。"""

    def _make(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(), filename="f.bin")
        t.total_size = 500
        t.block_size = 100
        t.block_count = 5
        t.bitmap = bytearray((5 + 7) // 8)
        t._claimed = bytearray((5 + 7) // 8)
        t._single_mode = False
        t.chunk_size = 150  # 每批 150 bytes，跨過 block 邊界
        with open(t.temp_filepath, 'wb') as f:
            f.truncate(t.total_size)
        return t

    def test_complete_run_marks_all_done(self):
        t = self._make()
        data = bytes(range(256)) * 2  # 512 bytes，但 run 只吃前 500
        r = _FakeResp(data, chunk_size=150)
        stop = _StopFlag()
        result = t._write_run(0, 4, 0, 499, r, stop, None)
        self.assertEqual(result, 'ok')
        for i in range(5):
            self.assertTrue(t._is_block_done(i))
        self.assertEqual(t._popcount(), 5)
        self.assertEqual(t._completed_bytes_locked(), 500)
        # 完成後不該殘留 partial
        self.assertEqual(t._partial, {})

    def test_stop_preserves_partial(self):
        t = self._make()
        data = bytes(range(256)) * 2  # 500 有效位元組
        r = _FakeResp(data, chunk_size=150)
        stop = _StopFlag()
        # 第一次 yield 150 bytes 後觸發 stop
        r = _FakeResp(data, chunk_size=150, stop=stop)
        result = t._write_run(0, 4, 0, 499, r, stop, None)
        self.assertEqual(result, 'fail')
        # block 0 (0-100) 已完成，block 1 (100-200) 已寫 50 bytes
        self.assertTrue(t._is_block_done(0))
        self.assertFalse(t._is_block_done(1))
        self.assertEqual(t._partial.get(1), 50)


class TestHandleRunFailure(unittest.TestCase):
    def _make(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(), filename="f.bin")
        t.total_size = 500
        t.block_size = 100
        t.block_count = 5
        t.bitmap = bytearray((5 + 7) // 8)
        t._claimed = bytearray((5 + 7) // 8)
        t._single_mode = False
        return t

    def test_releases_claims_and_counts_retry(self):
        t = self._make()
        for i in range(3):
            t._set_block_claimed(i)
        t._set_block_done(0)  # block 0 已完成，不應被釋放 claim 影響 done 狀態
        stop = _StopFlag()
        result = t._handle_run_failure(0, 2, 2, "boom", stop)
        self.assertEqual(result, 'fail')
        # 未完成的 block 1、2 應釋放 claim
        self.assertFalse(t._is_block_claimed(1))
        self.assertFalse(t._is_block_claimed(2))
        self.assertEqual(t._block_retries.get(2), 1)
        self.assertFalse(t._fatal)

    def test_reaches_max_retries_sets_error(self):
        t = self._make()
        stop = _StopFlag()
        for _ in range(t.MAX_RETRIES):
            t._handle_run_failure(0, 1, 1, "boom", stop)
            t._set_block_claimed(1)
        self.assertTrue(t._fatal)
        self.assertEqual(t.status, 'error')
        self.assertTrue(stop.is_set())


class TestFilenameFromRedirect(unittest.TestCase):
    """302 轉址、伺服器不帶 Content-Disposition 時，應改用最終網址推導檔名。"""

    def test_filename_from_url_strips_query(self):
        self.assertEqual(
            _filename_from_url("https://x.com/a/b.pdf?X-Amz-Signature=abc"),
            "b.pdf")

    def test_filename_from_url_invalid(self):
        self.assertEqual(_filename_from_url(""), "")
        self.assertEqual(_filename_from_url(None), "")

    def test_endpoint_detection(self):
        self.assertTrue(_looks_like_endpoint("getfile.jsp"))
        self.assertTrue(_looks_like_endpoint("download"))          # 無副檔名
        self.assertFalse(
            _looks_like_endpoint("postgresql-18.6-3-windows-x64.exe"))

    def test_better_filename_upgrades_endpoint(self):
        self.assertEqual(
            _better_filename("getfile.jsp", "pkg-1.2.exe"), "pkg-1.2.exe")

    def test_better_filename_keeps_custom_name(self):
        self.assertEqual(
            _better_filename("My Video.mp4", "pkg-1.2.exe"), "My Video.mp4")

    def test_better_filename_rejects_endpoint_candidate(self):
        # 轉址目標本身也是端點（無副檔名）時，不應覆蓋原值
        self.assertEqual(
            _better_filename("getfile.jsp", "videoplayback"), "getfile.jsp")

    def test_prepare_uses_redirect_target(self):
        tmpdir = tempfile.mkdtemp()
        t = DownloadTask(
            "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260486", tmpdir)
        self.assertEqual(t.filename, "getfile.jsp")
        t._probe = lambda lines: {
            "headers": {},
            "resolved_url": ("https://get.enterprisedb.com/postgresql/"
                             "postgresql-18.6-3-windows-x64.exe"),
            "supports_range": False,
            "total_size": 0,
        }
        self.assertTrue(t.prepare())
        self.assertEqual(t.filename, "postgresql-18.6-3-windows-x64.exe")

    def test_prepare_prefers_content_disposition(self):
        tmpdir = tempfile.mkdtemp()
        t = DownloadTask("https://x.com/getfile.jsp?fileid=1", tmpdir)
        t._probe = lambda lines: {
            "headers": {
                "content-disposition": 'attachment; filename="real.zip"'},
            "resolved_url": "https://cdn.x.com/other.exe",
            "supports_range": False,
            "total_size": 0,
        }
        self.assertTrue(t.prepare())
        self.assertEqual(t.filename, "real.zip")


class _DeadThread:
    def is_alive(self):
        return False


class _AliveThread:
    def is_alive(self):
        return True


class TestLineHealth(unittest.TestCase):
    """線路連續失敗後應暫時隔離，避免壞線把無辜區塊的重試次數推向 fatal。"""

    def _proxy(self):
        return {"host": "1.2.3.4", "port": 1080}

    def test_quarantine_after_threshold(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp())
        p = self._proxy()
        for _ in range(LINE_FAIL_THRESHOLD):
            t._note_line_failure(p)
        self.assertFalse(t._line_available(p, time.monotonic()))
        # 隔離期滿後應恢復可用
        self.assertTrue(
            t._line_available(p, time.monotonic() + LINE_QUARANTINE_SECONDS + 1))

    def test_success_clears_quarantine(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp())
        p = self._proxy()
        for _ in range(LINE_FAIL_THRESHOLD):
            t._note_line_failure(p)
        t._note_line_success(p)
        self.assertTrue(t._line_available(p, time.monotonic()))

    def test_next_line_skips_quarantined(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(),
                         proxies=[self._proxy()])
        t._lines = t._build_lines()      # [None, proxy]
        p = self._proxy()
        for _ in range(LINE_FAIL_THRESHOLD):
            t._note_line_failure(p)
        # 代理被隔離後，輪派應只挑到直連（None）
        for _ in range(4):
            self.assertIsNone(t._next_line(time.monotonic()))


class TestWorkerRespawn(unittest.TestCase):
    """worker 全數退場後，看門狗必須補位，否則任務會永久卡在 downloading。"""

    def _make(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(),
                         filename="f.bin")
        t.total_size = 1000
        t.block_size = 100
        t.block_count = 10
        t.bitmap = bytearray((10 + 7) // 8)
        t._claimed = bytearray((10 + 7) // 8)
        t._single_mode = False
        t._lines = [None]
        t._target_workers = 2
        t.threads_per_proxy = 2
        t._spawn_index = 0
        return t

    def test_respawns_when_all_workers_dead(self):
        t = self._make()
        spawned = []
        t._spawn_http_workers = lambda stop, count: spawned.append(count)
        t._workers = [_DeadThread()]
        self.assertTrue(t._maintain_workers(_StopFlag()))
        self.assertEqual(spawned, [2])

    def test_respawns_to_target_while_work_remains(self):
        # 舊行為是「只要還有 worker 活著就不補」，於是慢線的 worker 退場後它
        # 釋放的區塊永遠沒人下載。現在只要有未認領區塊就補到目標數。
        t = self._make()
        spawned = []
        t._spawn_http_workers = lambda stop, count: spawned.append(count)
        t._workers = [_AliveThread()]
        self.assertTrue(t._maintain_workers(_StopFlag()))
        self.assertEqual(spawned, [1])

    def test_no_respawn_when_all_blocks_done(self):
        t = self._make()
        spawned = []
        t._spawn_http_workers = lambda stop, count: spawned.append(count)
        t._workers = [_AliveThread()]
        for i in range(t.block_count):
            t._set_block_done(i)
        self.assertFalse(t._maintain_workers(_StopFlag()))
        self.assertEqual(spawned, [])

    def test_respawn_clears_stale_claims(self):
        t = self._make()
        t._spawn_http_workers = lambda stop, count: None
        t._workers = []
        t._set_block_claimed(0)
        t._set_block_claimed(1)
        self.assertTrue(t._maintain_workers(_StopFlag()))
        self.assertFalse(t._is_block_claimed(0))
        self.assertFalse(t._is_block_claimed(1))


class TestRunStallDetection(unittest.TestCase):
    """線路慢速滴流時，_write_run 應主動中斷該 run 並計一次重試。"""

    def _make(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(),
                         filename="f.bin")
        t.total_size = 500
        t.block_size = 100
        t.block_count = 5
        t.bitmap = bytearray((5 + 7) // 8)
        t._claimed = bytearray((5 + 7) // 8)
        t._single_mode = False
        t.chunk_size = 150
        # 測試用：窗長歸零、門檻拉高，首批 150 bytes 即被判定停滯。
        t.STALL_WINDOW = 0.0
        t.STALL_MIN_BYTES = 1024
        with open(t.temp_filepath, 'wb') as f:
            f.truncate(t.total_size)
        return t

    def test_slow_trickle_aborts_run(self):
        t = self._make()
        for i in range(5):
            t._set_block_claimed(i)
        r = _FakeResp(bytes(range(256)) * 2, chunk_size=150)
        stop = _StopFlag()
        result = t._write_run(0, 4, 0, 499, r, stop, None)
        self.assertEqual(result, 'fail')
        self.assertTrue(r.closed)
        self.assertGreaterEqual(sum(t._block_retries.values()), 1)
        self.assertFalse(t._fatal)


class TestSingleModeRetry(unittest.TestCase):
    """單線模式失敗應自動重試，且失敗時不得把半成品標記為完成。"""

    def _make(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(),
                         filename="f.bin")
        t._single_mode = True
        t.RETRY_BACKOFF = 0.0
        return t

    def test_retries_then_succeeds(self):
        t = self._make()
        attempts = {'n': 0}

        def fake(proxy, stop):
            attempts['n'] += 1
            return attempts['n'] >= 2

        t._single_attempt = fake
        completed = []
        t.complete_download = lambda: completed.append(True)
        t._single_worker(None, t._stop)
        self.assertEqual(attempts['n'], 2)
        self.assertEqual(completed, [True])

    def test_marks_error_after_max_retries(self):
        t = self._make()
        t._single_attempt = lambda proxy, stop: False
        t.error_message = 'boom'
        t.complete_download = lambda: self.fail("失敗的任務不該被標記為完成")
        t._single_worker(None, t._stop)
        self.assertEqual(t.status, 'error')
        self.assertEqual(t.error_message, 'boom')


class TestRetryCounterReset(unittest.TestCase):
    def test_block_done_clears_retry_counter(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(),
                         filename="f.bin")
        t.total_size = 1000
        t.block_size = 100
        t.block_count = 10
        t.bitmap = bytearray((10 + 7) // 8)
        t._single_mode = False
        t._block_retries[3] = 2
        t._set_block_done(3)
        self.assertNotIn(3, t._block_retries)


class TestCompleteGuard(unittest.TestCase):
    """complete_download 只允許在 downloading 狀態收尾。"""

    def test_error_task_not_marked_completed(self):
        t = DownloadTask("http://example.com/f.bin", tempfile.mkdtemp(),
                         filename="f.bin")
        t._set_status('error')
        t.complete_download()
        self.assertEqual(t.status, 'error')

    def test_downloading_task_completes(self):
        tmpdir = tempfile.mkdtemp()
        t = DownloadTask("http://example.com/f.bin", tmpdir, filename="f.bin")
        with open(t.temp_filepath, 'wb') as f:
            f.write(b'0123456789')
        t._set_status('downloading')
        t.complete_download()
        self.assertEqual(t.status, 'completed')
        self.assertTrue(os.path.exists(t.filepath))
        self.assertFalse(os.path.exists(t.temp_filepath))


class TestSpeedLimitAllocation(unittest.TestCase):
    """全域限速要同時約束 HTTP 與 BT：BT 的資料由 libtorrent 寫盤，應用層的
    RateLimiter 攔不到，只能逐任務設 download_limit，因此按活躍任務數均分。"""

    def setUp(self):
        self.manager = DownloadManager()
        self.manager.config_file = os.path.join(tempfile.mkdtemp(), "config.json")

    def _add(self, task, status):
        task.status = status
        tid = self.manager.next_id
        self.manager.next_id += 1
        self.manager.task_ids[tid] = task
        return tid

    def _bt(self):
        return BTTask('magnet:?xt=urn:btih:' + 'a' * 40, tempfile.mkdtemp())

    def _http(self):
        return DownloadTask('http://example.com/f.bin', tempfile.mkdtemp(),
                            filename='f.bin')

    def test_bt_gets_whole_limit_when_alone(self):
        bt = self._bt()
        self._add(bt, 'downloading')
        self.manager.set_speed_limit(1024 * 1024)
        self.assertEqual(bt.download_rate_limit, 1024 * 1024)

    def test_mixed_tasks_split_evenly(self):
        bt = self._bt()
        http = self._http()
        self._add(bt, 'downloading')
        self._add(http, 'downloading')
        self.manager.set_speed_limit(1000)
        self.assertEqual(bt.download_rate_limit, 500)
        # HTTP 任務共用同一個桶，桶速率 = 每任務配額 × HTTP 任務數
        self.assertEqual(self.manager.rate_limiter.get_rate(), 500)

    def test_paused_task_not_counted(self):
        bt = self._bt()
        http = self._http()
        self._add(bt, 'downloading')
        self._add(http, 'paused')
        self.manager.set_speed_limit(1000)
        self.assertEqual(bt.download_rate_limit, 1000)

    def test_zero_limit_clears_bt_cap(self):
        bt = self._bt()
        self._add(bt, 'downloading')
        bt.download_rate_limit = 4096
        self.manager.set_speed_limit(0)
        self.assertEqual(bt.download_rate_limit, 0)
        self.assertEqual(self.manager.rate_limiter.get_rate(), 0)

    def test_cancel_task_reallocates(self):
        bt = self._bt()
        http = self._http()
        self._add(bt, 'downloading')
        tid = self._add(http, 'downloading')
        self.manager.set_speed_limit(1000)
        self.assertEqual(bt.download_rate_limit, 500)
        self.manager.cancel_task(tid)
        self.assertEqual(bt.download_rate_limit, 1000)


if __name__ == "__main__":
    unittest.main()
