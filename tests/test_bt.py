import os
import sys
import json
import hashlib
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libtorrent as lt
from bt_downloader import (
    BTTask, is_magnet, source_kind, magnet_display_name, partition_ranges, LineSession,
    torrent_file_tree, bt_info_hash, _downsample_blocks, _verified_done,
    _stall_timeout_for, _update_stall,
)


def make_torrent_info(piece_len, files):
    """以確定性內容建立 torrent_info；files 為 [(name, data_bytes), ...]。"""
    stream = b''.join(d for _, d in files)
    total = len(stream)
    num_pieces = (total + piece_len - 1) // piece_len
    pieces_hash = b''.join(
        hashlib.sha1(stream[p * piece_len:(p + 1) * piece_len]).digest()
        for p in range(num_pieces))
    if len(files) == 1:
        name, data = files[0]
        info = {b'name': name.encode(), b'length': len(data),
                b'piece length': piece_len, b'pieces': pieces_hash}
    else:
        info = {
            b'name': b'root',
            b'piece length': piece_len,
            b'pieces': pieces_hash,
            b'files': [
                {b'length': len(d),
                 b'path': [x.encode() for x in n.split('/')]}
                for n, d in files
            ],
        }
    return lt.torrent_info(lt.bencode({b'info': info})), stream


class TestSourceKind(unittest.TestCase):
    def test_magnet(self):
        self.assertEqual(source_kind('magnet:?xt=urn:btih:aaaa'), 'magnet')
        self.assertEqual(source_kind('MAGNET:?xt=urn:btih:aaaa'), 'magnet')

    def test_http_not_bt(self):
        self.assertIsNone(source_kind('http://example.com/f.zip'))
        self.assertIsNone(source_kind('ftp://example.com/f.zip'))

    def test_torrent_file(self):
        with tempfile.NamedTemporaryFile(suffix='.torrent', delete=False) as f:
            f.write(b'x')
            path = f.name
        try:
            self.assertEqual(source_kind(path), 'torrent')
        finally:
            os.remove(path)

    def test_torrent_missing_file(self):
        self.assertIsNone(source_kind('C:/nope/does_not_exist.torrent'))


class TestIsMagnet(unittest.TestCase):
    def test_true(self):
        self.assertTrue(is_magnet('magnet:?xt=urn:btih:bbbb'))

    def test_false(self):
        self.assertFalse(is_magnet('http://x'))
        self.assertFalse(is_magnet(''))


class TestMagnetDisplayName(unittest.TestCase):
    def test_dn(self):
        self.assertEqual(
            magnet_display_name('magnet:?xt=urn:btih:cc&dn=My%20File'),
            'My File')

    def test_no_dn(self):
        self.assertEqual(magnet_display_name('magnet:?xt=urn:btih:cc'), '')


class TestPartitionRanges(unittest.TestCase):
    def test_even(self):
        self.assertEqual(partition_ranges(10, 2), [(0, 5), (5, 10)])

    def test_remainder(self):
        self.assertEqual(partition_ranges(10, 3), [(0, 4), (4, 7), (7, 10)])

    def test_more_sessions_than_pieces(self):
        self.assertEqual(partition_ranges(2, 5), [(0, 1), (1, 2), (2, 2), (2, 2), (2, 2)])

    def test_empty(self):
        self.assertEqual(partition_ranges(0, 2), [])
        self.assertEqual(partition_ranges(10, 0), [])


