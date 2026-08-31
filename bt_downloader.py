"""BitTorrent 下載支援：以 libtorrent 為核心引擎，支援 magnet 連結與 .torrent 檔。

架構（單一 Torrent 單一 Session 模型）：
- 每個 BTTask 擁有獨立的 libtorrent.session，可綁定單一線路（PPPoE 直連或指定的 SOCKS5 代理）。
- 由 libtorrent 原生 swarm 與 piece picker 進行全域調度、校驗與落盤。
- 對外偽裝成 qBittorrent 4.6.0（user_agent 與 peer_fingerprint），避免自製客戶端被 Tracker 白名單拒收。
- 下載完成後可依設定繼續做種一段時間；PT（private）種子由 libtorrent 依 private=1
  自動關閉 DHT / PEX / LSD，且本架構單一 session 單一線路，天然不拆線分流。
"""

import os
import json
import time
import threading
import logging
from urllib.parse import urlparse, parse_qs, unquote

import libtorrent as lt

logger = logging.getLogger('bt_downloader')

# 客戶端偽裝：以 qBittorrent 4.6.0 的身份向 Tracker / Peers 宣告。
# peer_id 前 8 bytes（-qB4600-）由 generate_fingerprint 產生，後 12 bytes 由 libtorrent 補上。
BT_USER_AGENT = 'qBittorrent/4.6.0'
BT_PEER_FINGERPRINT = lt.generate_fingerprint('qB', 4, 6, 0, 0)


def is_magnet(source):
    return isinstance(source, str) and source.lower().startswith('magnet:')


def source_kind(source):
    """判別 BT 來源型態：'magnet' 或 'torrent'（.torrent 檔路徑），其餘回傳 None。"""
    if is_magnet(source):
        return 'magnet'
    if (isinstance(source, str) and source.lower().endswith('.torrent')
            and os.path.isfile(source)):
        return 'torrent'
    return None


def magnet_display_name(source):
    """從 magnet URI 的 dn= 參數取顯示名稱（無則回傳空字串）。"""
    try:
        parsed = urlparse(source)
        qs = parse_qs(parsed.query)
        for n in qs.get('dn') or []:
            if n:
                return unquote(n)
    except Exception:
        pass
    return ''


def _hash_hex(info_hashes):
    """把 info_hash_t 轉成 hex 字串（優先 v1 sha1，否則 v2）。"""
    try:
        v1 = getattr(info_hashes, 'v1', None)
        if v1:
            return str(v1)
    except Exception:
        pass
    try:
        v2 = getattr(info_hashes, 'v2', None)
        if v2:
            return str(v2)
    except Exception:
        pass
    return 'nohash'


def _bit(bitfield, idx):
    return bool((bitfield[idx >> 3] >> (idx & 7)) & 1)


def _downsample_blocks(pieces, max_blocks=1000):
    """把區塊完成度降採樣到最多 max_blocks 個區間，降低 UI 重繪成本。"""
    n = len(pieces)
    if n <= max_blocks:
        return [{'frac': f, 'active': False} for f in pieces]
    out = []
    per = n / max_blocks
    for i in range(max_blocks):
        lo = int(i * per)
        hi = int((i + 1) * per)
        seg = pieces[lo:hi] or [0.0]
        out.append({'frac': sum(seg) / len(seg), 'active': False})
    return out


def partition_ranges(num_pieces, num_sessions):
    """區間輔助函式（保留相容性）。"""
    if num_sessions <= 0 or num_pieces <= 0:
        return []
    per = num_pieces // num_sessions
    rem = num_pieces % num_sessions
    ranges = []
    start = 0
    for i in range(num_sessions):
        extra = 1 if i < rem else 0
        end = start + per + extra
        ranges.append((start, end))
        start = end
    return ranges


def torrent_file_tree(ti):
    """解析 torrent_info 的檔案結構，回傳 [(relative_path, size), ...]，依檔案 index 排序。

    relative_path 以 '/' 分隔（如 'dir/sub/file.bin'），供 UI 建檔案樹與選擇性下載使用。
    """
    fs = ti.files()
    files = []
    for i in range(fs.num_files()):
        try:
            path = fs.file_path(i)
        except TypeError:
            path = fs.file_path(i, '')
        # 統一以 '/' 分隔（Windows 上 file_path 會回反斜線）
        files.append((path.replace('\\', '/'), fs.file_size(i)))
    return files


