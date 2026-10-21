# -*- coding: utf-8 -*-
"""BT 同名種子隔離測試：兩顆不同 info hash、同 name 的種子同時下載到同一目錄。

驗證重點：
1. 兩邊都完成、內容各自正確（不會互相覆寫成殘缺混血）
2. 第二顆被改成 "name (1)"，第一顆維持原名
3. 多檔種子（根目錄同名）同樣分家
4. 續傳（.bt_tmp 有紀錄）不會再多一個後綴

需要 libtorrent，直接執行：
    <venv>/python tests/test_bt_same_name.py
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

_MOD = os.environ.get('MSD_MODULE_DIR') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _MOD)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass

import libtorrent as lt
from bt_downloader import BTTask, renamed_files_for

PL = 16384


def _mk_single(name, data):
    ph = b''.join(hashlib.sha1(data[i:i + PL]).digest() for i in range(0, len(data), PL))
    return {b'name': name.encode(), b'length': len(data), b'piece length': PL, b'pieces': ph}


def _mk_multi(name, files):
    ph = b''.join(hashlib.sha1(d[i:i + PL]).digest()
                  for _, d in files for i in range(0, len(d), PL))
    return {b'name': name.encode(), b'piece length': PL, b'pieces': ph,
            b'files': [{b'length': len(d), b'path': p.encode().split(b'/')}
                       for p, d in files]}


def _write_torrent(info, path):
    with open(path, 'wb') as f:
        f.write(lt.bencode({b'info': info}))
    return path


class Seeder:
    def __init__(self, save_dir, info, port):
        self.s = lt.session({'listen_interfaces': '127.0.0.1:%d' % port,
                             'enable_dht': False, 'enable_lsd': False,
                             'enable_upnp': False, 'enable_natpmp': False})
        p = lt.add_torrent_params()
        p.ti = lt.torrent_info(lt.bencode({b'info': info}))
        p.save_path = save_dir
        p.flags = lt.torrent_flags.seed_mode
        self.h = self.s.add_torrent(p)
        self.port = port
        deadline = time.time() + 15
        while time.time() < deadline:
            self.s.post_torrent_updates()
            if self.h.status().is_seeding:
                break
            time.sleep(0.2)
        self.ready = self.h.status().is_seeding

    def close(self):
        try:
            self.s.remove_torrent(self.h)
        except Exception:
            pass


def _run(task, peer_port, timeout=40):
    task.start()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in task._lines:
            h = line.handle
            if h is not None and h.is_valid():
                try:
                    h.connect_peer(('127.0.0.1', peer_port))
                except Exception:
                    pass
        if task.is_completed():
            return True
        time.sleep(0.3)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix='msd_same_')
    failures = []
    try:
        dl_dir = os.path.join(tmp, 'dl')
        os.makedirs(dl_dir)

        # ---------------- 單檔同名 ----------------
        data_a = b'CONTENT-A-' * 3000
        data_b = b'CONTENT-B-' * 3000
        for tag, data in (('a', data_a), ('b', data_b)):
            d = os.path.join(tmp, 'seed_' + tag)
            os.makedirs(d)
            with open(os.path.join(d, 'same.bin'), 'wb') as f:
                f.write(data)
        info_a = _mk_single('same.bin', data_a)
        info_b = _mk_single('same.bin', data_b)
        t_a = _write_torrent(info_a, os.path.join(tmp, 'a.torrent'))
        t_b = _write_torrent(info_b, os.path.join(tmp, 'b.torrent'))

        sa = Seeder(os.path.join(tmp, 'seed_a'), info_a, 29301)
        sb = Seeder(os.path.join(tmp, 'seed_b'), info_b, 29302)
        print('seeder ready:', sa.ready, sb.ready)

        ta = BTTask(t_a, dl_dir, proxies=[None])
        tb = BTTask(t_b, dl_dir, proxies=[None])
        ok_a = _run(ta, 29301)
        ok_b = _run(tb, 29302)
        print('單檔完成:', ok_a, ok_b, '| root:', ta._root_name, '/', tb._root_name)

        pa = os.path.join(dl_dir, ta._root_name)
        pb = os.path.join(dl_dir, tb._root_name)
        got_a = open(pa, 'rb').read() if os.path.isfile(pa) else None
        got_b = open(pb, 'rb').read() if os.path.isfile(pb) else None
        if got_a != data_a:
            failures.append('單檔 A 內容錯誤（%s）' % ta._root_name)
        if got_b != data_b:
            failures.append('單檔 B 內容錯誤（%s）' % tb._root_name)
        if ta._root_name != 'same.bin':
            failures.append('第一顆不該改名，實際 %s' % ta._root_name)
        if tb._root_name != 'same (1).bin':
            failures.append('第二顆應改為 same (1).bin，實際 %s' % tb._root_name)
        ta.cancel()
        tb.cancel()
        sa.close()
        sb.close()

        # ---------------- 多檔同名 ----------------
        dl2 = os.path.join(tmp, 'dl2')
        os.makedirs(dl2)
        files_a = [('x.txt', b'X' * PL), ('y.txt', b'Y' * PL)]
        files_b = [('x.txt', b'x' * PL), ('y.txt', b'y' * PL)]
        for tag, files in (('a', files_a), ('b', files_b)):
            d = os.path.join(tmp, 'mseed_' + tag, 'multi')
            os.makedirs(d)
            for n, data in files:
                with open(os.path.join(d, n), 'wb') as f:
                    f.write(data)
        minfo_a = _mk_multi('multi', files_a)
        minfo_b = _mk_multi('multi', files_b)
        mt_a = _write_torrent(minfo_a, os.path.join(tmp, 'ma.torrent'))
        mt_b = _write_torrent(minfo_b, os.path.join(tmp, 'mb.torrent'))
        msa = Seeder(os.path.join(tmp, 'mseed_a'), minfo_a, 29303)
        msb = Seeder(os.path.join(tmp, 'mseed_b'), minfo_b, 29304)
        mta = BTTask(mt_a, dl2, proxies=[None])
        mtb = BTTask(mt_b, dl2, proxies=[None])
        mok_a = _run(mta, 29303)
        mok_b = _run(mtb, 29304)
        print('多檔完成:', mok_a, mok_b, '| root:', mta._root_name, '/', mtb._root_name)
        for tag, task, files in (('A', mta, files_a), ('B', mtb, files_b)):
            for n, data in files:
                p = os.path.join(dl2, task._root_name, n)
                got = open(p, 'rb').read() if os.path.isfile(p) else None
                if got != data:
                    failures.append('多檔 %s/%s 內容錯誤' % (tag, n))
        if mtb._root_name != 'multi (1)':
            failures.append('多檔第二顆應為 multi (1)，實際 %s' % mtb._root_name)
        mta.cancel()
        mtb.cancel()
        msa.close()
        msb.close()

        # ---------------- 續傳不重複加後綴 ----------------
        dl3 = os.path.join(tmp, 'dl3')
        os.makedirs(dl3)
        s3 = Seeder(os.path.join(tmp, 'seed_a'), info_a, 29305)
        t1 = BTTask(t_a, dl3, proxies=[None])
        r1 = _run(t1, 29305)
        root1 = t1._root_name
        t1.cancel()  # 已完成 → 不清檔
        t2 = BTTask(t_a, dl3, proxies=[None], root_name=root1)
        r2 = _run(t2, 29305)
        print('續傳:', r1, r2, '| root:', root1, '->', t2._root_name)
        if t2._root_name != root1:
            failures.append('續傳根名漂移：%s -> %s' % (root1, t2._root_name))
        t2.cancel()
        s3.close()

        # ---------------- magnet 取名與改名映射 ----------------
        # magnet 加入時沒有 info dict，無法預先用 params.renamed_files 指定路徑，
        # 只能等 metadata 到達後逐檔 rename_file。本地沒有 tracker，無法在此環境
        # 真實走完 metadata 交換，因此直接驗證「取名」與「改名映射」兩段純邏輯；
        # rename_file 本身在 torrent 分支已被同一套機制覆蓋（params.renamed_files）。
        ih_b = hashlib.sha1(lt.bencode({b'info': info_b})).hexdigest()
        magnet_b = 'magnet:?xt=urn:btih:%s' % ih_b
        ti_b = lt.torrent_info(lt.bencode({b'info': info_b}))
        tm = BTTask(magnet_b, dl3, proxies=[None])
        tm._info_hash_hex = ih_b
        tm._resuming = False
        root_m = tm._resolve_root_name(ti_b)
        mapping = renamed_files_for(ti_b, root_m)
        print('magnet 取名:', root_m, '| 映射:', mapping)
        if root_m != 'same (1).bin':
            failures.append('magnet 應改名為 same (1).bin，實際 %s' % root_m)
        if mapping != {0: 'same (1).bin'}:
            failures.append('magnet 改名映射錯誤: %s' % mapping)
        tm.cancel()
        # 多檔的映射要把根目錄換掉、子路徑保留
        ti_ma = lt.torrent_info(lt.bencode({b'info': minfo_a}))
        mmap = renamed_files_for(ti_ma, 'multi (1)')
        if mmap != {0: 'multi (1)/x.txt', 1: 'multi (1)/y.txt'}:
            failures.append('多檔改名映射錯誤: %s' % mmap)

        print()
        if failures:
            for f in failures:
                print('FAIL:', f)
            return 1
        print('全部通過')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
