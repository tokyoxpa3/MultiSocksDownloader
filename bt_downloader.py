"""BitTorrent 下載支援：以 libtorrent 為核心引擎，支援 magnet 連結與 .torrent 檔。

架構（單一 Torrent 多 Session 模型）：
- 一個 BTTask 可建立多個 libtorrent.session，每條線路（直連或 SOCKS5 代理）各綁定
  一個 session。公開種子時把 piece 空間切成不重疊區段，每個 session 只下自己那一段，
  達成多線路頻寬聚合；private（PT）種子強制單線路。
- 每個 session 以 `piece_priorities` 只啟用自己的 piece 範圍（其餘 priority=0 不下載），
  並共用同一個 save_path、`storage_mode_sparse`，各自寫不重疊位元組，最終自然拼成
  完整檔案。
- 對外偽裝成 qBittorrent 4.6.0（user_agent 與 peer_fingerprint），避免自製客戶端被
  Tracker 白名單拒收。
- 下載完成後可依設定繼續做種一段時間；PT（private）種子由 libtorrent 依 private=1
  自動關閉 DHT / PEX / LSD。

MVP 限制（詳見 docs/bt-multi-line-design.md）：
- 選擇性下載（selected_files）暫不與多線路併用，設了 selected_files 就退回單線路。
- 多線路暫不做 resume data 持久化（各 session 的 piece 位元圖合併留待後續）。
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
    """把 num_pieces 個 piece 切成 num_sessions 個不重疊連續區段 [(start, end), ...]。"""
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


def _line_key_for(proxy):
    return 'direct' if proxy is None else f"proxy:{proxy.get('host')}:{proxy.get('port')}"


def _line_label_for(proxy):
    return '直連' if proxy is None else f"{proxy.get('host')}:{proxy.get('port')}"


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
            # 強制所有流量（tracker + peer 連線）走代理，SOCKS5 不轉發 UDP、DHT 關閉
            settings['force_proxy'] = True
            settings['proxy_peer_connections'] = True
            settings['proxy_hostnames'] = True
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
    """BT 下載任務：一或多個 session 下載同一 torrent，生命週期對齊 DownloadTask。"""

    def __init__(self, source, save_dir, filename=None, proxy=None, proxies=None,
                 selected_files=None, seed_hours=0, upload_rate_limit=0):
        self.source = source
        self.url = source
        self.kind = source_kind(source)
        self.save_dir = save_dir
        self.seed_hours = max(0.0, float(seed_hours or 0))
        self.upload_rate_limit = max(0, int(upload_rate_limit or 0))

        # 線路解析：proxies 為「完整線路清單」，其中 None 代表直連。
        # proxy 單獨給定時視為單一線路（向下相容）。
        if proxy is not None:
            self._line_proxies = [proxy]
        elif proxies:
            self._line_proxies = list(proxies)
        else:
            self._line_proxies = [None]
        self.proxy = next((p for p in self._line_proxies if p is not None), None)

        # 選擇性下載：None 表示下載整包；list 表示只下載指定檔案 index。
        # 多線路分片只支援整包（selected_files 為 None），否則退回單線路。
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

        self._lines = [
            LineSession(_line_key_for(p), p, upload_rate_limit=self.upload_rate_limit)
            for p in self._line_proxies
        ]
        self._line = self._lines[0]  # 向下相容：指向主要線路
        self._line_key = self._lines[0].key
        self._line_labels = {_line_key_for(p): _line_label_for(p) for p in self._line_proxies}
        self._active_lines = []  # 目前已加入 torrent 的 session（magnet 初始只有 primary）

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._coordinator = None

        self._ti = None
        self._info_hash_hex = None
        self._work_root = None
        self._num_pieces = 0
        self._total_size = 0
        self._total_done = 0
        self._line_done = {}
        self._last_speed = 0.0
        self._last_upload = 0.0
        self._pieces = []

        # PT（private）種子標記：載入 metadata 後於 start()/協調迴圈填入。
        self.is_private = False
        # 做種狀態：下載完成時刻與做種截止時刻（不做種時 seed_hours=0）。
        self._download_completed_at = None
        self._seed_deadline = None
        # resume data 定期保存時間戳（供重啟續傳，僅單線路時使用）。
        self._last_resume_save = 0.0
        # magnet 是否已扇出多線路
        self._fanned_out = False

    @property
    def proxies(self):
        return [p for p in self._line_proxies if p is not None]

    @property
    def total_size(self):
        return self._total_size

    @property
    def is_private_torrent(self):
        return self.is_private

    @property
    def _multi_line(self):
        return len(self._lines) > 1

    def _can_multi(self):
        """多線路分片的成立條件：公開種子 + 整包下載 + 多條線路。"""
        return (not self.is_private) and (not self.selected_files) and self._multi_line

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
                'proxies': self._line_proxies,
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
        if not self._work_root:
            return params
        path = self._resume_path()
        if not os.path.isfile(path):
            return params
        try:
            with open(path, 'rb') as f:
                data = f.read()
            rp = lt.read_resume_data(data)
            ti = rp.ti if rp.ti is not None else getattr(params, 'ti', None)
            if ti is None:
                return params
            rp.ti = ti
            rp.save_path = self.save_dir
            rp.storage_mode = lt.storage_mode_t.storage_mode_sparse
            rp.flags = lt.torrent_flags.auto_managed
            self._ti = ti
            self._num_pieces = ti.num_pieces()
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

    def _drain_alerts(self, session=None):
        session = session or self._line.session
        try:
            for a in session.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    self._persist_resume_data(a.params)
        except Exception:
            pass

    def _wait_resume_alert(self, timeout=3.0, session=None):
        session = session or self._line.session
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                for a in session.pop_alerts():
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

    def _make_params_with_ti(self, ti):
        p = lt.add_torrent_params()
        p.ti = ti
        p.save_path = self.save_dir
        p.storage_mode = lt.storage_mode_t.storage_mode_sparse
        p.flags = lt.torrent_flags.auto_managed
        return p

    def _make_params_magnet(self):
        p = lt.parse_magnet_uri(self.source)
        p.save_path = self.save_dir
        p.storage_mode = lt.storage_mode_t.storage_mode_sparse
        p.flags = lt.torrent_flags.auto_managed
        return p

    def _apply_file_priorities(self):
        """依 selected_files 設定檔案優先權（0 = 不下載）；僅單線路且有選擇性下載時生效。"""
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
        """偵測 PT（private）種子並記錄。"""
        try:
            self.is_private = bool(ti.priv())
            if self.is_private:
                logger.info('偵測到 PT 種子（private=1），維持單線路並由 libtorrent 停用 DHT/PEX: %s',
                            self.filename)
        except Exception:
            self.is_private = False

    def _reset_handles(self):
        """移除所有 session 現有的 torrent，確保 start() 可重複呼叫。"""
        for line in self._lines:
            line.remove()
        self._active_lines = []
        self._fanned_out = False

    def _partition_priorities(self, lo, hi):
        return [1 if lo <= i < hi else 0 for i in range(self._num_pieces)]

    def _fan_out(self):
        """metadata 已知後，把 piece 空間分片給各 session（主要供 magnet 延遲扇出）。"""
        if self._fanned_out or not self._can_multi() or self._ti is None:
            return
        self._fanned_out = True
        ranges = partition_ranges(self._num_pieces, len(self._lines))
        if not ranges:
            return

        # primary 已在單線路階段加入，動態改其 piece 優先權
        try:
            self._line.handle.prioritize_pieces(self._partition_priorities(*ranges[0]))
        except Exception as e:
            logger.warning('重設 primary piece 優先權失敗: %s', e)

        # 其餘線路逐一加入同一 torrent（共用 save_path）
        for i in range(1, len(self._lines)):
            line = self._lines[i]
            params = self._make_params_with_ti(self._ti)
            params.piece_priorities = self._partition_priorities(*ranges[i])
            try:
                line.add_torrent(params)
                self._active_lines.append(line)
            except Exception as e:
                logger.warning('啟動 BT 線路 %s 失敗: %s', line.key, e)

    def start(self):
        if self.kind is None:
            self.status = 'error'
            self.error_message = '不是有效的 BT 來源（magnet 或 .torrent）'
            return False

        self.status = 'downloading'
        self.error_message = ''
        self.start_time = time.time()
        self._stop.clear()
        self._download_completed_at = None
        self._seed_deadline = None

        os.makedirs(self.save_dir, exist_ok=True)
        self._reset_handles()

        if self.kind == 'torrent':
            try:
                ti = lt.torrent_info(self.source)
            except Exception as e:
                self.status = 'error'
                self.error_message = f'解析 .torrent 失敗: {e}'
                return False
            self._ti = ti
            self._num_pieces = ti.num_pieces()
            self._total_size = self._wanted_size(ti)
            self.filename = ti.name() or self.filename
            self.filepath = os.path.join(self.save_dir, self.filename)
            self._info_hash_hex = _hash_hex(ti.info_hashes())
            self._mark_private(ti)
            params = self._make_params_with_ti(ti)
        else:
            try:
                params = self._make_params_magnet()
            except Exception as e:
                self.status = 'error'
                self.error_message = f'解析 magnet 失敗: {e}'
                return False
            self._info_hash_hex = _hash_hex(params.info_hashes)

        if self._info_hash_hex:
            self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)

        # resume 續傳僅支援單線路（多線路 resume 合併留待後續）
        if not self._can_multi():
            params = self._try_load_resume(params)

        if self._info_hash_hex:
            self._save_state()

        if self.kind == 'torrent' and self._can_multi():
            # 公開種子 + 整包：立即分片到全部線路
            self._fanned_out = True
            ranges = partition_ranges(self._num_pieces, len(self._lines))
            try:
                params.piece_priorities = self._partition_priorities(*ranges[0])
                self._line.add_torrent(params)
                self._active_lines = [self._line]
            except Exception as e:
                self.status = 'error'
                self.error_message = f'啟動 BT 下載失敗: {e}'
                return False
            for i in range(1, len(self._lines)):
                line = self._lines[i]
                p2 = self._make_params_with_ti(self._ti)
                p2.piece_priorities = self._partition_priorities(*ranges[i])
                try:
                    line.add_torrent(p2)
                    self._active_lines.append(line)
                except Exception as e:
                    logger.warning('啟動 BT 線路 %s 失敗: %s', line.key, e)
        else:
            # 單線路（或 magnet 先以 primary 取 metadata）
            try:
                self._line.add_torrent(params)
                self._active_lines = [self._line]
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
        for line in self._active_lines:
            h = line.handle
            if h is None:
                continue
            try:
                h.save_resume_data(
                    lt.save_resume_flags_t.flush_disk_cache | lt.save_resume_flags_t.save_info_dict)
            except Exception:
                pass
            try:
                h.pause()
            except Exception as e:
                logger.warning('BT pause 失敗: %s', e)
        if not self._can_multi():
            self._wait_resume_alert(timeout=3.0)
        self._save_state()
        return True

    def resume(self):
        if self.status not in ('paused', 'initialized'):
            return False
        if not self._active_lines:
            return self.start()
        self.status = 'seeding' if self._download_completed_at is not None else 'downloading'
        for line in self._active_lines:
            h = line.handle
            if h is not None:
                try:
                    h.resume()
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
        for line in self._lines:
            line.remove()
        self._active_lines = []
        self._cleanup_work()
        self.status = 'canceled'
        return True

    # ------------------------------------------------------------------ #
    # 協調迴圈
    # ------------------------------------------------------------------ #
    def _on_metadata(self, ti):
        self._ti = ti
        self._num_pieces = ti.num_pieces()
        self._total_size = self._wanted_size(ti)
        self.filename = ti.name() or self.filename
        self.filepath = os.path.join(self.save_dir, self.filename)
        self._mark_private(ti)
        if not self._info_hash_hex:
            self._info_hash_hex = _hash_hex(ti.info_hashes())
            self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)
        self._save_state()
        self._fan_out()

    def _coordinator_loop(self):
        while not self._stop.is_set():
            try:
                for line in self._active_lines:
                    line.session.post_torrent_updates()

                # resume data 定期保存：僅單線路（多線路留待後續）
                if not self._can_multi():
                    now = time.time()
                    if now - self._last_resume_save >= 10.0:
                        self._last_resume_save = now
                        h = self._line.handle
                        if h is not None and h.is_valid():
                            try:
                                h.save_resume_data(
                                    lt.save_resume_flags_t.flush_disk_cache
                                    | lt.save_resume_flags_t.save_info_dict)
                            except Exception:
                                pass
                    self._drain_alerts()

                # magnet：primary 取得 metadata 後更新並視情況扇出多線
                if self.kind == 'magnet' and self._ti is None and self._line.handle is not None:
                    st0 = self._line.handle.status()
                    if getattr(st0, 'has_metadata', False):
                        try:
                            ti = self._line.handle.torrent_file()
                            if ti:
                                self._on_metadata(ti)
                        except Exception as e:
                            logger.debug('讀取 metadata 失敗: %s', e)

                # 聚合各 active session 狀態
                total_wanted = 0
                total_done = 0
                total_rate = 0
                total_upload = 0
                line_done = {}
                merged_bits = None
                num_pieces = self._num_pieces

                for line in self._active_lines:
                    h = line.handle
                    if h is None or not h.is_valid():
                        continue
                    st = h.status()
                    wanted = st.total_wanted
                    done = st.total_wanted_done
                    total_wanted += wanted
                    total_done += done
                    total_rate += st.download_rate
                    total_upload += getattr(st, 'upload_rate', 0)
                    line_done[line.key] = line_done.get(line.key, 0) + done
                    bits = getattr(st, 'pieces', None)
                    if bits:
                        if merged_bits is None:
                            merged_bits = bytearray(bits)
                        else:
                            for j in range(len(bits)):
                                merged_bits[j] |= bits[j]

                pieces_frac = []
                if merged_bits is not None and num_pieces > 0:
                    pieces_frac = [1.0 if _bit(merged_bits, i) else 0.0
                                   for i in range(num_pieces)]

                with self._lock:
                    if total_wanted > 0:
                        self._total_size = total_wanted
                    self._total_done = total_done
                    self._line_done = line_done
                    self._last_speed = total_rate
                    self._last_upload = total_upload
                    if pieces_frac:
                        self._pieces = pieces_frac

                # 完成偵測：
                # 單線路沿用 libtorrent 的 seeding/finished + done>=wanted 判準；
                # 多線路以「所有分片 piece 總下載量達整包大小」判定。
                download_done = False
                if self._can_multi():
                    download_done = (self._total_size > 0 and total_done >= self._total_size)
                else:
                    for line in self._active_lines:
                        h = line.handle
                        if h is None or not h.is_valid():
                            continue
                        st = h.status()
                        state_val = getattr(st, 'state', None)
                        is_fin = (state_val == lt.torrent_status.finished
                                  or state_val == lt.torrent_status.seeding)
                        if (bool(getattr(st, 'is_seeding', False)) or is_fin
                                or (st.total_wanted > 0 and st.total_wanted_done >= st.total_wanted)):
                            download_done = True
                            break

                if download_done:
                    # 依設定決定是否進入做種；不做種（seed_hours=0）下載完成即停
                    if self._download_completed_at is None:
                        self._download_completed_at = time.time()
                        if self.seed_hours > 0:
                            self._seed_deadline = self._download_completed_at + self.seed_hours * 3600
                            self.status = 'seeding'
                            logger.info('BT 下載完成，進入做種 %s 小時: %s',
                                        self.seed_hours, self.filename)
                        else:
                            self.end_time = time.time()
                            self.status = 'completed'
                            for line in self._lines:
                                line.remove()
                            self._active_lines = []
                            self._cleanup_work()
                            return

                if self.status == 'seeding' and self._seed_deadline is not None \
                        and time.time() >= self._seed_deadline:
                    self.end_time = time.time()
                    self.status = 'completed'
                    for line in self._lines:
                        line.remove()
                    self._active_lines = []
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
            line_done = dict(self._line_done)

        pct = (done / total * 100) if total > 0 else 0.0
        elapsed = 0
        if self.start_time:
            elapsed = (self.end_time or time.time()) - self.start_time

        line_bytes = {}
        for key, _label in self._line_labels.items():
            line_bytes[key] = line_done.get(key, 0)

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
            'thread_count': max(1, len(self._lines)),
            'block_count': len(pieces),
            'blocks': _downsample_blocks(pieces),
            'line_bytes': line_bytes,
            'line_labels': dict(self._line_labels),
            'is_private': self.is_private,
            'seed_hours': self.seed_hours,
            'seeding_remaining': seeding_remaining,
        }