class LineSession:
    """包裝一個 libtorrent.session，綁定單一線路（直連或 SOCKS5 代理）。"""

    def __init__(self, key='direct', proxy=None, upload_rate_limit=0):
        self.key = key
        self.proxy = proxy
        self.upload_rate_limit = max(0, int(upload_rate_limit or 0))
        self.session = self._make_session()
        self.handle = None

    def _make_session(self):
        settings = {
            'listen_interfaces': '0.0.0.0:0',  # 動態綁定可用埠號，避免多實例搶 6881
            'enable_dht': True,
            'enable_lsd': True,
            'enable_upnp': True,
            'enable_natpmp': True,
            # 客戶端偽裝：對外宣告為 qBittorrent 4.6.0
            'user_agent': BT_USER_AGENT,
            'peer_fingerprint': BT_PEER_FINGERPRINT,
        }
        if self.upload_rate_limit > 0:
            # 上傳限速（bytes/sec），0 表示不限。PT 用戶可設非零上限以控制上傳頻寬。
            settings['upload_rate_limit'] = self.upload_rate_limit
        if self.proxy:
            settings['proxy_type'] = int(lt.proxy_type_t.socks5)
            settings['proxy_hostname'] = self.proxy['host']
            settings['proxy_port'] = int(self.proxy['port'])
            if self.proxy.get('username'):
                settings['proxy_username'] = self.proxy['username']
            if self.proxy.get('password'):
                settings['proxy_password'] = self.proxy['password']
            # SOCKS5 通常不轉發 UDP，DHT 僅對直連有效
            settings['enable_dht'] = False
        return lt.session(settings)

    def add_torrent(self, params):
        self.handle = self.session.add_torrent(params)
        return self.handle

    def remove(self):
        if self.handle is not None:
            try:
                self.session.remove_torrent(self.handle)
            except Exception:
                pass
            self.handle = None


