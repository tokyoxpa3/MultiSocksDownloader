"""BT 單一 session 獨立下載的端到端整合測試（loopback，無需對外網路）。

以本地 libtorrent seeder 做種，BTTask 建立單一 session 連線下載，驗證落盤檔案
內容與原始資料一致。
"""

import os
import sys
import time
import hashlib
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libtorrent as lt
from bt_downloader import BTTask, _hash_hex

# 每個測試分配獨立 seeder 埠，避免同程序內多個測試（os.getpid() 相同）共用
# 同一埠，前一個測試的 libtorrent session 尚未回收而佔用埠，導致下載逾時。
_next_seed_port = 22000


def _alloc_seed_port():
    global _next_seed_port
    port = _next_seed_port
    _next_seed_port += 2
    return port


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


# 關閉 BTTask 各線路 session 的 DHT/LSD/UPnP/NAT-PMP。整合測試以 connect_peer
# 直連本機 seeder 完成下載，不需任何節點發現協定；這些協定（尤其 DHT 走 UDP）
# 在無對外網路的測試環境會持續重試、互相干擾，導致整套測試並行時偶發逾時。
_DISABLE_DISCOVERY = {
    'enable_dht': False,
    'enable_lsd': False,
    'enable_upnp': False,
    'enable_natpmp': False,
}


def _quiet_task_sessions(task):
    """在 start() 前關閉 task 各線路 session 的節點發現協定。"""
    for line in task._lines:
        try:
            line.session.apply_settings(_DISABLE_DISCOVERY)
        except Exception:
            pass


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
        seed_port = _alloc_seed_port()
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
            _quiet_task_sessions(task)
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


