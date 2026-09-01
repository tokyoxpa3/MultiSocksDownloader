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
    torrent_file_tree,
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


if __name__ == '__main__':
    unittest.main()