class TestStallDetection(unittest.TestCase):
    """卡死偵測與收尾救援的純函式邏輯。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_stall_timeout_endgame_shorter(self):
        self.assertLess(_stall_timeout_for(3), _stall_timeout_for(100))

    def test_update_stall_first_seen_no_stall(self):
        since, stalled = _update_stall(None, 0, 0, None, 100.0, 60.0)
        self.assertIsNone(since)
        self.assertFalse(stalled)

    def test_update_stall_progress_resets(self):
        since, stalled = _update_stall(0, 1, 0, None, 10.0, 5.0)
        self.assertIsNone(since)
        self.assertFalse(stalled)

    def test_update_stall_rate_resets(self):
        since, stalled = _update_stall(5, 5, 1024, None, 10.0, 5.0)
        self.assertIsNone(since)
        self.assertFalse(stalled)

    def test_update_stall_accumulates_then_triggers(self):
        since, stalled = _update_stall(5, 5, 0, None, 100.0, 60.0)
        self.assertEqual(since, 100.0)
        self.assertFalse(stalled)
        since2, stalled2 = _update_stall(5, 5, 0, since, 200.0, 60.0)
        self.assertEqual(since2, 100.0)
        self.assertTrue(stalled2)

    def test_remaining_pieces_for(self):
        t = BTTask('magnet:?xt=urn:btih:st', self.tmpdir, proxies=[None, None])
        t._num_pieces = 4
        t._piece_owner = [0, 0, 1, 1]
        merged = [True, False, False, True]
        self.assertEqual(t._remaining_pieces_for(0, merged), [1])
        self.assertEqual(t._remaining_pieces_for(1, merged), [2])


class TestBTTaskConstruction(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_magnet_display_name(self):
        t = BTTask('magnet:?xt=urn:btih:dd&dn=Hello', self.tmpdir)
        self.assertEqual(t.kind, 'magnet')
        self.assertEqual(t.filename, 'Hello')
        self.assertIsNone(t.proxy)

    def test_magnet_no_dn_fallback(self):
        t = BTTask('magnet:?xt=urn:btih:dd', self.tmpdir)
        self.assertEqual(t.filename, '磁力連結下載')

    def test_torrent_basename(self):
        path = os.path.join(self.tmpdir, 'archive.torrent')
        with open(path, 'wb') as f:
            f.write(b'x')
        t = BTTask(path, self.tmpdir)
        self.assertEqual(t.kind, 'torrent')
        self.assertEqual(t.filename, 'archive')

    def test_explicit_filename_wins(self):
        t = BTTask('magnet:?xt=urn:btih:dd&dn=Ignored', self.tmpdir, filename='mine.bin')
        self.assertEqual(t.filename, 'mine.bin')

    def test_proxy_assignment(self):
        proxy = {'host': '127.0.0.1', 'port': 1080}
        t = BTTask('magnet:?xt=urn:btih:dd', self.tmpdir, proxy=proxy)
        self.assertEqual(t.proxy, proxy)
        self.assertEqual(t.proxies, [proxy])
        self.assertEqual(t._line_key, 'proxy:127.0.0.1:1080')

    def test_proxies_fallback(self):
        proxy = {'host': '127.0.0.1', 'port': 1080}
        t = BTTask('magnet:?xt=urn:btih:dd', self.tmpdir, proxies=[proxy])
        self.assertEqual(t.proxy, proxy)


class TestBTTaskProgress(unittest.TestCase):
    def setUp(self):
        self.t = BTTask('magnet:?xt=urn:btih:ee', tempfile.mkdtemp())

    def test_initial_progress_schema(self):
        p = self.t.get_progress()
        for key in ('total_size', 'downloaded_size', 'percentage', 'speed',
                    'status', 'error_message', 'elapsed_time', 'thread_count',
                    'block_count', 'blocks', 'line_bytes', 'line_labels'):
            self.assertIn(key, p)
        self.assertEqual(p['status'], 'initialized')
        self.assertEqual(p['total_size'], 0)
        # 單一 session：直連線路即時可見
        self.assertEqual(p['line_labels'], {'direct': '直連'})

    def test_start_invalid_source_fails(self):
        t = BTTask('not-a-valid-source', tempfile.mkdtemp())
        self.assertFalse(t.start())
        self.assertEqual(t.status, 'error')


class TestLineSessionCreation(unittest.TestCase):
    def test_direct_session(self):
        ls = LineSession('direct', None)
        self.assertIsNotNone(ls.session)

    def test_socks5_session(self):
        ls = LineSession('proxy:127.0.0.1:1080', {'host': '127.0.0.1', 'port': 1080})
        self.assertIsNotNone(ls.session)


class TestStatePersistence(unittest.TestCase):
    def _make(self):
        tmp = tempfile.mkdtemp()
        t = BTTask('magnet:?xt=urn:btih:ff', tmp, proxy={'host': '1.2.3.4', 'port': 1080})
        t._work_root = os.path.join(tmp, '.bt_tmp', 'abc123')
        return t, tmp

    def test_roundtrip(self):
        t, _tmp = self._make()
        t._save_state()
        path = os.path.join(t._work_root, 'task.json')
        self.assertTrue(os.path.isfile(path))
        with open(path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        self.assertEqual(loaded['source'], 'magnet:?xt=urn:btih:ff')
        self.assertEqual(loaded['proxy'], {'host': '1.2.3.4', 'port': 1080})
        self.assertEqual(loaded['kind'], 'magnet')


class TestScanBtTasks(unittest.TestCase):
    def test_scan_creates_bt_task(self):
        from downloader import DownloadManager
        mgr = DownloadManager()
        mgr.config_file = os.path.join(tempfile.mkdtemp(), 'config.json')
        save_dir = tempfile.mkdtemp()
        # 完全隔離：不繼承真實 config 的 download_dirs，避免掃到別處殘留的 .bt_tmp
        mgr.download_dirs = {save_dir}

        root = os.path.join(save_dir, '.bt_tmp', 'abc123')
        os.makedirs(root)
        with open(os.path.join(root, 'task.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'source': 'magnet:?xt=urn:btih:gg',
                'save_dir': save_dir,
                'filename': 'x',
                'kind': 'magnet',
                'proxy': {'host': '5.6.7.8', 'port': 1080},
            }, f)

        count = mgr.scan_unfinished_tasks()
        self.assertEqual(count, 1)
        self.assertIn('magnet:?xt=urn:btih:gg', mgr.tasks)
        t = mgr.tasks['magnet:?xt=urn:btih:gg']
        self.assertIsInstance(t, BTTask)
        self.assertEqual(t.status, 'paused')
        self.assertEqual(t.proxy, {'host': '5.6.7.8', 'port': 1080})

    def test_scan_ignores_empty_tmp(self):
        from downloader import DownloadManager
        mgr = DownloadManager()
        mgr.config_file = os.path.join(tempfile.mkdtemp(), 'config.json')
        save_dir = tempfile.mkdtemp()
        # 完全隔離：不繼承真實 config 的 download_dirs，避免掃到別處殘留的 .bt_tmp
        mgr.download_dirs = {save_dir}
        os.makedirs(os.path.join(save_dir, '.bt_tmp', 'empty'))
        count = mgr.scan_unfinished_tasks()
        self.assertEqual(count, 0)

    def test_cancel_scanned_task_cleans_bt_tmp(self):
        """刪除重開後掃描回來的 paused 任務時，應一併清掉 .bt_tmp 目錄，
        否則重開後 scan_unfinished_tasks() 會把同一任務再掃回來。"""
        from downloader import DownloadManager
        mgr = DownloadManager()
        mgr.config_file = os.path.join(tempfile.mkdtemp(), 'config.json')
        save_dir = tempfile.mkdtemp()
        mgr.download_dirs = {save_dir}

        ih = 'a' * 40
        source = 'magnet:?xt=urn:btih:' + ih
        root = os.path.join(save_dir, '.bt_tmp', ih)
        os.makedirs(root)
        with open(os.path.join(root, 'task.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'source': source,
                'save_dir': save_dir,
                'filename': 'x',
                'kind': 'magnet',
                'proxy': None,
            }, f)

        self.assertEqual(mgr.scan_unfinished_tasks(), 1)
        task_id = mgr.tasks[source].task_id
        # 掃描時已記錄實際 _work_root，刪除即清掉 .bt_tmp 目錄
        self.assertTrue(mgr.cancel_task(task_id))
        self.assertFalse(os.path.exists(root))

    def test_cancel_scanned_task_cleans_sentinel_dir(self):
        """目錄名為舊版哨兵值（如 'nohash'）時，刪除仍應清掉該目錄，
        不能靠 source 的 info hash 反推（兩者對不上）。"""
        from downloader import DownloadManager
        mgr = DownloadManager()
        mgr.config_file = os.path.join(tempfile.mkdtemp(), 'config.json')
        save_dir = tempfile.mkdtemp()
        mgr.download_dirs = {save_dir}

        source = 'magnet:?xt=urn:btih:' + 'a' * 40  # 真實 info hash = 'a'*40
        root = os.path.join(save_dir, '.bt_tmp', 'nohash')  # 舊版留下的哨兵目錄名
        os.makedirs(root)
        with open(os.path.join(root, 'task.json'), 'w', encoding='utf-8') as f:
            json.dump({
                'source': source,
                'save_dir': save_dir,
                'filename': 'x',
                'kind': 'magnet',
                'proxy': None,
            }, f)

        self.assertEqual(mgr.scan_unfinished_tasks(), 1)
        task_id = mgr.tasks[source].task_id
        self.assertTrue(mgr.cancel_task(task_id))
        self.assertFalse(os.path.exists(root))

    def test_cancel_incomplete_removes_partial_file(self):
        """取消未下載完成的 BT 任務時，應連同 save_dir 下的部分檔案一併刪除。"""
        save_dir = tempfile.mkdtemp()
        task = BTTask('magnet:?xt=urn:btih:' + 'a' * 40, save_dir,
                      filename='partial.bin')
        # 模擬 libtorrent 以 sparse 模式直接寫入 save_path 的部分檔案
        with open(task.filepath, 'wb') as f:
            f.write(b'partial data')
        self.assertTrue(os.path.exists(task.filepath))

        self.assertTrue(task.cancel())
        self.assertFalse(os.path.exists(task.filepath))

    def test_cancel_completed_preserves_file(self):
        """已下載完成（做種/完成）的成品檔案，取消任務時應保留、不誤刪。"""
        save_dir = tempfile.mkdtemp()
        task = BTTask('magnet:?xt=urn:btih:' + 'b' * 40, save_dir,
                      filename='done.bin')
        with open(task.filepath, 'wb') as f:
            f.write(b'complete data')
        task._download_completed_at = 1.0  # 標記已下載完成（進入做種/已完成）

        self.assertTrue(task.cancel())
        self.assertTrue(os.path.exists(task.filepath))


class TestTorrentFileTree(unittest.TestCase):
    def test_single_file(self):
        ti, _ = make_torrent_info(16384, [('solo.bin', b'x' * 100)])
        tree = torrent_file_tree(ti)
        self.assertEqual(len(tree), 1)
        path, size = tree[0]
        self.assertTrue(path.endswith('solo.bin'))
        self.assertEqual(size, 100)

    def test_multi_file_nested(self):
        ti, _ = make_torrent_info(16384, [('a/b.txt', b'111'), ('c.txt', b'22')])
        tree = torrent_file_tree(ti)
        self.assertEqual(len(tree), 2)
        paths = [p for p, _ in tree]
        # 路徑統一以 '/' 分隔，且各檔名出現在對應路徑末端
        self.assertTrue(any(p.endswith('a/b.txt') for p in paths))
        self.assertTrue(any(p.endswith('c.txt') for p in paths))
        self.assertFalse(any('\\' in p for p in paths))
        sizes = {size for _, size in tree}
        self.assertEqual(sizes, {3, 2})


class TestBTTaskSelectedFiles(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_wanted_size_partial(self):
        ti, _ = make_torrent_info(
            16384, [('a.bin', b'1' * 10), ('b.bin', b'2' * 20), ('c.bin', b'3' * 30)])
        t = BTTask('magnet:?xt=urn:btih:aa', self.tmpdir, selected_files=[0, 2])
        # a.bin(10) + c.bin(30) = 40
        self.assertEqual(t._wanted_size(ti), 40)

    def test_wanted_size_full(self):
        ti, _ = make_torrent_info(16384, [('a.bin', b'1' * 10), ('b.bin', b'2' * 20)])
        t = BTTask('magnet:?xt=urn:btih:aa', self.tmpdir, selected_files=None)
        self.assertEqual(t._wanted_size(ti), ti.total_size())

    def test_state_roundtrip_selected_files(self):
        t = BTTask('magnet:?xt=urn:btih:ff', self.tmpdir, selected_files=[2, 5])
        t._work_root = os.path.join(self.tmpdir, '.bt_tmp', 'abc')
        t._save_state()
        with open(os.path.join(t._work_root, 'task.json'), 'r', encoding='utf-8') as f:
            self.assertEqual(json.load(f)['selected_files'], [2, 5])


    def test_download_manager_bt_config(self):
        from downloader import DownloadManager
        with tempfile.TemporaryDirectory() as td:
            cfg = os.path.join(td, 'config.json')
            dm = DownloadManager()
            dm.config_dir = td
            dm.config_file = cfg
            dm.set_bt_seed_hours(24.5)
            dm.set_bt_upload_rate(204800)
            dm.save_config()

            dm2 = DownloadManager()
            dm2.config_dir = td
            dm2.config_file = cfg
            dm2.load_config()
            self.assertEqual(dm2.bt_seed_hours, 24.5)
            self.assertEqual(dm2.bt_upload_rate, 204800)


class TestMultiLineResume(unittest.TestCase):
    """多線路 resume 的合併位元圖持久化與剩餘 piece 分派。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_merged_pieces_roundtrip(self):
        t = BTTask('magnet:?xt=urn:btih:xx', self.tmpdir, proxies=[None, None])
        t._work_root = os.path.join(self.tmpdir, '.bt_tmp', 'abc123')
        t._persist_merged_pieces([True, False, True])
        self.assertEqual(t._load_merged_pieces(), [True, False, True])

    def test_assign_remaining_partitions(self):
        ti, _ = make_torrent_info(32 * 1024, [('x.bin', b'x' * (8 * 32 * 1024))])
        t = BTTask('magnet:?xt=urn:btih:yy', self.tmpdir, proxies=[None, None])
        t._ti = ti
        t._num_pieces = 8
        merged_have = [True, True, False, False, True, True, False, False]
        piece_owner, have = t._assign_remaining(merged_have)
        # 剩餘 piece [2,3,6,7] 分給 2 線：線 0 -> [2,3]、線 1 -> [6,7]
        self.assertEqual(piece_owner[2], 0)
        self.assertEqual(piece_owner[3], 0)
        self.assertEqual(piece_owner[6], 1)
        self.assertEqual(piece_owner[7], 1)
        # 已完成 piece 不指派給任何線路
        self.assertEqual(piece_owner[0], -1)
        self.assertEqual(piece_owner[1], -1)
        self.assertEqual(piece_owner[4], -1)
        self.assertEqual(piece_owner[5], -1)
        self.assertEqual(have, merged_have)

    def test_priorities_for(self):
        ti, _ = make_torrent_info(32 * 1024, [('x.bin', b'x' * (4 * 32 * 1024))])
        t = BTTask('magnet:?xt=urn:btih:zz', self.tmpdir, proxies=[None, None])
        t._ti = ti
        t._num_pieces = 4
        owner = [0, 0, 1, 1]
        self.assertEqual(t._priorities_for(0, owner), [1, 1, 0, 0])
        self.assertEqual(t._priorities_for(1, owner), [0, 0, 1, 1])

    def test_validate_resume(self):
        t = BTTask('magnet:?xt=urn:btih:ww', self.tmpdir, proxies=[None, None])
        t.filename = 'gone.bin'
        t.filepath = os.path.join(self.tmpdir, 'gone.bin')
        # 檔案不存在 -> 放棄續傳，改從頭下載
        self.assertIsNone(t._validate_resume([True, False, True]))
        # 檔案存在 -> 保留位元圖
        with open(t.filepath, 'wb') as f:
            f.write(b'x')
        self.assertEqual(t._validate_resume([True, False, True]), [True, False, True])

    def test_initial_owner_falls_back_to_full_partition(self):
        ti, _ = make_torrent_info(32 * 1024, [('x.bin', b'x' * (4 * 32 * 1024))])
        t = BTTask('magnet:?xt=urn:btih:oo', self.tmpdir, proxies=[None, None])
        t._ti = ti
        t._num_pieces = 4
        t._work_root = os.path.join(self.tmpdir, '.bt_tmp', 'none')
        self.assertEqual(t._initial_piece_owner(), [0, 0, 1, 1])

    def test_initial_owner_balances_remaining_and_avoids_orphans(self):
        ti, _ = make_torrent_info(32 * 1024, [('x.bin', b'x' * (8 * 32 * 1024))])
        t = BTTask('magnet:?xt=urn:btih:pp', self.tmpdir, proxies=[None, None])
        t._ti = ti
        t._num_pieces = 8
        t._work_root = os.path.join(self.tmpdir, '.bt_tmp', 'abc')
        # 前 4 塊已完成、後 4 塊剩餘
        t._persist_merged_pieces([True, True, True, True, False, False, False, False])
        owner = t._initial_piece_owner()
        # 剩餘 piece [4,5,6,7] 平均分給 2 線
        self.assertEqual(owner[4], 0)
        self.assertEqual(owner[5], 0)
        self.assertEqual(owner[6], 1)
        self.assertEqual(owner[7], 1)
        # 沒有 orphan（每個 piece 都有線路負責）
        self.assertNotIn(-1, owner)
        self.assertEqual(len(owner), 8)