class TestMultiSessionBTTask(unittest.TestCase):
    """多線路分片下載：多個 session 共用 save_path、piece 不重疊，驗證合併檔正確。"""

    TIMEOUT = 60

    def test_multi_session_shared_file_download(self):
        tmp = tempfile.mkdtemp()
        seed_dir = os.path.join(tmp, 'seed')
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(seed_dir)
        os.makedirs(dl_dir)

        name = 'payload.bin'
        data = os.urandom(256 * 1024)  # 32 KiB piece -> 8 pieces，2 session 各 4 pieces
        with open(os.path.join(seed_dir, name), 'wb') as f:
            f.write(data)
        ti, info = make_ti(32 * 1024, name, data)

        torrent_file_path = os.path.join(tmp, 'sample.torrent')
        with open(torrent_file_path, 'wb') as f:
            f.write(lt.bencode({b'info': info}))

        seed_port = _alloc_seed_port()
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

            # 兩條直連線路驗證分片機制（實務為直連 + 不同 SOCKS5；key 碰撞不影響下載正確性）
            task = BTTask(torrent_file_path, dl_dir, proxies=[None, None])
            _quiet_task_sessions(task)
            self.assertTrue(task.start())
            self.assertEqual(len(task._lines), 2)

            task._lines[0].handle.connect_peer(('127.0.0.1', seed_port))
            task._lines[1].handle.connect_peer(('127.0.0.1', seed_port))

            deadline = time.time() + self.TIMEOUT
            while time.time() < deadline:
                if task.is_completed():
                    break
                time.sleep(0.3)

            self.assertTrue(task.is_completed(), '多線 BT 未在時限內完成')
            out_file = os.path.join(dl_dir, name)
            self.assertTrue(os.path.isfile(out_file))
            with open(out_file, 'rb') as f:
                self.assertEqual(f.read(), data)
        finally:
            if task is not None:
                task.cancel()
            seed.remove_torrent(sh)

    def test_resume_completes_without_seeder(self):
        """完整下載後停止 seeder，再以同 .torrent 重啟：新任務不帶 have_pieces，
        依賴 libtorrent 的 checking_files 驗證磁碟上已存在的檔案，不需 seeder 即完成。"""
        tmp = tempfile.mkdtemp()
        seed_dir = os.path.join(tmp, 'seed')
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(seed_dir)
        os.makedirs(dl_dir)

        name = 'payload.bin'
        data = os.urandom(256 * 1024)
        with open(os.path.join(seed_dir, name), 'wb') as f:
            f.write(data)
        ti, info = make_ti(32 * 1024, name, data)
        torrent_file_path = os.path.join(tmp, 'sample.torrent')
        with open(torrent_file_path, 'wb') as f:
            f.write(lt.bencode({b'info': info}))

        seed_port = _alloc_seed_port()
        seed = lt.session({'listen_interfaces': f'127.0.0.1:{seed_port}',
                           'enable_dht': False, 'enable_lsd': False,
                           'enable_upnp': False, 'enable_natpmp': False})
        sp = lt.add_torrent_params()
        sp.ti = ti
        sp.save_path = seed_dir
        sp.flags = lt.torrent_flags.seed_mode
        sh = seed.add_torrent(sp)
        deadline = time.time() + 15
        while time.time() < deadline:
            seed.post_torrent_updates()
            if sh.status().is_seeding:
                break
            time.sleep(0.2)
        self.assertTrue(sh.status().is_seeding)

        # 第一階段：完整下載
        task = BTTask(torrent_file_path, dl_dir, proxies=[None, None])
        _quiet_task_sessions(task)
        self.assertTrue(task.start())
        task._lines[0].handle.connect_peer(('127.0.0.1', seed_port))
        task._lines[1].handle.connect_peer(('127.0.0.1', seed_port))
        deadline = time.time() + self.TIMEOUT
        while time.time() < deadline:
            if task.is_completed():
                break
            time.sleep(0.3)
        self.assertTrue(task.is_completed())

        # 停止 seeder：資料檔已在磁碟，續傳只靠本地 hash 校驗即可完成、不需 seeder。
        seed.remove_torrent(sh)

        task2 = BTTask(torrent_file_path, dl_dir, proxies=[None, None])
        _quiet_task_sessions(task2)
        self.assertTrue(task2.start())
        deadline = time.time() + 10
        while time.time() < deadline:
            if task2.is_completed():
                break
            time.sleep(0.3)
        self.assertTrue(task2.is_completed(), '續傳未能在無 seeder 下完成')

    def test_resume_repairs_corrupt_piece(self):
        """完整下載後故意損壞磁碟上的一個 piece，重啟任務應靠 checking_files 揪出
        壞 piece 並補下修復，最終檔案與來源一致（回歸：修正前會誤報 100% 完成）。"""
        tmp = tempfile.mkdtemp()
        seed_dir = os.path.join(tmp, 'seed')
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(seed_dir)
        os.makedirs(dl_dir)

        name = 'payload.bin'
        data = os.urandom(256 * 1024)  # 32 KiB piece -> 8 pieces
        with open(os.path.join(seed_dir, name), 'wb') as f:
            f.write(data)
        ti, info = make_ti(32 * 1024, name, data)
        torrent_file_path = os.path.join(tmp, 'sample.torrent')
        with open(torrent_file_path, 'wb') as f:
            f.write(lt.bencode({b'info': info}))

        seed_port = _alloc_seed_port()
        seed = lt.session({'listen_interfaces': f'127.0.0.1:{seed_port}',
                           'enable_dht': False, 'enable_lsd': False,
                           'enable_upnp': False, 'enable_natpmp': False})
        sp = lt.add_torrent_params()
        sp.ti = ti
        sp.save_path = seed_dir
        sp.flags = lt.torrent_flags.seed_mode
        sh = seed.add_torrent(sp)
        deadline = time.time() + 15
        while time.time() < deadline:
            seed.post_torrent_updates()
            if sh.status().is_seeding:
                break
            time.sleep(0.2)
        self.assertTrue(sh.status().is_seeding)

        task1 = task2 = None
        try:
            # 第一階段：完整下載
            task1 = BTTask(torrent_file_path, dl_dir, proxies=[None, None])
            _quiet_task_sessions(task1)
            self.assertTrue(task1.start())
            task1._lines[0].handle.connect_peer(('127.0.0.1', seed_port))
            task1._lines[1].handle.connect_peer(('127.0.0.1', seed_port))
            deadline = time.time() + self.TIMEOUT
            while time.time() < deadline:
                if task1.is_completed():
                    break
                time.sleep(0.3)
            self.assertTrue(task1.is_completed())

            # 故意損壞第 4 個 piece 的其中一個位元組（該 piece 的 hash 必失敗）
            out_path = os.path.join(dl_dir, name)
            with open(out_path, 'r+b') as f:
                f.seek(4 * 32 * 1024 + 7)
                orig = f.read(1)
                f.seek(4 * 32 * 1024 + 7)
                f.write(bytes([orig[0] ^ 0xFF]))

            # 第二階段：重啟任務（seeder 仍在），應揪出壞 piece 並補下修復
            task2 = BTTask(torrent_file_path, dl_dir, proxies=[None, None])
            _quiet_task_sessions(task2)
            self.assertTrue(task2.start())
            task2._lines[0].handle.connect_peer(('127.0.0.1', seed_port))
            task2._lines[1].handle.connect_peer(('127.0.0.1', seed_port))
            deadline = time.time() + self.TIMEOUT
            while time.time() < deadline:
                if task2.is_completed():
                    break
                time.sleep(0.3)
            self.assertTrue(task2.is_completed(), '損壞的 piece 未被補下修復')
            with open(out_path, 'rb') as f:
                self.assertEqual(f.read(), data)
        finally:
            if task1 is not None:
                task1.cancel()
            if task2 is not None:
                task2.cancel()
            seed.remove_torrent(sh)


