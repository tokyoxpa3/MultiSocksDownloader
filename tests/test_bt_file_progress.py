"""BT 逐檔進度（get_file_progress）的計算驗證。

不需要真實下載：直接建構 torrent 的檔案版面與 piece 完成狀態，驗證「把 piece
完成度依重疊位元組攤回檔案」的加權是否正確。重點是 piece 邊界橫跨檔案的情況
（piece 1000 位元組、檔案 1500 位元組時，piece 1 同時屬於前兩個檔案），
直接以 piece 數平均會讓小檔失真。
"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

import libtorrent as lt

from bt_downloader import BTTask


def make_multi_ti(name, files, piece_len):
    """建立多檔 torrent_info；piece hash 用 0 填滿（本測試不驗證內容）。"""
    total = sum(size for _, size in files)
    num = (total + piece_len - 1) // piece_len
    info = {
        b'name': name.encode(),
        b'piece length': piece_len,
        b'pieces': b'\x00' * 20 * num,
        b'files': [
            {b'length': size, b'path': [p.encode() for p in path.split('/')]}
            for path, size in files
        ],
    }
    return lt.torrent_info(lt.bencode({b'info': info}))


def make_single_ti(name, size, piece_len):
    num = (size + piece_len - 1) // piece_len
    info = {
        b'name': name.encode(),
        b'piece length': piece_len,
        b'pieces': b'\x00' * 20 * num,
        b'length': size,
    }
    return lt.torrent_info(lt.bencode({b'info': info}))


def fake_task(ti, pieces, blocks_done=None, blocks_per_piece=None, selected=None):
    """組出只帶進度狀態的 BTTask（不建立任何 libtorrent session）。"""
    task = object.__new__(BTTask)
    task._ti = ti
    task._num_pieces = ti.num_pieces()
    task._pieces = list(pieces)
    task._blocks_done = list(blocks_done or [0] * task._num_pieces)
    task._blocks_per_piece = list(blocks_per_piece or [1] * task._num_pieces)
    task._last_block_ts = [0.0] * task._num_pieces
    task._file_cache = None
    task._lock = threading.Lock()
    task.selected_files = selected
    return task


class TestFileProgress(unittest.TestCase):
    def test_piece_spanning_files_is_byte_weighted(self):
        # 版面：a.bin 0~1499、sub/b.bin 1500~1999、c.bin 2000~3999
        # piece 0=0~999、piece 1=1000~1999、piece 2=2000~2999、piece 3=3000~3999
        ti = make_multi_ti('pkg', [('a.bin', 1500), ('sub/b.bin', 500),
                                   ('c.bin', 2000)], 1000)
        task = fake_task(ti, [1, 0, 0, 0])

        rows = {r['path']: r for r in task.get_file_progress()}
        self.assertEqual(list(rows), ['a.bin', 'sub/b.bin', 'c.bin'])

        # a.bin 只有 piece 0 完整（1000 位元組），piece 1 完全沒下載
        self.assertEqual(rows['a.bin']['done'], 1000)
        self.assertEqual(rows['a.bin']['state'], 'partial')
        self.assertAlmostEqual(rows['a.bin']['progress'], 1000 / 1500 * 100, places=4)
        # 小檔只落在 piece 1，piece 1 沒完成 → 一個位元組都沒有
        self.assertEqual(rows['sub/b.bin']['done'], 0)
        self.assertEqual(rows['sub/b.bin']['state'], 'pending')
        self.assertEqual(rows['c.bin']['state'], 'pending')

    def test_shared_piece_credits_both_files(self):
        # piece 1 橫跨 a.bin 的尾巴與 b.bin 的開頭：完成它要同時算給兩個檔案
        ti = make_multi_ti('pkg', [('a.bin', 1500), ('sub/b.bin', 500),
                                   ('c.bin', 2000)], 1000)
        task = fake_task(ti, [1, 1, 0, 0])
        rows = {r['path']: r for r in task.get_file_progress()}
        self.assertEqual(rows['a.bin']['done'], 1500)
        self.assertEqual(rows['a.bin']['state'], 'done')
        self.assertEqual(rows['sub/b.bin']['done'], 500)
        self.assertEqual(rows['sub/b.bin']['state'], 'done')
        self.assertEqual(rows['c.bin']['state'], 'pending')

    def test_all_pieces_done_marks_every_file_done(self):
        ti = make_multi_ti('pkg', [('a.bin', 1500), ('sub/b.bin', 500),
                                   ('c.bin', 2000)], 1000)
        task = fake_task(ti, [1, 1, 1, 1])
        rows = task.get_file_progress()
        for r in rows:
            self.assertEqual(r['state'], 'done', r['path'])
            self.assertEqual(r['done'], r['size'], r['path'])
            self.assertEqual(r['progress'], 100.0, r['path'])

    def test_partial_block_counts_toward_file(self):
        # 一個 piece 只有一個 block（piece_len < 16 KiB），完成該 block 等於完成 piece
        ti = make_multi_ti('pkg', [('a.bin', 1500), ('sub/b.bin', 500),
                                   ('c.bin', 2000)], 1000)
        task = fake_task(ti, [0, 0, 0, 0], blocks_done=[1, 0, 0, 0],
                         blocks_per_piece=[1, 1, 1, 1])
        rows = {r['path']: r for r in task.get_file_progress()}
        self.assertEqual(rows['a.bin']['done'], 1000)
        self.assertEqual(rows['a.bin']['state'], 'partial')

    def test_half_piece_gives_proportional_bytes(self):
        # a.bin 0~1499、b.bin 1500~3999；piece 0=0~1999、piece 1=2000~3999
        # piece 0 完成、piece 1 完成 4/8 個 block → b.bin 拿到 500 + 1000
        ti = make_multi_ti('pkg', [('a.bin', 1500), ('b.bin', 2500)], 2000)
        task = fake_task(ti, [1, 0], blocks_done=[0, 4],
                         blocks_per_piece=[8, 8])
        rows = {r['path']: r for r in task.get_file_progress()}
        self.assertEqual(rows['a.bin']['done'], 1500)
        self.assertEqual(rows['a.bin']['state'], 'done')
        self.assertEqual(rows['b.bin']['done'], 1500)
        self.assertEqual(rows['b.bin']['state'], 'partial')
        self.assertAlmostEqual(rows['b.bin']['progress'], 60.0, places=4)

    def test_unselected_files_are_skipped(self):
        ti = make_multi_ti('pkg', [('a.bin', 1500), ('sub/b.bin', 500),
                                   ('c.bin', 2000)], 1000)
        task = fake_task(ti, [0, 0, 0, 0], selected=[2])
        rows = {r['path']: r for r in task.get_file_progress()}
        self.assertEqual(rows['a.bin']['state'], 'skipped')
        self.assertEqual(rows['sub/b.bin']['state'], 'skipped')
        self.assertEqual(rows['c.bin']['state'], 'pending')

    def test_single_file_torrent(self):
        ti = make_single_ti('one.bin', 3000, 1000)
        task = fake_task(ti, [1, 0, 0])
        rows = task.get_file_progress()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['path'], 'one.bin')
        self.assertEqual(rows[0]['done'], 1000)
        self.assertEqual(rows[0]['state'], 'partial')

    def test_no_metadata_returns_empty(self):
        task = object.__new__(BTTask)
        task._ti = None
        task._file_cache = None
        self.assertEqual(task.get_file_progress(), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
