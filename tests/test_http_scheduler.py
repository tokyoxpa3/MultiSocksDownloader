# -*- coding: utf-8 -*-
"""HTTP 分段調度：快慢兩條線路時，快的線路必須吃下大部分位元組。

回歸的是「開場即靜態均分」。舊版固定 64 MiB 顆粒、每線 6 個 worker，只要
worker 數 × 顆粒 ≥ 檔案大小，全部區塊在啟動瞬間就被認領完；快線下完自己那
一份之後 `_pop_run()` 永遠回 None，worker 直接退場，只能等慢線把另一半爬完。
實測 384 MiB 檔案剛好 50/50（500 Mbps 那半 3 秒結束，100 Mbps 那半要 15 秒）。

用假的 session 取代真實連線：這裡要驗的是調度決策，不是 socket 行為，用真的
SOCKS5 代理會讓測試依賴外部環境且無法控制速率。
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from downloader import DownloadTask, CLAIM_LEASE_SECONDS  # noqa: E402

MiB = 1024 * 1024
PAYLOAD_UNIT = bytes(range(256))


class _FakeResponse(object):
    """假的 206 回應：以固定速率吐資料，並記錄該線路實際收到多少位元組。"""

    def __init__(self, body, rate, on_read):
        self._body = body
        self._rate = max(1.0, float(rate))
        self._on_read = on_read
        self.status_code = 206
        self.url = 'http://fake/file.bin'
        self.headers = {}
        self._closed = False

    def iter_content(self, size):
        # 切片大小隨速率放大：Windows 的 sleep 解析度約 15.6 ms，固定 32 KiB
        # 在高速線路上會被計時器粒度壓成 2 MB/s，模擬出來的速率就不對了。
        # 同時保留「遠小於 run」的特性，讓搶佔與停滯判定能在區塊中間生效。
        step = max(32 * 1024, int(self._rate * 0.04))
        sent = 0
        while sent < len(self._body):
            if self._closed:
                return
            piece = self._body[sent:sent + step]
            sent += len(piece)
            self._on_read(len(piece))
            yield piece
            time.sleep(len(piece) / self._rate)

    def close(self):
        self._closed = True


class _FakeSession(object):
    def __init__(self, task, proxy):
        self._task = task
        self._key = task._line_key(proxy)
        self._rate = task.LINE_RATES[self._key]

    def get(self, url, headers=None, stream=False, timeout=None,
            allow_redirects=True):
        start, end = 0, self._task.total_size - 1
        spec = (headers or {}).get('Range', '').split('=', 1)[-1]
        a, _, b = spec.partition('-')
        if a:
            start = int(a)
        if b:
            end = int(b)
        end = min(end, self._task.total_size - 1)
        body = self._task.payload[start:end + 1]
        return _FakeResponse(body, self._rate,
                             lambda n: self._task._count(self._key, n))

    def close(self):
        pass


class _LineTask(DownloadTask):
    """兩條線路（直連 + 一個假代理），速率由 LINE_RATES 指定。"""

    TOTAL = 16 * MiB
    BLOCKS = 64
    LINE_RATES = {'direct': 1.5 * MiB, 'proxy:slow:1080': 0.15 * MiB}
    payload = b''
    SNUB_WINDOW = 0.8
    LINE_PREEMPT_COOLDOWN = 2.0

    def __init__(self, *args, **kwargs):
        super(_LineTask, self).__init__(*args, **kwargs)
        self.served = {}
        self._served_lock = threading.Lock()

    def _count(self, key, n):
        with self._served_lock:
            self.served[key] = self.served.get(key, 0) + n

    def served_on(self, key):
        with self._served_lock:
            return self.served.get(key, 0)

    def prepare(self):
        # 略過真實的網路探測，其餘檔案處理照舊（含原子佔位與 truncate）。
        self.total_size = self.TOTAL
        self.supports_range = True
        self._ensure_unique_filepath(reserve=True)
        self._build_blocks()
        self._ensure_temp_file()
        return True

    def _make_session(self, proxy):
        return _FakeSession(self, proxy)


def _wait_completed(task, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.status in ('completed', 'error', 'canceled'):
            return True
        time.sleep(0.05)
    return False


class _SchedulerCase(unittest.TestCase):
    SNUB_RATIO = DownloadTask.SNUB_RATIO

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='msd_sched_')
        payload = PAYLOAD_UNIT * (self.TOTAL // len(PAYLOAD_UNIT))
        handler = type('Task', (_LineTask,), {
            'payload': payload,
            'SNUB_RATIO': self.SNUB_RATIO,
            'LINE_RATES': dict(self.LINE_RATES),
        })
        self.payload = payload
        self.task = handler(
            'http://127.0.0.1:1/file.bin', self.tmp,
            filename='file.bin',
            proxies=[{'host': 'slow', 'port': 1080}],
            threads_per_proxy=2,
            chunks_per_part=self.BLOCKS)

    def tearDown(self):
        self.task._stop.set()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, timeout):
        started = time.time()
        self.assertTrue(self.task.start(), 'start() 應成功')
        self.assertTrue(_wait_completed(self.task, timeout),
                        '下載未在 %s 秒內結束，狀態=%s' % (timeout, self.task.status))
        elapsed = time.time() - started
        self.assertEqual(self.task.status, 'completed',
                         '狀態應為 completed，實際=%s 錯誤=%s'
                         % (self.task.status, self.task.error_message))
        return elapsed

    def _assert_content(self):
        with open(self.task.filepath, 'rb') as f:
            self.assertEqual(f.read(), self.payload, '檔案內容與來源不一致')

    def _slow_key(self):
        return 'proxy:slow:1080'

    def _fast_key(self):
        return 'direct'


class TestWorkStealing(_SchedulerCase):
    """只驗動態顆粒、預取配額與尾巴接管（把 snub 關掉，確保改善不是靠速率判定）。"""

    TOTAL = _LineTask.TOTAL
    BLOCKS = _LineTask.BLOCKS
    LINE_RATES = _LineTask.LINE_RATES
    SNUB_RATIO = 0.0   # 任何速率都 >= 0 × best，永遠不會判定為慢線

    def test_fast_line_takes_most_bytes(self):
        elapsed = self._run(timeout=20)
        total = self.TOTAL
        fast = self.task.served_on(self._fast_key())
        slow = self.task.served_on(self._slow_key())
        self._assert_content()
        self.assertGreaterEqual(
            fast / float(total), 0.80,
            '快線只吃了 %.1f%%（fast=%d slow=%d），工作偷取沒有生效'
            % (100.0 * fast / total, fast, slow))
        self.assertLess(
            elapsed, 14.0,
            '耗時 %.1fs 太長；靜態均分約需 29s，理想值約 4.9s' % elapsed)

    def test_tail_is_stolen_when_pool_runs_dry(self):
        self._run(timeout=20)
        self.assertGreater(
            self.task._steal_count, 0,
            '池子空了卻沒有接管慢線的尾巴（_steal_count=0）')
        self.assertEqual(
            self.task._snub_count, 0,
            'SNUB_RATIO=0 時不該有速率判定觸發的搶佔')

    def test_no_block_left_unclaimed(self):
        self._run(timeout=20)
        self.assertEqual(self.task._count_unclaimed_locked(), 0,
                         '完成後仍有未認領區塊')
        self.assertEqual(self.task._popcount(), self.task.block_count,
                         'bitmap 應全部標記完成')


class TestSlowLinePreempted(_SchedulerCase):
    """驗慢線搶佔（libtorrent snub 的對應物）。"""

    TOTAL = _LineTask.TOTAL
    BLOCKS = _LineTask.BLOCKS
    LINE_RATES = _LineTask.LINE_RATES
    SNUB_RATIO = 0.34

    def test_slow_line_yields_remaining_blocks(self):
        elapsed = self._run(timeout=20)
        self._assert_content()
        self.assertGreater(
            self.task._snub_count, 0,
            '慢線從未被速率判定搶佔，snub 沒有生效（_snub_count=0）')
        slow_share = self.task.served_on(self._slow_key()) / float(self.TOTAL)
        self.assertLess(slow_share, 0.20,
                        '慢線吃了 %.1f%%，應被壓在 20%% 以下' % (100 * slow_share))
        self.assertLess(elapsed, 14.0,
                        '耗時 %.1fs 太長（靜態均分約 29s）' % elapsed)

    def test_preemption_does_not_count_as_failure(self):
        self._run(timeout=20)
        self.assertEqual(
            self.task.error_message, '',
            '搶佔被誤記成失敗：%s' % self.task.error_message)
        self.assertFalse(self.task._fatal, '_fatal 不應被設定')


class TestDynamicRunSize(unittest.TestCase):
    """_pop_run 的顆粒應隨「剩餘量 / worker 數」收縮，而不是固定 64 MiB。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='msd_run_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, total, blocks=0, workers=12):
        t = DownloadTask('http://127.0.0.1:1/f.bin', self.tmp,
                         proxies=[], chunks_per_part=blocks,
                         threads_per_proxy=1)
        t.total_size = total
        t._build_blocks()
        t._target_workers = workers
        return t

    def test_run_shrinks_with_remaining_bytes(self):
        t = self._task(384 * MiB)          # 96 個 4 MiB 區塊
        self.assertEqual(t.block_count, 96)
        run = t._pop_run()
        self.assertIsNotNone(run)
        span = run[1] - run[0] + 1
        # 96 / (12 × 4) = 2 個區塊（8 MiB），遠小於舊版的 16 個區塊
        self.assertEqual(span, 2, '首個 run 應為 2 個區塊，實際 %d' % span)

    def test_run_never_below_one_block(self):
        t = self._task(384 * MiB)
        t._target_workers = 4096          # 分母大到 want 會變 0
        run = t._pop_run()
        self.assertEqual(run[1] - run[0] + 1, 1)

    def test_run_capped_by_target_run_bytes(self):
        t = self._task(8 * 1024 * MiB)    # 2048 個區塊，分母小 → 會撞上限
        t._target_workers = 1
        run = t._pop_run()
        self.assertEqual(run[1] - run[0] + 1, t._run_block_cap())


class TestClaimLease(unittest.TestCase):
    """worker 消失留下的認領必須會過期，否則區塊永遠沒人下載。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='msd_lease_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self):
        t = DownloadTask('http://127.0.0.1:1/f.bin', self.tmp,
                         proxies=[], chunks_per_part=8, threads_per_proxy=1)
        t.total_size = 8 * MiB
        t._build_blocks()
        t._target_workers = 1
        return t

    def test_fresh_claim_is_respected(self):
        t = self._task()
        t._set_block_claimed(0)
        run = t._pop_run()
        self.assertEqual(run[0], 1, '有效租約內的認領不該被搶走')

    def test_expired_claim_is_reclaimed(self):
        t = self._task()
        t._set_block_claimed(0)
        t._claim_at[0] = time.monotonic() - (CLAIM_LEASE_SECONDS + 5)
        run = t._pop_run()
        self.assertEqual(run[0], 0, '租約過期的區塊應可重新認領')


if __name__ == '__main__':
    unittest.main(verbosity=2)
