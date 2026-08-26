import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from downloader import DownloadTask, DownloadManager, format_size


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


if __name__ == "__main__":
    unittest.main()
