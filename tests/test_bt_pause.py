"""BT 暫停是否真的停下來（回歸測試：按下暫停後仍偷下載）。

libtorrent 的 auto_managed 是「開始/暫停交給 session 佇列管理器決定」，官方文件
明說要手動暫停就得先清掉它。過去 pause() 只呼叫 handle.pause()，旗標還在，於是
佇列管理器在 checking_files 跑完時把種子重新啟動——畫面顯示「已暫停」，資料卻
繼續進來（實測 6 秒多收 4.5 MB payload、落盤多 6.5 MB）。

以本地 seeder 做種，不需要對外網路。
"""

import os
import sys
import time
import hashlib
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass

import libtorrent as lt

from bt_downloader import BTTask

PIECE = 512 * 1024
NAME = 'pkg'
FILES = [('pkg/a.mkv', 4 * 1024 * 1024),
         ('pkg/b.mkv', 2 * 1024 * 1024),
         ('pkg/c.txt', 256 * 1024)]

_QUIET = {'enable_dht': False, 'enable_lsd': False,
          'enable_upnp': False, 'enable_natpmp': False}

# 暫停後仍容許的在途資料量：暫停的瞬間可能有幾個 block 已經在路上/待寫盤。
SLACK = 256 * 1024


def _build(tmp):
    seed_dir = os.path.join(tmp, 'seed')
    os.makedirs(os.path.join(seed_dir, NAME))
    payload = {}
    files = []
    for rel, size in FILES:
        data = os.urandom(size)
        payload[rel] = data
        path = os.path.join(seed_dir, *rel.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        files.append((rel.split('/', 1)[1], size))
    blob = b''.join(payload[rel] for rel, _ in FILES)
    num = (len(blob) + PIECE - 1) // PIECE
    pieces = b''.join(hashlib.sha1(blob[p * PIECE:(p + 1) * PIECE]).digest()
                      for p in range(num))
    info = {
        b'name': NAME.encode(),
        b'piece length': PIECE,
        b'pieces': pieces,
        b'files': [{b'length': size, b'path': [p.encode() for p in rel.split('/')]}
                   for rel, size in files],
    }
    torrent_path = os.path.join(tmp, 'pkg.torrent')
    with open(torrent_path, 'wb') as f:
        f.write(lt.bencode({b'info': info}))
    return seed_dir, torrent_path, lt.torrent_info(lt.bencode({b'info': info}))


class TestBTPause(unittest.TestCase):
    TIMEOUT = 60

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.seed_dir, self.torrent_path, self.ti = _build(self.tmp)
        self.seed_port = 23400 + (os.getpid() % 100) * 2
        self.seed = lt.session(dict(_QUIET,
                                    listen_interfaces='127.0.0.1:%d' % self.seed_port,
                                    upload_rate_limit=4 * 1024 * 1024))
        params = lt.add_torrent_params()
        params.ti = self.ti
        params.save_path = self.seed_dir
        params.flags = lt.torrent_flags.seed_mode
        self.sh = self.seed.add_torrent(params)
        self.task = None
        deadline = time.time() + 15
        while time.time() < deadline:
            self.seed.post_torrent_updates()
            if self.sh.status().is_seeding:
                break
            time.sleep(0.2)
        self.assertTrue(self.sh.status().is_seeding, 'seeder 未進入 seeding')

    def tearDown(self):
        if self.task is not None:
            self.task.cancel()
        self.seed.remove_torrent(self.sh)

    # -- 工具 -------------------------------------------------------------- #
    def _start(self, dl_dir, prep=None):
        if prep:
            prep()
        self.task = BTTask(self.torrent_path, dl_dir)
        for line in self.task._lines:
            line.session.apply_settings(_QUIET)
        self.assertTrue(self.task.start(), self.task.error_message)
        self.task.set_download_limit(1024 * 1024)
        for line in self.task._active_lines:
            line.handle.connect_peer(('127.0.0.1', self.seed_port))

    def _payload(self):
        total = 0
        for line in self.task._active_lines:
            h = line.handle
            if h is None or not h.is_valid():
                continue
            total += h.status().total_payload_download
        return total

    def _disk(self):
        total = 0
        for root, _dirs, names in os.walk(self.task.save_dir):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total

    def _assert_paused(self):
        for line in self.task._active_lines:
            h = line.handle
            if h is None or not h.is_valid():
                continue
            self.assertTrue(h.status().paused,
                            '線路 %s 的 handle 沒有真的暫停' % line.key)

    # -- 案例 -------------------------------------------------------------- #
    def test_pause_stops_download(self):
        dl_dir = os.path.join(self.tmp, 'dl')
        os.makedirs(dl_dir)
        self._start(dl_dir)

        deadline = time.time() + self.TIMEOUT
        while time.time() < deadline and self.task._total_done < self.task._total_size * 0.15:
            time.sleep(0.1)
        self.assertTrue(self.task.pause())

        time.sleep(0.5)
        payload = self._payload()
        disk = self._disk()
        time.sleep(4)
        self._assert_paused()
        self.assertLess(self._payload() - payload, SLACK,
                        '暫停後仍在接收資料（%d 位元組）' % (self._payload() - payload))
        self.assertLess(self._disk() - disk, SLACK,
                        '暫停後檔案仍在長大（%d 位元組）' % (self._disk() - disk))

        # 恢復後要能繼續
        before = self._payload()
        self.assertTrue(self.task.resume())
        time.sleep(4)
        self.assertGreater(self._payload() - before, 256 * 1024,
                           '恢復後沒有繼續下載')

    def test_pause_during_checking_stays_paused(self):
        """既有資料 → 一啟動就 checking_files；此時暫停，校驗結束後不得自己跑起來。"""
        dl_dir = os.path.join(self.tmp, 'dl')
        os.makedirs(os.path.join(dl_dir, NAME))

        def prep():
            for rel, _size in FILES:
                src = os.path.join(self.seed_dir, *rel.split('/'))
                dst = os.path.join(dl_dir, *rel.split('/'))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src, 'rb') as f:
                    data = f.read()
                with open(dst, 'wb') as f:
                    f.write(data[:len(data) // 2])

        self._start(dl_dir, prep=prep)
        # 一起步就暫停（此時多半還在校驗既有資料）
        self.assertTrue(self.task.pause())

        time.sleep(1.0)
        payload = self._payload()
        disk = self._disk()
        time.sleep(5)
        self._assert_paused()
        self.assertLess(self._payload() - payload, SLACK,
                        '校驗結束後自己恢復下載（+%d 位元組）' % (self._payload() - payload))
        self.assertLess(self._disk() - disk, SLACK,
                        '校驗結束後檔案仍長大（+%d 位元組）' % (self._disk() - disk))


if __name__ == '__main__':
    unittest.main(verbosity=2)
