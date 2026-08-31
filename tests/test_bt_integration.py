"""BT 單一 session 獨立下載的端到端整合測試（loopback，無需對外網路）。

以本地 libtorrent seeder 做種，BTTask 建立單一 session 連線下載，驗證落盤檔案
內容與原始資料一致。
"""

import os
import sys
import time
import hashlib
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libtorrent as lt
from bt_downloader import BTTask


def make_ti(piece_len, name, data):
    """建立 (torrent_info, info_dict)；info_dict 用於寫出 .torrent 檔。"""
    total = len(data)
    num = (total + piece_len - 1) // piece_len
    ph = b''.join(
        hashlib.sha1(data[p * piece_len:(p + 1) * piece_len]).digest()
        for p in range(num))
    info = {b'name': name.encode(), b'length': total,
            b'piece length': piece_len, b'pieces': ph}
    return lt.torrent_info(lt.bencode({b'info': info})), info


class TestSingleSessionBTTask(unittest.TestCase):
    TIMEOUT = 60

    def test_single_session_download_and_verify(self):
        tmp = tempfile.mkdtemp()
        seed_dir = os.path.join(tmp, 'seed')
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(seed_dir)
        os.makedirs(dl_dir)

        name = 'test_payload.bin'
        data = os.urandom(64 * 1024)  # 64 KiB / 16 KiB = 4 pieces
        with open(os.path.join(seed_dir, name), 'wb') as f:
            f.write(data)
        ti, info = make_ti(16 * 1024, name, data)

        # 寫出 .torrent 檔供 BTTask 載入
        torrent_file_path = os.path.join(tmp, 'sample.torrent')
        with open(torrent_file_path, 'wb') as f:
            f.write(lt.bencode({b'info': info}))

        # 本地 seeder
        seed_port = 22000 + (os.getpid() % 500)
        seed = lt.session({'listen_interfaces': f'127.0.0.1:{seed_port}',
                           'enable_dht': False, 'enable_lsd': False,
                           'enable_upnp': False, 'enable_natpmp': False})
        sp = lt.add_torrent_params()
        sp.ti = ti
        sp.save_path = seed_dir
        sp.flags = lt.torrent_flags.seed_mode
        sh = seed.add_torrent(sp)

        task = None
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                seed.post_torrent_updates()
                if sh.status().is_seeding:
                    break
                time.sleep(0.2)
            self.assertTrue(sh.status().is_seeding, 'seeder 未進入 seeding')

            # 啟動 BTTask，直連 session
            task = BTTask(torrent_file_path, dl_dir)
            self.assertTrue(task.start())

            # 主動連線本地 seeder
            task._line.handle.connect_peer(('127.0.0.1', seed_port))

            deadline = time.time() + self.TIMEOUT
            while time.time() < deadline:
                if task.is_completed():
                    break
                time.sleep(0.3)

            self.assertTrue(task.is_completed(), 'BTTask 未在時限內完成下載')
            out_file = os.path.join(dl_dir, name)
            self.assertTrue(os.path.isfile(out_file))
            with open(out_file, 'rb') as f:
                self.assertEqual(f.read(), data)
        finally:
            if task is not None:
                task.cancel()
            seed.remove_torrent(sh)


if __name__ == '__main__':
    unittest.main()