class TestMagnetResumePlan(unittest.TestCase):
    """magnet 多線路 resume 的 _resume_have_from 決策（無需真實 session）。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _task(self, num_pieces=8):
        ti, _ = make_torrent_info(32 * 1024, [('x.bin', b'x' * (8 * 32 * 1024))])
        t = BTTask('magnet:?xt=urn:btih:mm', self.tmpdir, proxies=[None, None])
        t._ti = ti
        t._num_pieces = num_pieces
        return t

    def test_valid_partial_resume(self):
        t = self._task(8)
        merged = [True, True, False, False, True, True, False, False]
        plan = t._resume_have_from(merged)
        self.assertIsNotNone(plan)
        piece_owner, have, resume_bytes = plan
        self.assertEqual(have, merged)
        # 4 個已完成 piece 不指派給任何線路
        self.assertEqual(piece_owner.count(-1), 4)
        self.assertEqual(resume_bytes, 4 * 32 * 1024)

    def test_all_done(self):
        t = self._task(8)
        plan = t._resume_have_from([True] * 8)
        self.assertIsNotNone(plan)
        piece_owner, have, resume_bytes = plan
        self.assertEqual(piece_owner, [-1] * 8)
        self.assertEqual(resume_bytes, 8 * 32 * 1024)

    def test_none_and_empty_rejected(self):
        t = self._task(8)
        self.assertIsNone(t._resume_have_from(None))
        self.assertIsNone(t._resume_have_from([]))

    def test_length_mismatch_rejected(self):
        t = self._task(8)
        self.assertIsNone(t._resume_have_from([True] * 4))

    def test_all_pending_rejected(self):
        t = self._task(8)
        self.assertIsNone(t._resume_have_from([False] * 8))


class TestSingleLineResume(unittest.TestCase):
    """單線路 resume 續傳：清空 have_pieces 與 verified_pieces，強制 libtorrent
    對磁碟上的既有資料重新做 hash 校驗，避免把未真正落盤的資料誤判為完成。"""

    def test_try_load_resume_keeps_verified_empty(self):
        ti, _ = make_torrent_info(16384, [('a.bin', b'x' * 100)])
        t = BTTask('magnet:?xt=urn:btih:' + 'a' * 40, tempfile.mkdtemp())
        t._work_root = os.path.join(tempfile.mkdtemp(), '.bt_tmp', 'abc')
        os.makedirs(t._work_root, exist_ok=True)

        # 模擬 libtorrent save_resume_data 的輸出：只存 have_pieces，verified_pieces 為空
        p = lt.add_torrent_params()
        p.ti = ti
        p.save_path = t.save_dir
        p.have_pieces = [True] + [False] * (ti.num_pieces() - 1)
        p.verified_pieces = []
        with open(t._resume_path(), 'wb') as f:
            f.write(lt.write_resume_data_buf(p))

        params = lt.add_torrent_params()
        params.ti = ti
        rp = t._try_load_resume(params)
        self.assertIsNotNone(rp)
        # 不偽造 verified_pieces、也不保留 have_pieces：兩者都清空，強制 libtorrent
        # 以 checking_files 對磁碟資料重新 hash 校驗（have_pieces 非空會讓
        # libtorrent 2.1.1 跳過校驗、把損壞 piece 誤判為完成）。
        self.assertEqual(list(rp.verified_pieces), [])
        self.assertEqual(list(rp.have_pieces), [])


class TestBtInfoHash(unittest.TestCase):
    """跨來源字串去重的 info hash 提取。"""

    def test_magnet(self):
        ih = 'a' * 40
        self.assertEqual(bt_info_hash(f'magnet:?xt=urn:btih:{ih}'), ih)

    def test_torrent_file(self):
        data = b'x' * 100
        ph = hashlib.sha1(data).digest()
        info = {b'name': b'a.bin', b'length': 100, b'piece length': 16384, b'pieces': ph}
        path = os.path.join(tempfile.mkdtemp(), 't.torrent')
        with open(path, 'wb') as f:
            f.write(lt.bencode({b'info': info}))
        self.assertEqual(bt_info_hash(path), str(lt.torrent_info(path).info_hashes().v1))

    def test_non_bt_returns_none(self):
        self.assertIsNone(bt_info_hash('http://example.com/x.zip'))


class TestBtTaskDedup(unittest.TestCase):
    """磁力連結重複加入時，應去重返還同一 task_id，不建立重複任務。"""

    def _manager(self):
        from downloader import DownloadManager
        mgr = DownloadManager()
        mgr.config_file = os.path.join(tempfile.mkdtemp(), 'config.json')
        return mgr

    def test_same_magnet_returns_same_id(self):
        mgr = self._manager()
        save_dir = tempfile.mkdtemp()
        m = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=dedup_test'
        tid1 = mgr.add_task(m, save_dir=save_dir, use_proxy=False)
        tid2 = mgr.add_task(m, save_dir=save_dir, use_proxy=False)
        self.assertEqual(tid1, tid2)
        self.assertEqual(len(mgr.task_ids), 1)
        self.assertEqual(len(mgr.tasks), 1)

    def test_same_hash_different_dn_dedup(self):
        mgr = self._manager()
        save_dir = tempfile.mkdtemp()
        m1 = 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=one'
        m2 = 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=two'
        tid1 = mgr.add_task(m1, save_dir=save_dir, use_proxy=False)
        tid2 = mgr.add_task(m2, save_dir=save_dir, use_proxy=False)
        self.assertEqual(tid1, tid2)
        self.assertEqual(len(mgr.task_ids), 1)

    def test_start_task_does_not_restart_running(self):
        """已在執行的任務再次 start_task 不應重啟（避免中斷進行中的校驗/下載）。"""
        mgr = self._manager()
        save_dir = tempfile.mkdtemp()
        m = 'magnet:?xt=urn:btih:' + 'c' * 40 + '&dn=running'
        tid = mgr.add_task(m, save_dir=save_dir, use_proxy=False)
        task = mgr.task_ids[tid]
        task.status = 'downloading'  # 模擬已啟動並在校驗/下載中
        calls = []
        orig_start = task.start
        task.start = lambda: calls.append(True) or True
        try:
            self.assertTrue(mgr.start_task(tid))
        finally:
            task.start = orig_start
        self.assertEqual(calls, [])  # 不得再次呼叫 start()


class TestMagnetTrackerFallback(unittest.TestCase):
    """磁力連結應自動補上 HTTP/HTTPS tracker（UDP 被封環境的 metadata 來源）。"""

    def test_magnet_without_trackers_gets_http_fallback(self):
        t = BTTask('magnet:?xt=urn:btih:' + 'a' * 40, tempfile.mkdtemp())
        p = t._make_params_magnet()
        trackers = list(p.trackers)
        self.assertTrue(any(tr.startswith(('http://', 'https://')) for tr in trackers))

    def test_magnet_preserves_existing_udp_tracker(self):
        m = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&tr=udp://tracker.opentrackr.org:1337/announce'
        t = BTTask(m, tempfile.mkdtemp())
        p = t._make_params_magnet()
        trackers = list(p.trackers)
        self.assertIn('udp://tracker.opentrackr.org:1337/announce', trackers)
        self.assertTrue(any(tr.startswith(('http://', 'https://')) for tr in trackers))


class TestPieceProgress(unittest.TestCase):
    """BT 區塊進度：block 計數 → frac（0~1）與 active 的計算。"""

    def _task(self):
        tmpdir = tempfile.mkdtemp()
        # 4 個 32 KiB piece，每個 piece 含 2 個 16 KiB block，可測得部分進度。
        ti, _ = make_torrent_info(32 * 1024, [('a.bin', b'x' * (4 * 32 * 1024))])
        t = BTTask('magnet:?xt=urn:btih:' + 'd' * 40, tmpdir)
        t._ti = ti
        t._num_pieces = 4
        t._init_piece_progress()
        return t

    def test_compute_piece_states(self):
        t = self._task()
        now = 1000.0
        # piece 0 完成、piece 1 下載中（1/2 block，剛完成）、
        # piece 2 部分完成但已閒置、piece 3 未開始
        t._pieces = [1.0, 0.0, 0.0, 0.0]
        t._blocks_done = [0, 1, 1, 0]
        t._last_block_ts = [0.0, now - 1.0, now - 100.0, 0.0]
        states = t._compute_piece_states(now=now)
        self.assertEqual(states, [
            {'frac': 1.0, 'active': False},
            {'frac': 0.5, 'active': True},
            {'frac': 0.5, 'active': False},
            {'frac': 0.0, 'active': False},
        ])

    def test_fully_downloaded_piece_not_active(self):
        """區塊已全部下載完（frac=1.0）但尚未進入合併位元圖的 piece，
        應視為「已完成」而非「下載中」，active 必須為 False。"""
        t = self._task()
        now = 1000.0
        t._pieces = [0.0, 0.0, 0.0, 0.0]
        t._blocks_done = [2, 0, 0, 0]
        t._last_block_ts = [now - 1.0, 0.0, 0.0, 0.0]
        states = t._compute_piece_states(now=now)
        self.assertEqual(states[0], {'frac': 1.0, 'active': False})

    def test_downsample_blocks_averages_and_ors(self):
        states = [
            {'frac': 1.0, 'active': False},
            {'frac': 0.0, 'active': False},
            {'frac': 0.5, 'active': True},
            {'frac': 0.5, 'active': False},
        ]
        # 不超過 max_blocks：原樣保留
        self.assertEqual(_downsample_blocks(states, max_blocks=4), states)
        # 4 片壓成 2 區間：frac 平均、active 只要區間內任一片為 True
        down = _downsample_blocks(states, max_blocks=2)
        self.assertEqual(down, [
            {'frac': 0.5, 'active': False},
            {'frac': 0.5, 'active': True},
        ])

    def test_get_progress_blocks_empty_before_metadata(self):
        t = BTTask('magnet:?xt=urn:btih:' + 'e' * 40, tempfile.mkdtemp())
        p = t.get_progress()
        self.assertEqual(p['blocks'], [])
        self.assertEqual(p['block_count'], 0)


if __name__ == '__main__':
    unittest.main()
