"""POC：驗證兩個 libtorrent session 能否對同一 save_path、同一稀疏檔，
各自下載不重疊的 piece，而不因 Windows 檔案鎖互相卡住。

這是「BT 多線路聚合」設計文件（docs/bt-multi-line-design.md）中 R1 風險的實測。

用法：
    .venv/Scripts/python.exe tests/poc_bt_multisession.py

成功判準（三者皆須成立）：
    1) 兩個 session 各自完成自己分配的 piece 範圍（total_wanted_done >= total_wanted）
    2) 全程沒有 file_error_alert（檔案開啟/寫入失敗，疑似檔案鎖）
    3) 合併後的最終檔與原始資料逐位元組一致

回傳碼：
    0 = PASS
    1 = seeder 未就緒
    2 = 偵測到檔案錯誤（檔案鎖）
    3 = 有 session 未在時限內完成
    4 = 合併檔不存在
    5 = 合併檔內容不一致
"""

import os
import sys
import time
import hashlib
import tempfile

import libtorrent as lt

PIECE = 64 * 1024
DATA_LEN = 512 * 1024  # 8 pieces，每 session 分 4 pieces
TIMEOUT = 60


def make_torrent_info(name, data, piece_len):
    total = len(data)
    num = (total + piece_len - 1) // piece_len
    ph = b''.join(
        hashlib.sha1(data[p * piece_len:(p + 1) * piece_len]).digest()
        for p in range(num))
    info = {b'name': name.encode(), b'length': total,
            b'piece length': piece_len, b'pieces': ph}
    return lt.torrent_info(lt.bencode({b'info': info}))


def drain_file_errors(session, errors):
    for a in session.pop_alerts():
        if isinstance(a, lt.file_error_alert):
            try:
                err = a.error()
                msg = err.message() if hasattr(err, 'message') else str(err)
            except Exception:
                msg = str(a)
            try:
                f = a.file()
            except Exception:
                f = '?'
            errors.append(f'{msg} (file={f})')


def session_done(h):
    st = h.status()
    return st.total_wanted > 0 and st.total_wanted_done >= st.total_wanted


def main():
    tmp = tempfile.mkdtemp(prefix='poc_bt_ms_')
    seed_dir = os.path.join(tmp, 'seed')
    dl_dir = os.path.join(tmp, 'dl')
    os.makedirs(seed_dir)
    os.makedirs(dl_dir)

    name = 'payload.bin'
    data = os.urandom(DATA_LEN)
    with open(os.path.join(seed_dir, name), 'wb') as f:
        f.write(data)

    ti = make_torrent_info(name, data, PIECE)
    num_pieces = ti.num_pieces()
    half = num_pieces // 2
    print(f'pieces={num_pieces}, half={half}, data={DATA_LEN} bytes')

    # --- seeder ---
    seed_port = 23000 + (os.getpid() % 500)
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
    if not sh.status().is_seeding:
        print('FAIL: seeder 未進入 seeding')
        return 1
    print(f'seeder ready on 127.0.0.1:{seed_port}')

    # --- 兩個下載 session，共用 dl_dir，piece 分片不重疊 ---
    def make_downloader(lo, hi):
        s = lt.session({'listen_interfaces': '0.0.0.0:0',
                        'enable_dht': False, 'enable_lsd': False,
                        'enable_upnp': False, 'enable_natpmp': False})
        p = lt.add_torrent_params()
        p.ti = ti
        p.save_path = dl_dir          # 關鍵：兩個 session 指向同一個目錄
        p.storage_mode = lt.storage_mode_t.storage_mode_sparse
        p.flags = lt.torrent_flags.auto_managed
        # 關鍵：只下 [lo, hi) 範圍的 piece，其餘 priority=0（不下載）
        p.piece_priorities = [1 if lo <= i < hi else 0 for i in range(num_pieces)]
        h = s.add_torrent(p)
        h.connect_peer(('127.0.0.1', seed_port))
        return s, h

    s1, h1 = make_downloader(0, half)
    s2, h2 = make_downloader(half, num_pieces)

    errors = []
    start = time.time()
    try:
        while time.time() - start < TIMEOUT:
            s1.post_torrent_updates()
            s2.post_torrent_updates()
            drain_file_errors(s1, errors)
            drain_file_errors(s2, errors)
            if session_done(h1) and session_done(h2):
                break
            time.sleep(0.3)

        st1 = h1.status()
        st2 = h2.status()
        print(f's1 wanted_done={st1.total_wanted_done}/{st1.total_wanted} state={st1.state}')
        print(f's2 wanted_done={st2.total_wanted_done}/{st2.total_wanted} state={st2.state}')

        if errors:
            print('FAIL: 偵測到檔案錯誤（疑似 Windows 檔案鎖）:')
            for e in errors:
                print('  -', e)
            return 2

        if not (session_done(h1) and session_done(h2)):
            print('FAIL: 有 session 未在時限內完成自己分配的 piece')
            return 3

        out = os.path.join(dl_dir, name)
        if not os.path.isfile(out):
            print('FAIL: 合併檔不存在')
            return 4
        with open(out, 'rb') as f:
            got = f.read()
        if got == data:
            print('PASS: 兩個 session 成功併寫同一稀疏檔，合併後內容逐位元組一致')
            return 0
        print(f'FAIL: 合併檔內容不一致（len {len(got)} vs {len(data)}）')
        return 5
    finally:
        for s, h in ((s1, h1), (s2, h2)):
            try:
                s.remove_torrent(h)
            except Exception:
                pass
        try:
            seed.remove_torrent(sh)
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