class TestMagnetMultiLine(unittest.TestCase):
    """magnet 多線路：metadata 延遲扇出與續傳接續。"""

    TIMEOUT = 60

    @staticmethod
    def _build_seeder(tmp, name, data, piece_len, port):
        seed_dir = os.path.join(tmp, 'seed')
        os.makedirs(seed_dir, exist_ok=True)
        with open(os.path.join(seed_dir, name), 'wb') as f:
            f.write(data)
        ti, _info = make_ti(piece_len, name, data)
        seed = lt.session({'listen_interfaces': f'127.0.0.1:{port}',
                           'enable_dht': False, 'enable_lsd': False,
                           'enable_upnp': False, 'enable_natpmp': False})
        sp = lt.add_torrent_params()
        sp.ti = ti
        sp.save_path = seed_dir
        sp.flags = lt.torrent_flags.seed_mode
        sh = seed.add_torrent(sp)
        return seed, sh, ti

    @staticmethod
    def _wait_seeding(seed, sh):
        deadline = time.time() + 15
        while time.time() < deadline:
            seed.post_torrent_updates()
            if sh.status().is_seeding:
                return
            time.sleep(0.2)
        raise AssertionError('seeder 未進入 seeding')

    def test_magnet_multiline_download(self):
        tmp = tempfile.mkdtemp()
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(dl_dir, exist_ok=True)
        name = 'payload.bin'
        data = os.urandom(256 * 1024)  # 32 KiB piece -> 8 pieces，2 session 各 4 pieces
        seed_port = _alloc_seed_port()
        seed, sh, ti = self._build_seeder(tmp, name, data, 32 * 1024, seed_port)
        self._wait_seeding(seed, sh)

        magnet = f"magnet:?xt=urn:btih:{str(ti.info_hashes().v1)}&dn={name}"
        task = None
        try:
            task = BTTask(magnet, dl_dir, proxies=[None, None])
            _quiet_task_sessions(task)
            self.assertTrue(task.start())
            self.assertEqual(task.kind, 'magnet')
            task._lines[0].handle.connect_peer(('127.0.0.1', seed_port))

            # 扇出發生於 metadata 到達後（非同步）；loopback 下載極快，_active_lines
            # 可能在單次輪詢內就因完成清理而歸零，故以穩定訊號 _fanned_out 判斷。
            # 每輪讓所有已建立 handle 的線路連上 seeder，涵蓋 handle 尚未建立的瞬間。
            deadline = time.time() + self.TIMEOUT
            while time.time() < deadline:
                if task.is_completed():
                    break
                for line in task._lines:
                    h = line.handle
                    if h is not None and h.is_valid():
                        try:
                            h.connect_peer(('127.0.0.1', seed_port))
                        except Exception:
                            pass
                time.sleep(0.2)

            self.assertTrue(task._fanned_out, 'magnet 未扇出多線路')
            self.assertTrue(task.is_completed(), 'magnet 多線下載未在時限內完成')
            out = os.path.join(dl_dir, name)
            self.assertTrue(os.path.isfile(out))
            with open(out, 'rb') as f:
                self.assertEqual(f.read(), data)
        finally:
            if task is not None:
                task.cancel()
            seed.remove_torrent(sh)

    def test_magnet_multiline_resume(self):
        """檔案已完整在磁碟時，以 magnet 續傳：metadata 到達後全量分片、無 have_pieces，
        靠 libtorrent 的 checking_files 驗證既有資料（不需重下），最後正常完成。"""
        tmp = tempfile.mkdtemp()
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(dl_dir, exist_ok=True)
        name = 'payload.bin'
        data = os.urandom(256 * 1024)
        seed_port = _alloc_seed_port()
        seed, sh, ti = self._build_seeder(tmp, name, data, 32 * 1024, seed_port)
        self._wait_seeding(seed, sh)

        magnet = f"magnet:?xt=urn:btih:{str(ti.info_hashes().v1)}&dn={name}"

        # 模擬前次下載：資料檔已完整落在磁碟
        with open(os.path.join(dl_dir, name), 'wb') as f:
            f.write(data)

        task = None
        try:
            task = BTTask(magnet, dl_dir, proxies=[None, None])
            _quiet_task_sessions(task)
            self.assertTrue(task.start())
            task._lines[0].handle.connect_peer(('127.0.0.1', seed_port))

            # metadata 到達後 _fan_out 做全量分片（所有 piece 都有 owner，無 -1 孤兒）
            deadline = time.time() + 20
            while time.time() < deadline:
                if task._fanned_out:
                    break
                time.sleep(0.2)
            self.assertTrue(task._fanned_out, 'magnet 未扇出多線路')
            self.assertEqual(len(task._piece_owner), ti.num_pieces())
            self.assertNotIn(-1, task._piece_owner)

            # 續傳後靠本地校驗即可完成，資料檔內容不變
            deadline = time.time() + self.TIMEOUT
            while time.time() < deadline:
                if task.is_completed():
                    break
                time.sleep(0.3)
            self.assertTrue(task.is_completed(), 'magnet 續傳未在時限內完成')
            with open(os.path.join(dl_dir, name), 'rb') as f:
                self.assertEqual(f.read(), data)
        finally:
            if task is not None:
                task.cancel()
            seed.remove_torrent(sh)


if __name__ == '__main__':
    unittest.main()