class BTTask:
    """BT 下載任務：單一 session 獨立下載，生命週期對齊 DownloadTask。"""

    def __init__(self, source, save_dir, filename=None, proxy=None, proxies=None,
                 selected_files=None, seed_hours=0, upload_rate_limit=0):
        self.source = source
        self.url = source
        self.kind = source_kind(source)
        self.save_dir = save_dir
        self.seed_hours = max(0.0, float(seed_hours or 0))
        self.upload_rate_limit = max(0, int(upload_rate_limit or 0))

        # 支援單一 proxy 參數，相容傳入 proxies 列表（取第一筆）
        if proxy is not None:
            self.proxy = proxy
        elif proxies and len(proxies) > 0:
            self.proxy = proxies[0]
        else:
            self.proxy = None

        # 選擇性下載：None 表示下載整包；list 表示只下載指定檔案 index。
        self.selected_files = selected_files

        self.filename = filename
        if not self.filename:
            if self.kind == 'magnet':
                self.filename = magnet_display_name(source) or '磁力連結下載'
            else:
                self.filename = os.path.basename(source)
                if self.filename.lower().endswith('.torrent'):
                    self.filename = self.filename[:-len('.torrent')]

        self.filepath = os.path.join(save_dir, self.filename)
        self.status = 'initialized'
        self.error_message = ''
        self.start_time = None
        self.end_time = None
        self.task_id = None

        line_key = 'direct' if self.proxy is None else f"proxy:{self.proxy.get('host')}:{self.proxy.get('port')}"
        self._line_key = line_key
        self._line = LineSession(line_key, self.proxy, upload_rate_limit=self.upload_rate_limit)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._coordinator = None

        self._ti = None
        self._info_hash_hex = None
        self._work_root = None
        self._total_size = 0
        self._total_done = 0
        self._last_speed = 0.0
        self._last_upload = 0.0
        self._pieces = []

        # PT（private）種子標記：載入 metadata 後於 start()/協調迴圈填入。
        self.is_private = False
        # 做種狀態：下載完成時刻與做種截止時刻（不做種時 seed_hours=0）。
        self._download_completed_at = None
        self._seed_deadline = None
        # resume data 定期保存時間戳（供重啟續傳，跳過重複校驗）。
        self._last_resume_save = 0.0

    @property
    def proxies(self):
        return [self.proxy] if self.proxy else []

    @property
    def total_size(self):
        return self._total_size

    @property
    def is_private_torrent(self):
        return self.is_private

    # ------------------------------------------------------------------ #
    # 狀態持久化
    # ------------------------------------------------------------------ #
    def _state_path(self):
        if not self._work_root:
            return None
        return os.path.join(self._work_root, 'task.json')

    def _save_state(self):
        try:
            if not self._work_root:
                return
            os.makedirs(self._work_root, exist_ok=True)
            state = {
                'source': self.source,
                'save_dir': self.save_dir,
                'filename': self.filename,
                'kind': self.kind,
                'proxy': self.proxy,
                'proxies': [self.proxy] if self.proxy else [],
                'selected_files': self.selected_files,
                'seed_hours': self.seed_hours,
                'upload_rate_limit': self.upload_rate_limit,
            }
            tmp = self._state_path() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self._state_path())
        except Exception as e:
            logger.warning('BT 狀態保存失敗: %s', e)

    def _cleanup_work(self):
        if not self._work_root:
            return
        try:
            import shutil
            shutil.rmtree(self._work_root, ignore_errors=True)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # resume data（重啟續傳：跳過重複校驗，進度不再從 0 開始）
    # ------------------------------------------------------------------ #
    def _resume_path(self):
        return os.path.join(self._work_root, 'resume.bin') if self._work_root else None

    def _persist_resume_data(self, params):
        """把 libtorrent resume data（piece 位元圖 + info_dict）寫入磁碟。"""
        if not self._work_root:
            return
        try:
            data = lt.write_resume_data_buf(params)
            path = self._resume_path()
            tmp = path + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as e:
            logger.debug('保存 resume data 失敗: %s', e)

    def _try_load_resume(self, params):
        """若存在 resume data，改以 resume 參數加入，跳過重複校驗。"""
        if not self._work_root:
            return params
        path = self._resume_path()
        if not os.path.isfile(path):
            return params
        try:
            with open(path, 'rb') as f:
                data = f.read()
            rp = lt.read_resume_data(data)
            # resume data 含 info_dict 時會自動帶 ti；否則回退到已解析的 ti
            ti = rp.ti if rp.ti is not None else getattr(params, 'ti', None)
            if ti is None:
                return params
            rp.ti = ti
            rp.save_path = self.save_dir
            rp.storage_mode = lt.storage_mode_t.storage_mode_sparse
            rp.flags = lt.torrent_flags.auto_managed
            self._ti = ti
            self._total_size = self._wanted_size(ti)
            self.filename = ti.name() or self.filename
            self.filepath = os.path.join(self.save_dir, self.filename)
            self._info_hash_hex = _hash_hex(ti.info_hashes())
            self._mark_private(ti)
            logger.info('BT 以 resume data 續傳，跳過重複校驗: %s', self.filename)
            return rp
        except Exception as e:
            logger.warning('載入 resume data 失敗，改為全新加入: %s', e)
            return params

    def _drain_alerts(self):
        """處理 libtorrent alerts；目前只處理 resume data 保存結果。"""
        try:
            for a in self._line.session.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    self._persist_resume_data(a.params)
        except Exception:
            pass

    def _wait_resume_alert(self, timeout=3.0):
        """同步等待並保存 resume data（暫停/關閉前確保續傳資料落盤）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for a in self._line.session.pop_alerts():
                    if isinstance(a, lt.save_resume_data_alert):
                        self._persist_resume_data(a.params)
                        return
            except Exception:
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def _wanted_size(self, ti):
        """選擇性下載時計算實際要下載的總位元組（未指定則回整包大小）。"""
        if not self.selected_files:
            return ti.total_size()
        fs = ti.files()
        n = fs.num_files()
        total = sum(fs.file_size(i) for i in self.selected_files if 0 <= i < n)
        return total if total > 0 else ti.total_size()

    def _apply_file_priorities(self):
        """依 selected_files 設定 libtorrent 檔案優先權（0 = 不下載）。"""
        if not self.selected_files or self._line.handle is None or self._ti is None:
            return
        fs = self._ti.files()
        n = fs.num_files()
        wanted = set(self.selected_files)
        for i in range(n):
            prio = 1 if i in wanted else 0
            try:
                self._line.handle.file_priority(i, prio)
            except Exception as e:
                logger.warning('設定檔案優先權 %s 失敗: %s', i, e)

    def _mark_private(self, ti):
        """偵測 PT（private）種子並記錄。

        libtorrent 看到 private=1 會自動停用該種子的 DHT / PEX / LSD，此處僅標記供
        上層與日誌辨識。本架構單一 session 單一線路，天然不拆線分流，符合 PT 規範。
        """
        try:
            self.is_private = bool(ti.priv())
            if self.is_private:
                logger.info('偵測到 PT 種子（private=1），維持單線路並由 libtorrent 停用 DHT/PEX: %s',
                            self.filename)
        except Exception:
            self.is_private = False

    def start(self):
        if self.kind is None:
            self.status = 'error'
            self.error_message = '不是有效的 BT 來源（magnet 或 .torrent）'
            return False

        self.status = 'downloading'
        self.error_message = ''
        self.start_time = time.time()
        self._stop.clear()

        os.makedirs(self.save_dir, exist_ok=True)

        params = lt.add_torrent_params()
        params.save_path = self.save_dir
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        params.flags = lt.torrent_flags.auto_managed

        if self.kind == 'torrent':
            try:
                ti = lt.torrent_info(self.source)
                self._ti = ti
                params.ti = ti
                self._total_size = self._wanted_size(ti)
                self.filename = ti.name() or self.filename
                self.filepath = os.path.join(self.save_dir, self.filename)
                self._info_hash_hex = _hash_hex(ti.info_hashes())
                self._mark_private(ti)
            except Exception as e:
                self.status = 'error'
                self.error_message = f'解析 .torrent 失敗: {e}'
                return False
        else:
            try:
                magnet_p = lt.parse_magnet_uri(self.source)
                params = magnet_p
                params.save_path = self.save_dir
                params.storage_mode = lt.storage_mode_t.storage_mode_sparse
                params.flags = lt.torrent_flags.auto_managed
                self._info_hash_hex = _hash_hex(params.info_hashes)
            except Exception as e:
                self.status = 'error'
                self.error_message = f'解析 magnet 失敗: {e}'
                return False

        if self._info_hash_hex:
            self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)

        # 優先以 resume data 續傳，跳過重複校驗（重啟後進度不再從 0 開始）
        params = self._try_load_resume(params)

        if self._info_hash_hex:
            self._save_state()

        try:
            self._line.add_torrent(params)
        except Exception as e:
            self.status = 'error'
            self.error_message = f'啟動 BT 下載失敗: {e}'
            return False

        self._apply_file_priorities()

        self._coordinator = threading.Thread(target=self._coordinator_loop, daemon=True)
        self._coordinator.start()
        return True

    def pause(self):
        if self.status not in ('downloading', 'seeding'):
            return False
        self.status = 'paused'
        if self._line.handle is not None:
            h = self._line.handle
            try:
                h.save_resume_data(
                    lt.save_resume_flags_t.flush_disk_cache | lt.save_resume_flags_t.save_info_dict)
            except Exception:
                pass
            try:
                h.pause()
            except Exception as e:
                logger.warning('BT pause 失敗: %s', e)
            # 同步等待 resume data 落盤，確保關閉程式前已保存續傳資料
            self._wait_resume_alert(timeout=3.0)
        self._save_state()
        return True

    def resume(self):
        if self.status not in ('paused', 'initialized'):
            return False
        if self._line.handle is None:
            return self.start()
        # 曾在做種階段被暫停則恢復為做種，否則回到下載中
        self.status = 'seeding' if self._download_completed_at is not None else 'downloading'
        try:
            self._line.handle.resume()
        except Exception as e:
            logger.warning('BT resume 失敗: %s', e)
        return True

    def retry(self):
        if self.status not in ('error', 'paused'):
            return False
        self.error_message = ''
        self.status = 'initialized'
        return self.start()

    def cancel(self):
        self._stop.set()
        if self._coordinator is not None and self._coordinator is not threading.current_thread():
            self._coordinator.join(timeout=1.0)
        self._line.remove()
        self._cleanup_work()
        self.status = 'canceled'
        return True

    # ------------------------------------------------------------------ #
    # 協調迴圈
    # ------------------------------------------------------------------ #
    def _coordinator_loop(self):
        h = self._line.handle
        while not self._stop.is_set():
            try:
                self._line.session.post_torrent_updates()
                if h is None or not h.is_valid():
                    break

                # 定期保存 resume data（piece 位元圖 + info_dict），供重啟續傳
                now = time.time()
                if now - self._last_resume_save >= 10.0:
                    self._last_resume_save = now
                    try:
                        h.save_resume_data(
                            lt.save_resume_flags_t.flush_disk_cache | lt.save_resume_flags_t.save_info_dict)
                    except Exception:
                        pass
                self._drain_alerts()

                st = h.status()

                # 當 magnet 獲取到 metadata 時，更新檔案資訊
                if self._ti is None and getattr(st, 'has_metadata', False):
                    try:
                        ti = h.torrent_file()
                        if ti:
                            self._ti = ti
                            self._total_size = ti.total_size()
                            self.filename = ti.name() or self.filename
                            self.filepath = os.path.join(self.save_dir, self.filename)
                            self._mark_private(ti)
                            if not self._info_hash_hex:
                                self._info_hash_hex = _hash_hex(ti.info_hashes())
                                self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)
                            self._save_state()
                    except Exception as e:
                        logger.debug('讀取 metadata 失敗: %s', e)

                wanted = st.total_wanted if st.total_wanted > 0 else self._total_size
                done = st.total_wanted_done
                rate = st.download_rate

                bits = getattr(st, 'pieces', None)
                num_pieces = len(bits) * 8 if bits else (self._ti.num_pieces() if self._ti else 0)
                pieces_frac = []
                if bits and num_pieces > 0:
                    pieces_frac = [1.0 if _bit(bits, i) else 0.0 for i in range(num_pieces)]

                with self._lock:
                    if wanted > 0:
                        self._total_size = wanted
                    self._total_done = done
                    self._last_speed = rate
                    self._last_upload = getattr(st, 'upload_rate', 0)
                    if pieces_frac:
                        self._pieces = pieces_frac

                # 下載完成偵測（libtorrent 進入 seeding 或 finished 狀態，或 wanted_done 達標）
                is_seeding = bool(getattr(st, 'is_seeding', False))
                state_val = getattr(st, 'state', None)
                is_finished = (state_val == lt.torrent_status.finished or state_val == lt.torrent_status.seeding)
                download_done = is_seeding or is_finished or (wanted > 0 and done >= wanted)

                if download_done:
                    # 記錄首次完成時刻，依設定決定是否進入做種階段
                    if self._download_completed_at is None:
                        self._download_completed_at = time.time()
                        if self.seed_hours > 0:
                            self._seed_deadline = self._download_completed_at + self.seed_hours * 3600
                            self.status = 'seeding'
                            logger.info('BT 下載完成，進入做種 %s 小時: %s',
                                        self.seed_hours, self.filename)
                        else:
                            # 不做種（seed_hours=0），維持原有「下載完成即停」行為
                            self.end_time = time.time()
                            self.status = 'completed'
                            self._line.remove()
                            self._cleanup_work()
                            return

                    # 做種階段：到期才結束；期間持續上傳避免被判定為吸血鬼
                    if self._seed_deadline is not None and time.time() >= self._seed_deadline:
                        self.end_time = time.time()
                        self.status = 'completed'
                        self._line.remove()
                        self._cleanup_work()
                        return

            except Exception as e:
                if self.status in ('downloading', 'seeding'):
                    self.status = 'error'
                    self.error_message = str(e)
                break
            time.sleep(0.5)

    # ------------------------------------------------------------------ #
    # progress
    # ------------------------------------------------------------------ #
    def is_running(self):
        return self.status in ('downloading', 'seeding')

    def is_completed(self):
        return self.status == 'completed'

    def get_progress(self):
        with self._lock:
            total = self._total_size
            done = self._total_done
            pieces = list(self._pieces)
            speed = self._last_speed
            upload_speed = self._last_upload

        pct = (done / total * 100) if total > 0 else 0.0
        elapsed = 0
        if self.start_time:
            elapsed = (self.end_time or time.time()) - self.start_time

        line_key = self._line_key
        line_label = '直連' if self.proxy is None else f"{self.proxy.get('host')}:{self.proxy.get('port')}"
        line_bytes = {line_key: done}
        line_labels = {line_key: line_label}

        seeding_remaining = 0.0
        if self.status == 'seeding' and self._seed_deadline is not None:
            seeding_remaining = max(0.0, self._seed_deadline - time.time())

        return {
            'total_size': total,
            'downloaded_size': done,
            'percentage': pct,
            'speed': speed,
            'upload_speed': upload_speed,
            'status': self.status,
            'error_message': self.error_message,
            'elapsed_time': elapsed,
            'thread_count': 1,
            'block_count': len(pieces),
            'blocks': _downsample_blocks(pieces),
            'line_bytes': line_bytes,
            'line_labels': line_labels,
            'is_private': self.is_private,
            'seed_hours': self.seed_hours,
            'seeding_remaining': seeding_remaining,
        }
