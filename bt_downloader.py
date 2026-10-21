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

限制（詳見 docs/bt-multi-line-design.md）：
- 選擇性下載（selected_files）不與多線路併用，設了 selected_files 就退回單線路。
- 多線路 resume：不帶 have_pieces，交由 libtorrent 的 checking_files 對磁碟上的
  既有資料重新做 hash 校驗（已正確的 piece 不重下、損壞/缺漏的由負責線路補下），
  從根本保證「回報 100% 一定代表檔案真正完整」。
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

# DHT bootstrap 節點：預設節點（router.bittorrent.com 等）在部分地區可能被擋，
# 補上多個常用公共節點，提升磁力連結取 metadata 的成功率。
DHT_BOOTSTRAP_NODES = (
    'dht.libtorrent.org:25401,'
    'dht.transmissionbt.com:6881,'
    'router.bittorrent.com:6881,'
    'router.utorrent.com:6881'
)

# 每線路 session 的 DHT 上傳上限（bytes/sec）。DHT 的小封包會與 TCP ACK 搶
# 上行，設上限讓 ACK 優先通過，避免上行塞滿導致下載被 ACK 延遲拖慢。
DHT_UPLOAD_LIMIT = 4 * 1024

# libtorrent 的 block 大小固定為 16 KiB，block_finished_alert 以此粒度逐塊回報，
# 用於把「下載中/部分完成」的 piece 細分成 frac 供 UI 區塊進度顯示。
BT_BLOCK_SIZE = 16 * 1024

# 判斷 piece 是否「正在下載」的時間窗（秒）：最後一次收到 block 完成時間落在窗內
# 視為 active，超過則視為部分完成但已閒置（UI 顯示淡綠而非藍色）。
BT_PIECE_ACTIVE_WINDOW = 5.0

# 卡死偵測：一條線路仍握有未完成 piece、卻連續一段時間「既沒完成新 piece 也沒收到
# block」時判定卡死，把它的剩餘 piece 重派給其他仍在前進的線路，避免單一慢速／斷連
# 線路把整顆種子拖在 99.x%。一般階段用較長時間窗避免誤判；收尾階段（剩餘 piece 少）
# 用短窗加速救援——這是多線路架構下「endgame」的對應機制。
BT_STALL_TIMEOUT = 60.0          # 一般階段卡死判定（秒）
BT_STALL_ENDGAME_TIMEOUT = 10.0  # 收尾階段卡死判定（秒）
BT_ENDGAME_PIECES = 8            # 剩餘未完成 piece 數低於此值視為收尾階段
# 判定「有進度」的最小速率（bytes/s）。libtorrent 的 download_rate 含協定雜訊
# （keepalive、handshake、choke 往返），實測線路已 peers=0、payload 完全沒動時
# 仍會回報 2~25 B/s。若拿 rate > 0 當進度，卡死計時每輪都被重置，收尾救援永遠
# 不會觸發，多線任務就永遠停在 99.x%。1 KiB/s 遠高於雜訊、又低於任何可用線路。
BT_STALL_MIN_RATE = 1024
# 卡死救援後的冷卻時間（秒）：期間不再把 piece 派回該線路，避免與 _rebalance 互抵。
BT_RESCUE_COOLDOWN = 60.0
# 任務層級放棄判定（秒）：連續這麼久「一個位元組都沒拿到」就判定 swarm 已無來源、
# 把任務標成 error（可按重試）。卡死救援只處理「部分線路」卡住，全部線路都沒 peer
# 時過去會永遠停在 downloading。30 分鐘遠長於任何 tracker announce 間隔與 hash
# 校驗時間，不會誤殺慢速但活著的種子。
BT_GIVE_UP_SECONDS = 30 * 60

# HTTP/HTTPS tracker 後備清單：DHT 與 UDP tracker 在部分環境（5G 行動網路、
# SOCKS5 代理）被封，這些 TCP tracker 可作為 metadata / peer 來源的後備。
HTTP_FALLBACK_TRACKERS = [
    'http://tracker.opentrackr.org:1337/announce',
    'https://tracker.gbitt.info:443/announce',
    'https://tracker.tamersunion.org:443/announce',
    'https://tracker.loligirl.cn:443/announce',
    'http://tracker.bt4g.com:2095/announce',
    'https://tracker.tiny-vps.com:443/announce',
    'http://open.acgnxtracker.com:80/announce',
]


def _merge_http_tracker_fallback(params):
    """把 HTTP/HTTPS 後備 tracker 併入 add_torrent_params（去重）。

    磁力連結在 DHT/UDP 被擋的環境（如 5G 行動網路、SOCKS5 代理）下，無法靠
    DHT 或 UDP tracker 找 peers；HTTP/HTTPS tracker 走 TCP，可正常穿透代理。
    params.trackers 的 getter 回傳副本，append 會靜默失效，必須重新指派整個 list。
    """
    try:
        existing = list(params.trackers or [])
        merged = existing + [t for t in HTTP_FALLBACK_TRACKERS if t not in existing]
        params.trackers = merged
    except Exception as e:
        logger.debug('合併後備 tracker 失敗: %s', e)



# 同名種子隔離：不同 info hash 但 info dict 的 name 相同時，libtorrent 會把它們
# 寫進同一個 save_path/<name>，兩邊各自下自己的 piece、互相覆寫，最終變成
# 「兩邊都回報 100%、檔案卻是殘缺混血」——這是 BT 協定層無法避免的（檔案路徑只由
# name 決定），只能由客戶端在加入前把其中一顆改名。這裡維護「進程內某個根路徑
# 被哪個 info hash 佔用」，衝突時自動加 " (1)" 後綴分家（qBittorrent 同款做法）。
# 只做進程內登記：本程式是單實例，跨次啟動的衝突由磁碟存在性判斷兜住。
_BT_ROOT_CLAIMS = {}
_BT_ROOT_CLAIMS_LOCK = threading.Lock()


def _root_claim_key(path):
    """Windows 路徑大小寫不敏感，登記鍵一律正規化。"""
    return os.path.normcase(os.path.abspath(path))


def _claim_bt_root(path, info_hash):
    """登記 path 由 info_hash 佔用；已被「不同 info hash」佔用時回傳 False。"""
    key = _root_claim_key(path)
    with _BT_ROOT_CLAIMS_LOCK:
        owner = _BT_ROOT_CLAIMS.get(key)
        if owner is not None and owner != info_hash:
            return False
        _BT_ROOT_CLAIMS[key] = info_hash
        return True


def _release_bt_root(path, info_hash=None):
    """釋放登記；給定 info_hash 時只在仍由它持有才移除，避免誤刪別人的登記。"""
    if not path:
        return
    key = _root_claim_key(path)
    with _BT_ROOT_CLAIMS_LOCK:
        if info_hash is None or _BT_ROOT_CLAIMS.get(key) == info_hash:
            _BT_ROOT_CLAIMS.pop(key, None)


def renamed_files_for(ti, root_name):
    """算出「把 torrent 根名改成 root_name」所需的 renamed_files 映射。

    libtorrent 依 info dict 的 name 決定落盤根路徑（save_path/name）；
    add_torrent_params.name 不影響路徑（libtorrent 2.1.1 實測），只能逐檔改：
    單檔種子的相對路徑就是檔名，整段換掉；多檔種子把第一段（根目錄名）換掉，
    其餘子目錄保持原樣。分隔符用 '/'，libtorrent 在 Windows 上照樣接受。
    """
    try:
        orig = ti.name()
        if not root_name or not orig or root_name == orig:
            return {}
        fs = ti.files()
        out = {}
        for i in range(fs.num_files()):
            parts = fs.file_path(i).replace('\\', '/').split('/')
            if len(parts) <= 1:
                out[i] = root_name
            else:
                parts[0] = root_name
                out[i] = '/'.join(parts)
        return out
    except Exception as e:
        logger.warning('計算 BT 改名映射失敗: %s', e)
        return {}


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


def bt_info_hash(source):
    """從 magnet 或 .torrent 來源提取 info hash hex（v1 優先）；失敗回傳 None。

    供跨「來源字串」去重用：同一顆種子的 magnet 與 .torrent 檔（或不同路徑的
    .torrent 檔）其 info hash 相同，可據此避免重複建立任務。
    """
    try:
        if is_magnet(source):
            return _hash_hex(lt.parse_magnet_uri(source).info_hashes)
        if source_kind(source) == 'torrent':
            return _hash_hex(lt.torrent_info(source).info_hashes())
    except Exception:
        pass
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


def _downsample_blocks(states, max_blocks=1000):
    """把區塊狀態降採樣到最多 max_blocks 個區間，降低 UI 重繪成本。

    states 為 list of {'frac': 0.0~1.0, 'active': bool}。降採樣時 frac 取區間平均、
    active 只要區間內任一片正在下載即視為 True。
    """
    n = len(states)
    if n <= max_blocks:
        return [{'frac': s['frac'], 'active': s['active']} for s in states]
    out = []
    per = n / max_blocks
    for i in range(max_blocks):
        lo = int(i * per)
        hi = int((i + 1) * per)
        seg = states[lo:hi] or [{'frac': 0.0, 'active': False}]
        frac = sum(s['frac'] for s in seg) / len(seg)
        active = any(s['active'] for s in seg)
        out.append({'frac': frac, 'active': active})
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


def _unique_line_keys(proxies):
    """為每條線路產生唯一 key。

    `_line_key_for` 對所有直連都回 'direct'，同一組代理也可能出現兩筆相同
    host:port。而 _line_progress_snapshot / _line_stall_since 都以 line.key 為
    索引：key 一撞，每線的卡死計時就互相覆蓋（A 線沒有剩餘 piece 時會把 key
    清掉，B 線的計時因此每輪歸零），收尾救援永遠不觸發，多線任務就卡在 99.x%。
    只有真的重複時才加序號，單線路與一般多代理情境的 key 完全不變。
    """
    seen = {}
    keys = []
    for proxy in proxies:
        base = _line_key_for(proxy)
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append(base if n == 0 else f'{base}#{n}')
    return keys


def _line_label_for(proxy):
    return '直連' if proxy is None else f"{proxy.get('host')}:{proxy.get('port')}"


def _verified_done(st):
    """torrent_status 是否代表該 session 已把自己負責的 piece 全部下載並通過 hash 校驗。

    libtorrent 只有在所有 wanted piece 通過 hash 校驗後才會進入 finished/seeding
    狀態（或讓 total_wanted_done 追平 total_wanted），這是唯一可靠的「真正完成」
    訊號；st.pieces（have 位元圖）在 hash 校驗通過前就可能置位（甚至校驗失敗、
    待重下時也短暫置位），不能拿來判完成。
    """
    if bool(getattr(st, 'is_seeding', False)):
        return True
    state = getattr(st, 'state', None)
    if state in (lt.torrent_status.finished, lt.torrent_status.seeding):
        return True
    return st.total_wanted > 0 and st.total_wanted_done >= st.total_wanted


def _stall_timeout_for(remaining_total):
    """依剩餘 piece 數回傳卡死判定時間窗；收尾階段縮短以加速救援。"""
    if remaining_total <= BT_ENDGAME_PIECES:
        return BT_STALL_ENDGAME_TIMEOUT
    return BT_STALL_TIMEOUT


def _update_stall(prev, cur, rate, since, now, timeout):
    """純函式：依進度快照更新卡死計時。回傳 (新的 since, 是否卡死)。

    prev : 上次的 total_wanted_done（None 表示首次）
    cur  : 本次的 total_wanted_done
    rate : 本次 download_rate（bytes/sec）
    since: 卡死起算時間戳（None 表示尚未開始卡死）
    now  : 目前時間戳
    timeout : 卡死判定時間窗（秒）

    有實質進度（完成新 piece，或正在收 block）就重置卡死計時；否則累計，
    連續超過 timeout 秒無進度即判定卡死。
    """
    if prev is None or cur is None or cur > prev or rate >= BT_STALL_MIN_RATE:
        return None, False
    since = now if since is None else since
    return since, (now - since >= timeout)


class LineSession:
    """包裝一個 libtorrent.session，綁定單一線路（直連或 SOCKS5 代理）。"""

    def __init__(self, key='direct', proxy=None, upload_rate_limit=0, max_connections=0,
                 force_tcp=False, listen_port=0):
        self.key = key
        self.proxy = proxy
        self.upload_rate_limit = max(0, int(upload_rate_limit or 0))
        self.max_connections = max(0, int(max_connections or 0))
        self.force_tcp = bool(force_tcp)
        self.listen_port = max(0, int(listen_port or 0))
        self.session = self._make_session()
        self.handle = None

    def _make_session(self):
        # 監聽介面：直連線路可用固定埠（配合 UPnP/NAT-PMP 讓外部 peer 主動連入），
        # 其餘情況（代理線路、或埠為 0）維持動態埠，避免多 session 搶同一埠。
        if self.proxy is None and self.listen_port > 0:
            listen_iface = f'0.0.0.0:{self.listen_port}'
        else:
            listen_iface = '0.0.0.0:0'
        settings = {
            'listen_interfaces': listen_iface,
            'enable_dht': True,
            # LSD 與 DHT/tracker 的 peer 發現重疊，關閉以減少上行小封包。
            'enable_lsd': False,
            # UPnP/NAT-PMP 只在「需要外部 peer 主動連入（固定監聽埠做種）」時才有用；
            # 其餘情況關閉，避免週期性 UDP 佔用上行與 NAT 資源。
            'enable_upnp': self.proxy is None and self.listen_port > 0,
            'enable_natpmp': self.proxy is None and self.listen_port > 0,
            # 補上多個 DHT bootstrap 節點，避免預設節點被擋導致磁力 metadata 取不到
            'dht_bootstrap_nodes': DHT_BOOTSTRAP_NODES,
            # DHT 上傳限速，讓位給 TCP ACK（見 DHT_UPLOAD_LIMIT 註解）。
            'dht_upload_rate_limit': DHT_UPLOAD_LIMIT,
            # 客戶端偽裝：對外宣告為 qBittorrent 4.6.0
            'user_agent': BT_USER_AGENT,
            'peer_fingerprint': BT_PEER_FINGERPRINT,
            # 啟用區塊/分片完成通知，供區塊進度（frac/active）追蹤；預設只開 error
            # 類別，關閉後 pop_alerts() 收不到 block_finished_alert。
            'alert_mask': int(
                lt.alert.category_t.error_notification
                | lt.alert.category_t.status_notification
                | lt.alert.category_t.storage_notification
                | lt.alert.category_t.progress_notification
                | lt.alert.category_t.piece_progress_notification
                | lt.alert.category_t.block_progress_notification),
            # 高速下載時 block_finished_alert 量大，調高佇列上限，避免 0.5 秒輪詢
            # 週期內被丟棄而讓 active 指示閃爍（預設 2000）。
            'alert_queue_size': 16384,
        }
        if self.upload_rate_limit > 0:
            # 上傳限速（bytes/sec），0 表示不限。PT 用戶可設非零上限以控制上傳頻寬。
            settings['upload_rate_limit'] = self.upload_rate_limit
        if self.max_connections > 0:
            # 連線數上限：避免多顆種子並行時開出數百條連線，壓垮下游的轉接代理
            # （如手機 SOCKS5 / WinDivert 轉發）。0 = 用 libtorrent 預設。
            settings['connections_limit'] = self.max_connections
            # 半開（正在建立）連線數一併壓低，避免瞬間連線建立洪峰觸發大量回呼。
            settings['connection_speed'] = min(20, self.max_connections)
        if self.force_tcp:
            # 僅用 TCP：停用 uTP（走 UDP 的資料傳輸）。UDP 被封的環境（5G 行動網路 +
            # SOCKS5 轉接）下，uTP 連線注定失敗卻會佔用半開連線名額與重試資源，
            # 拖垮可用的 TCP 連線。停用後所有 peer 資料傳輸走 TCP。
            settings['enable_outgoing_utp'] = False
            settings['enable_incoming_utp'] = False
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


class DHTService:
    """常駐 DHT 服務：不掛任何 torrent 的獨立 session，僅做 routing table 預熱與節點統計。

    改為「按需」：__init__ 不再啟動 session，改由 DHTGovernor 依下載需求呼叫
    start()/apply_policy()/shutdown()。無 BT 任務時保持關閉（零封包）；下載進行中
    限速讓位給 TCP ACK。

    注意：真正替種子做 announce/get_peers 的 DHT 在各 BTTask 直連線路的 session
    （LineSession.enable_dht）上；此服務本身不掛 torrent，屬於可選的預熱節點。
    """

    UPLOAD_FULL = 0              # 0 = 不限（無下載競爭時）
    UPLOAD_THROTTLED = 2 * 1024  # 下載中：2 KB/s
    UPLOAD_IDLE = 512            # 下載飽和時：0.5 KB/s，勉強維持

    def __init__(self):
        self.session = None

    def _settings(self):
        return {
            'listen_interfaces': '0.0.0.0:0',
            'enable_dht': True,
            'dht_bootstrap_nodes': DHT_BOOTSTRAP_NODES,
            'user_agent': BT_USER_AGENT,
            'peer_fingerprint': BT_PEER_FINGERPRINT,
            'enable_lsd': False,
            'enable_upnp': False,
            'enable_natpmp': False,
            'enable_outgoing_utp': False,
            'enable_incoming_utp': False,
        }

    def start(self):
        if self.session is not None:
            return
        try:
            self.session = lt.session(self._settings())
        except Exception as e:
            logger.debug('常駐 DHT session 初始化失敗: %s', e)

    def apply_policy(self, mode):
        """套用 DHT 侵略度：'full' | 'throttled' | 'idle' | 'off'。"""
        if mode == 'off':
            self.shutdown()
            return
        self.start()
        if self.session is None:
            return
        cap = {
            'full': self.UPLOAD_FULL,
            'throttled': self.UPLOAD_THROTTLED,
            'idle': self.UPLOAD_IDLE,
        }[mode]
        try:
            self.session.apply_settings({'dht_upload_rate_limit': cap})
        except Exception as e:
            logger.debug('設定 DHT 上傳上限失敗: %s', e)

    def node_count(self):
        """回傳 routing table 中的 DHT 節點數（qBittorrent 顯示的同一指標）。"""
        if self.session is None:
            return 0
        try:
            return int(getattr(self.session.status(), 'dht_nodes', 0))
        except Exception:
            return 0

    def shutdown(self):
        """釋放 session：丟棄唯一引用，由 libtorrent 解構停止所有網路活動。

        註：此版 libtorrent 的 session 沒有 abort() 方法，直接丟引用即可。
        """
        self.session = None


class DHTGovernor:
    """DHT 自適應調速器：依「下載需求」動態決定 DHT 的侵略程度。

    TCP 下載被 ACK 時脈驅動、ACK 走上傳路徑；家用/行動網路的上傳遠小於下載，
    上傳一旦被小封包（DHT）塞滿，ACK 排隊 → RTT 上升 → cwnd/RTT 下滑 → 下載變慢。
    所以核心策略只有一條：下載在跑時把 DHT 壓到「夠用就好」，把上傳讓回給 ACK。

    Governor 只負責「算該處於哪個模式」；實際套用交由 DownloadManager 注入的
    apply_fn（它才知道有哪些 session 要調）。decide() 是純函式，方便單測。
    """

    FULL = 'full'            # 無下載 / 等待 metadata：DHT 全速
    THROTTLED = 'throttled'  # 有下載在跑：DHT 限速讓位
    IDLE = 'idle'            # 下載已飽和且 routing table 已夠：壓到最低
    OFF = 'off'              # 完全無 BT 需求：關閉 DHT，零封包

    def __init__(self, demand_fn, apply_fn, interval=1.5,
                 rich_nodes=200, saturate_rate=1024 * 1024):
        self._demand_fn = demand_fn   # -> dict(bt_active, magnet_waiting, total_download_rate, saturated, node_count)
        self._apply_fn = apply_fn     # (mode) -> None，實際套用 policy
        self._interval = interval
        self._rich_nodes = rich_nodes
        self._saturate_rate = saturate_rate
        self._current = None
        self._cooldown_until = 0.0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def decide(self, d):
        """純函式：依需求快照回傳目標 policy（可單測）。"""
        if not d.get('bt_active') and not d.get('magnet_waiting'):
            return self.OFF
        if d.get('magnet_waiting'):
            return self.FULL
        if d.get('total_download_rate', 0.0) > 0:
            if d.get('saturated') and d.get('node_count', 0) >= self._rich_nodes:
                return self.IDLE
            return self.THROTTLED
        return self.FULL

    def _loop(self):
        while not self._stop.is_set():
            try:
                d = self._demand_fn() or {}
            except Exception:
                d = {}
            target = self.decide(d)
            # 遲滯：模式切換後暫緩一段時間，避免在邊界頻繁抖動
            if target != self._current and time.time() >= self._cooldown_until:
                try:
                    self._apply_fn(target)
                except Exception:
                    logger.debug('套用 DHT policy 失敗', exc_info=True)
                self._current = target
                self._cooldown_until = time.time() + self._interval
            self._stop.wait(self._interval)


class BTTask:
    """BT 下載任務：一或多個 session 下載同一 torrent，生命週期對齊 DownloadTask。"""

    def __init__(self, source, save_dir, filename=None, proxy=None, proxies=None,
                 selected_files=None, seed_hours=0, upload_rate_limit=0, resume_interval=10.0,
                 max_connections=0, proxy_max_connections=0, force_tcp=False, listen_port=0,
                 root_name=None, download_rate_limit=0):
        self.source = source
        self.url = source
        self.kind = source_kind(source)
        self.bt_info_hash = bt_info_hash(source)
        self.save_dir = save_dir
        self.seed_hours = max(0.0, float(seed_hours or 0))
        self.upload_rate_limit = max(0, int(upload_rate_limit or 0))
        # 下載限速（bytes/s，0 = 不限）。BT 的資料由 libtorrent 直接寫盤，應用層的
        # RateLimiter 攔不到，只能靠 libtorrent 的 download_limit；由 DownloadManager
        # 依活躍任務數把全域額度均分後呼叫 set_download_limit() 下發。
        self.download_rate_limit = max(0, int(download_rate_limit or 0))
        self.max_connections = max(0, int(max_connections or 0))
        self.proxy_max_connections = max(0, int(proxy_max_connections or 0))
        self.force_tcp = bool(force_tcp)
        self.listen_port = max(0, int(listen_port or 0))

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
        # 實際落盤根名（相對 save_dir）。同名種子衝突時會被改成 "name (1)"；
        # 續傳時由 root_name 參數（task.json）帶回來，避免每次啟動都重新取名。
        self._root_name = root_name
        self._root_claim_path = None
        # start() 時判斷 .bt_tmp/<ih>/ 是否為上次留下的續傳狀態（見 _resolve_root_name）
        self._resuming = False
        # 任務層級放棄判定：上次看到的位元組數與開始停滯的時間
        self._give_up_done = None
        self._give_up_since = None
        self.status = 'initialized'
        self.error_message = ''
        self.start_time = None
        self.end_time = None
        self.task_id = None

        _line_keys = _unique_line_keys(self._line_proxies)
        self._lines = [
            LineSession(key, p, upload_rate_limit=self.upload_rate_limit,
                        max_connections=(self.proxy_max_connections
                                         if p is not None else self.max_connections),
                        # 代理線路強制 TCP：uTP 走 UDP，libtorrent 不會把 uTP 轉進
                        # SOCKS5，開著只會浪費半開連線；直連線路沿用全域開關。
                        force_tcp=self.force_tcp or p is not None,
                        listen_port=self.listen_port)
            for key, p in zip(_line_keys, self._line_proxies)
        ]
        self._line = self._lines[0]  # 向下相容：指向主要線路
        self._line_key = self._lines[0].key
        self._line_labels = {key: _line_label_for(p)
                             for key, p in zip(_line_keys, self._line_proxies)}
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
        # block 級部分進度：每 piece 已完成 block 數、block 總數、最後一次 block 完成
        # 時間戳（後者供 active 判斷）。metadata 到達後由 _init_piece_progress 填充。
        self._blocks_done = []
        self._blocks_per_piece = []
        self._last_block_ts = []

        # PT（private）種子標記：載入 metadata 後於 start()/協調迴圈填入。
        self.is_private = False
        # 做種狀態：下載完成時刻與做種截止時刻（不做種時 seed_hours=0）。
        self._download_completed_at = None
        self._seed_deadline = None
        # resume data 定期保存時間戳（供重啟續傳，僅單線路時使用）。
        self._last_resume_save = 0.0
        # resume 自動保存間隔（秒），最小 1 秒。
        self._resume_interval = max(1.0, float(resume_interval or 10.0))
        # magnet 是否已扇出多線路
        self._fanned_out = False
        # piece 所有權：list[line_idx]，index 為 piece、值為負責下載該 piece 的線路。
        self._piece_owner = []
        # 動態重新派工節流時間戳
        self._last_rebalance = 0.0
        # 跨線路 peer 共享節流時間戳（見 _coordinator_loop）
        self._last_peer_share = 0.0
        # 已嘗試派發過的 peer (ip, port)，避免對連不上的 peer 每 5 秒重試一次
        self._shared_peers = set()
        # 卡死偵測：每條線路（line_key）最近一次 own-piece 進度與卡死起算時間。
        self._line_progress_snapshot = {}  # line_key -> total_wanted_done
        self._line_stall_since = {}        # line_key -> timestamp or None
        # line_key -> 上次被卡死救援把 piece 搬走的時間。用來讓 _rebalance 在冷卻期
        # 內不要把 piece 發回這條線路（否則救援與重平衡會互相抵銷，見 _rebalance）。
        self._line_rescued_at = {}

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
    # 同名種子隔離
    # ------------------------------------------------------------------ #
    def _matches_torrent_data(self, ti):
        """既有路徑上的資料大小是否與 torrent 相符（用來判斷是不是同一顆種子）。

        只比大小、不讀內容：libtorrent 加入後本來就會做全量 hash 校驗（壞的 piece
        自動重下），大小相符就交給它；大小不符則幾乎確定是另一顆同名種子，改名
        保護既有檔案。讀內容做 hash 對大檔太貴，而且等於重複 libtorrent 的工作。
        """
        if ti is None:
            return False
        try:
            fs = ti.files()
            n = fs.num_files()
            if n <= 0:
                return False
            root = os.path.join(self.save_dir, ti.name() or '')
            first = fs.file_path(0).replace('\\', '/').split('/')
            if n == 1 and len(first) <= 1:
                # 單檔種子：save_dir/<name> 就是檔案本身
                if not os.path.isfile(root) or os.path.getsize(root) != fs.file_size(0):
                    return False
                return self._sample_piece_matches(root, ti)
            if not os.path.isdir(root):
                return False
            for i in range(n):
                parts = fs.file_path(i).replace('\\', '/').split('/')
                target = os.path.join(root, *parts[1:]) if len(parts) > 1 else root
                if not os.path.isfile(target):
                    return False
                if os.path.getsize(target) != fs.file_size(i):
                    return False
            return True
        except Exception as e:
            logger.debug('比對既有資料大小失敗: %s', e)
            return False

    def _sample_piece_matches(self, path, ti):
        """抽驗首尾 piece 的 SHA1，確認既有檔案就是這顆種子的資料。

        只比大小無法區分「同名、同大小、不同內容」的兩顆種子（實測兩顆 30KB 的
        同名種子就是這種情況）。抽驗首尾各一個 piece 成本可忽略，卻能把誤判率
        降到實務上可接受；完整校驗仍交給 libtorrent 的 checking_files。
        """
        try:
            import hashlib
            n = ti.num_pieces()
            if n <= 0:
                return True
            plen = ti.piece_length()
            size = os.path.getsize(path)
            idxs = [0] if n == 1 else [0, n - 1]
            with open(path, 'rb') as f:
                for i in idxs:
                    length = min(plen, size - i * plen)
                    if length <= 0:
                        return False
                    f.seek(i * plen)
                    if hashlib.sha1(f.read(length)).digest() != bytes(ti.hash_for_piece(i)):
                        return False
            return True
        except Exception as e:
            logger.debug('抽驗既有 piece 失敗: %s', e)
            return False

    def _resolve_root_name(self, ti):
        """決定本次任務實際使用的落盤根名，同名種子自動分家。

        libtorrent 依 info dict 的 name 建檔（save_path/name），兩顆不同 info hash
        但同名的種子會寫同一批檔案、互相覆寫，結果兩邊都回報 100% 而內容錯亂。
        這裡沿用 qBittorrent 的做法：衝突時改成 "name (1)"、"name (2)"。

        續傳（.bt_tmp/<ih>/ 有紀錄）必須沿用上次的根名：改名等於另起一份新檔案，
        libtorrent 校驗不到舊資料，進度會歸零重下。
        """
        if self._root_name:
            self._claim_root_path()
            return self._root_name
        ih = self._info_hash_hex or ''
        resuming = self._resuming
        # 續傳時 self.filename 就是上次的根名（由 task.json 帶回）；全新任務一律以
        # torrent 的 info dict 名為準，避免 magnet 顯示名（可能被截斷）污染路徑。
        # 根名一律以 torrent 的 info dict 名為準（self.filename 在 torrent 分支只是
        # 檔名去副檔名的暫定值，magnet 更是顯示名，都不能當路徑）。上次若改過名，
        # 會由 root_name 參數帶回、在函式開頭就返回，不會走到這裡。
        base = (ti.name() if ti is not None else None) or self.filename or 'bt'
        # 單檔種子的後綴要插在副檔名前（same.bin → same (1).bin），否則副檔名跑到
        # 中間，播放器／系統就認不出格式；多檔種子的根是目錄，整段加後綴即可
        # （目錄名常含點，如 My.Movie.2024，不能套 splitext）。
        single_file = False
        try:
            if ti is not None:
                fs = ti.files()
                single_file = (fs.num_files() == 1
                               and '/' not in fs.file_path(0).replace('\\', '/'))
        except Exception:
            single_file = False
        candidate = base
        suffix = 1
        while True:
            path = os.path.join(self.save_dir, candidate)
            if _claim_bt_root(path, ih):
                # 三種情況可以沿用原名：續傳、路徑不存在、或既有資料大小與本 torrent
                # 相符（同一顆種子的舊資料，交給 libtorrent 校驗即可，別無謂改名）。
                # 大小比對只在候選名 == 原名時有意義（帶後綴的候選不可能有本 torrent
                # 的資料）。
                if resuming or not os.path.exists(path) or (
                        candidate == base and self._matches_torrent_data(ti)):
                    break
                # 磁碟上已有同名資料卻不是本 torrent 的續傳 → 換下一個後綴
                _release_bt_root(path, ih)
            if single_file:
                stem, ext = os.path.splitext(base)
                candidate = '%s (%d)%s' % (stem, suffix, ext)
            else:
                candidate = '%s (%d)' % (base, suffix)
            suffix += 1
            if suffix > 1000:
                candidate = base
                break
        self._root_name = candidate
        self._root_claim_path = os.path.join(self.save_dir, candidate)
        if candidate != base:
            logger.info('BT 同名衝突，落盤根名改為 %s（原 %s）', candidate, base)
        return candidate

    def _claim_root_path(self):
        """把已解析的根路徑登記給本任務的 info hash。"""
        if self._root_claim_path and self._info_hash_hex:
            _claim_bt_root(self._root_claim_path, self._info_hash_hex)

    def _release_root_claim(self):
        _release_bt_root(self._root_claim_path, self._info_hash_hex or None)
        self._root_claim_path = None

    def _apply_root_rename(self, ti):
        """把「已加入 session」的 torrent 改到解析後的根名（magnet 走這條）。

        torrent 分支在加入前就用 params.renamed_files 指定好；magnet 加入時還沒有
        metadata，只能等 metadata 到達後逐檔 rename_file。libtorrent 會自行搬移
        已寫入的少量資料，不會留下孤兒檔。
        """
        renamed = renamed_files_for(ti, self._root_name)
        if not renamed:
            return
        h = self._line.handle
        if h is None or not h.is_valid():
            return
        for idx, new_path in renamed.items():
            try:
                h.rename_file(idx, new_path)
            except Exception as e:
                logger.warning('BT 改名失敗 index=%s -> %s: %s', idx, new_path, e)

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
                'root_name': self._root_name,
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
        root = self._work_root
        if not root:
            # 任務尚未 start()（例如重開後掃描回來的 paused 任務）時 _work_root 為
            # None；改用 source 的 info hash 推導 .bt_tmp 目錄，確保刪除時連同
            # task.json 一起清掉，否則重開後 scan_unfinished_tasks() 會再掃回同一任務。
            ih = getattr(self, '_info_hash_hex', None) or bt_info_hash(self.source)
            if ih:
                root = os.path.join(self.save_dir, '.bt_tmp', ih)
        if not root:
            return
        try:
            import shutil
            shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass

    def _remove_partial_files(self):
        """刪除尚未下載完成時寫在 save_path 下的部分檔案/目錄。

        libtorrent 以 storage_mode_sparse 直接把資料寫進 self.save_dir（單檔種子
        落在 save_dir/<filename>，多檔種子落在 save_dir/<filename>/ 目錄），不像
        HTTP 任務有獨立的 .downloading 暫存檔。只清未完成的部分資料：下載完成/
        做種中的檔案已是成品，交由歷史記錄的「連同檔案刪除」處理，不在取消時刪除。
        """
        path = self.filepath
        if not path or not os.path.exists(path):
            return
        try:
            if os.path.isdir(path):
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except Exception as e:
            logger.warning('刪除 BT 未完成檔案失敗: %s - %s', path, e)

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
            # libtorrent 2.1.1 在 resume 時只要 have_pieces 非空就會「信任」那些
            # piece 為已完成、跳過 hash 校驗（實測：磁碟上損壞的 piece 也照樣秒跳
            # seeding）。save_resume_data 又不存 verified_pieces，所以刻意把
            # have_pieces / verified_pieces 都清空，強制 libtorrent 以
            # checking_files 從磁碟重新做 hash 校驗：只有真正通過的 piece 才視為
            # 完成、壞掉或缺漏的會自動重下。這才能保證「任務 100% = 檔案真正完整」。
            rp.have_pieces = []
            rp.verified_pieces = []
            rp.download_limit = self.download_rate_limit
            self._ti = ti
            self._num_pieces = ti.num_pieces()
            self._total_size = self._wanted_size(ti)
            self._info_hash_hex = _hash_hex(ti.info_hashes())
            if self._info_hash_hex:
                self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)
            # 續傳沿用既有根名：_work_root 已存在 → _resolve_root_name 不會加後綴，
            # 否則改名後 libtorrent 找不到舊資料，進度歸零重下。
            self.filename = self._resolve_root_name(ti)
            self.filepath = os.path.join(self.save_dir, self.filename)
            self._mark_private(ti)
            renamed = renamed_files_for(ti, self._root_name)
            if renamed:
                rp.renamed_files = renamed
            logger.info('BT 以 resume data 續傳（重新 hash 校驗）: %s', self.filename)
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

    def _pieces_path(self):
        return os.path.join(self._work_root, 'pieces.json') if self._work_root else None

    def _persist_merged_pieces(self, merged):
        """把合併的 piece 完成位元圖存成 pieces.json（多線路 resume 用）。"""
        path = self._pieces_path()
        if not path:
            return
        try:
            os.makedirs(self._work_root, exist_ok=True)
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump([bool(b) for b in merged], f)
            os.replace(tmp, path)
        except Exception as e:
            logger.debug('保存合併 piece 位元圖失敗: %s', e)

    def _load_merged_pieces(self):
        """讀回 pieces.json 的合併位元圖；不存在或損壞回傳 None。"""
        path = self._pieces_path()
        if not path or not os.path.isfile(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, list):
                return None
            return [bool(b) for b in data]
        except Exception as e:
            logger.debug('載入合併 piece 位元圖失敗: %s', e)
            return None

    def _assign_remaining(self, merged_have):
        """依合併完成位元圖分派剩餘 piece：完成者 owner=-1，其餘平均分給各線路。

        回傳 (piece_owner, have)。have 直接回傳傳入的位元圖；piece_owner 長度同
        num_pieces，已完成 piece 為 -1，剩餘 piece 依序（連續區段）分給各線路。
        """
        n = self._num_pieces
        owner = [-1] * n
        remaining = [i for i in range(n) if not merged_have[i]]
        if remaining:
            ranges = partition_ranges(len(remaining), len(self._lines))
            for line_idx, (lo, hi) in enumerate(ranges):
                for p in remaining[lo:hi]:
                    owner[p] = line_idx
        return owner, merged_have

    def _initial_piece_owner(self):
        """決定初始 piece 所有權表（piece index -> 線路 index）。

        優先用上次多線下載留下的合併完成位元圖（pieces.json）：只把「剩餘」
        piece 平均分派給各線，讓重啟後的重下工作起步即均衡；「已完成」piece 也
        平均掛給各線——不設 have_pieces、交由 libtorrent 對磁碟重新 hash 校驗後，
        有效的 piece 不重下、缺漏/損壞的由所屬線路自動補下（掛線是為了避免上次
        位元圖誤記、重校後變成無人接手的孤兒）。沒有可用位元圖時退回全新靜態分片。
        """
        n = self._num_pieces
        num_lines = len(self._lines)
        merged = self._load_merged_pieces()
        if merged and len(merged) == n and any(merged):
            owner = [-1] * n
            remaining = [i for i in range(n) if not merged[i]]
            done = [i for i in range(n) if merged[i]]
            if remaining:
                ranges = partition_ranges(len(remaining), num_lines)
                for li, (lo, hi) in enumerate(ranges):
                    for p in remaining[lo:hi]:
                        owner[p] = li
            for j, p in enumerate(done):
                owner[p] = j % num_lines
            return owner
        ranges = partition_ranges(n, num_lines)
        owner = []
        for li, (lo, hi) in enumerate(ranges):
            owner.extend([li] * (hi - lo))
        return owner

    def _validate_resume(self, merged):
        """若目標檔案不存在（被刪除/移動），回傳 None 放棄續傳；否則回傳位元圖。"""
        if not self.filepath or not os.path.exists(self.filepath):
            return None
        return merged

    def _resume_have_from(self, merged):
        """magnet 多線路 resume 決策：依合併位元圖建 resume 計畫。

        回傳 (piece_owner, have, resume_bytes)；輸入為 None、空、長度不符、或
        完全沒有已完成 piece（無續傳價值）時回傳 None。resume_bytes 為已完成
        piece 的總位元組（僅供進度回報，不等於已落盤資料量）。
        """
        if not merged or len(merged) != self._num_pieces:
            return None
        if not any(merged):
            return None
        if self._ti is None:
            return None
        piece_owner, have = self._assign_remaining(merged)
        resume_bytes = sum(
            self._ti.piece_size(i) for i in range(self._num_pieces) if merged[i])
        return piece_owner, have, resume_bytes

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
        p.download_limit = self.download_rate_limit
        renamed = renamed_files_for(ti, self._root_name)
        if renamed:
            p.renamed_files = renamed
        return p

    def _make_params_magnet(self):
        p = lt.parse_magnet_uri(self.source)
        p.save_path = self.save_dir
        p.storage_mode = lt.storage_mode_t.storage_mode_sparse
        p.flags = lt.torrent_flags.auto_managed
        p.download_limit = self.download_rate_limit
        _merge_http_tracker_fallback(p)
        return p

    def set_download_limit(self, rate):
        """設定本任務的下載限速（bytes/s，0 = 不限），立即套用到已加入的 session。

        全域限速由 DownloadManager 依活躍任務數均分後呼叫；libtorrent 的限速是
        逐 torrent（含其所有 peer 連線）計算，正好對應「每個任務一份額度」。
        """
        rate = max(0, int(rate or 0))
        self.download_rate_limit = rate
        for line in self._lines:
            h = line.handle
            if h is None or not h.is_valid():
                continue
            try:
                h.set_download_limit(rate)
            except Exception as e:
                logger.debug('設定 BT 下載限速失敗 %s: %s', line.key, e)

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

    def _priorities_for(self, line_idx, owner):
        return [1 if owner[i] == line_idx else 0 for i in range(self._num_pieces)]

    def _apply_owner_priorities(self, line_idx):
        """依所有權表重設某條線路的 piece 優先權。"""
        line = self._lines[line_idx]
        if line.handle is None or not line.handle.is_valid():
            return
        priorities = [
            1 if (i < len(self._piece_owner) and self._piece_owner[i] == line_idx) else 0
            for i in range(self._num_pieces)
        ]
        try:
            line.handle.prioritize_pieces(priorities)
        except Exception as e:
            logger.warning('套用 piece 優先權失敗: %s', e)

    def _rebalance(self, merged_done):
        """動態重新派工：閒置線路接手最忙碌線路的一半剩餘 piece，解決靜態分片負載不均。"""
        if not self._can_multi() or self._num_pieces <= 0:
            return
        if len(self._piece_owner) != self._num_pieces or all(merged_done):
            return
        active_idx = [
            i for i, line in enumerate(self._lines)
            if line in self._active_lines
            and line.handle is not None and line.handle.is_valid()
        ]
        if len(active_idx) < 2:
            return
        remaining = {
            i: [p for p in range(self._num_pieces)
                if self._piece_owner[p] == i and not merged_done[p]]
            for i in active_idx
        }
        # 剛被卡死救援搬走 piece 的線路不算閒置：它只是「手上沒東西」而不是
        # 「有餘力」。少了這個冷卻，救援把 piece 搬給健康線路後，_rebalance 下一輪
        # 就把同一條卡死線路視為閒置、再把 piece 發回去，兩者無限互抵，
        # 多線任務會固定卡在尾端（實測 2 MB 種子停在 2016 KB）。
        now_ts = time.time()
        idle = [
            i for i in active_idx
            if not remaining.get(i)
            and now_ts - self._line_rescued_at.get(self._lines[i].key, 0.0)
            >= BT_RESCUE_COOLDOWN
        ]
        # 只要還有剩餘 piece 就能當 donor。過去要求 len(ps) > 1，於是「只剩最後
        # 一塊」的線路永遠不會被閒置線路接手（split = 1 // 2 = 0 直接 return），
        # 多線任務就固定卡在「總大小 - 1 piece」。剩 1 塊時整塊搬過去。
        donors = [(i, ps) for i, ps in remaining.items() if ps]
        if not idle or not donors:
            return
        donor_idx, donor_pieces = max(donors, key=lambda t: len(t[1]))
        thief_idx = idle[0]
        if len(donor_pieces) == 1:
            stolen = list(donor_pieces)
        else:
            split = len(donor_pieces) // 2
            stolen = donor_pieces[split:]
        for p in stolen:
            self._piece_owner[p] = thief_idx
        self._apply_owner_priorities(donor_idx)
        self._apply_owner_priorities(thief_idx)
        logger.debug('BT 重新派工：線路 %d -> %d 搬移 %d piece',
                     donor_idx, thief_idx, len(stolen))

    def _active_line_indices(self):
        """回傳目前有有效 torrent handle 的線路 index（與 _piece_owner 同座標系）。"""
        return [
            i for i, line in enumerate(self._lines)
            if line in self._active_lines
            and line.handle is not None and line.handle.is_valid()
        ]

    def _remaining_pieces_for(self, line_idx, merged_done):
        """某線路仍未完成（不在 merged_done）且歸它所有的 piece 清單。"""
        return [
            p for p in range(self._num_pieces)
            if p < len(self._piece_owner) and self._piece_owner[p] == line_idx
            and not merged_done[p]
        ]

    def _stall_rescue(self, line_progress, merged_done, now):
        """偵測卡死線路，把它的剩餘 piece 重派給仍在前進的線路。

        line_progress: {line_idx: (total_wanted_done, download_rate)}，由協調迴圈
        本次聚合的狀態快照產生。一條線路若仍握有未完成 piece、但連續
        _stall_timeout_for(...) 秒內既沒完成新 piece、也沒收到任何 block，視為
        卡死；把它的剩餘 piece 平均攤給其餘「有進度」的線路。所有線路都卡死時
        （swarm 已無來源）不搬動，避免無謂抖動。回傳是否有搬移。
        """
        if not self._can_multi() or self._num_pieces <= 0:
            return False
        # 只在真正下載中才做卡死偵測；暫停時時間照跑、進度凍結，會誤判卡死。
        if self.status != 'downloading':
            return False

        remaining_total = sum(1 for p in range(self._num_pieces) if not merged_done[p])
        timeout = _stall_timeout_for(remaining_total)

        stalled = []
        for line_idx in self._active_line_indices():
            line = self._lines[line_idx]
            if not self._remaining_pieces_for(line_idx, merged_done):
                # 已無 own piece，清掉卡死狀態
                self._line_stall_since.pop(line.key, None)
                continue
            prev = self._line_progress_snapshot.get(line.key)
            cur_done, cur_rate = line_progress.get(line_idx, (None, 0))
            self._line_progress_snapshot[line.key] = cur_done
            since, is_stalled = _update_stall(prev, cur_done, cur_rate,
                                              self._line_stall_since.get(line.key),
                                              now, timeout)
            if is_stalled:
                stalled.append(line_idx)
            if since is None:
                self._line_stall_since.pop(line.key, None)
            else:
                self._line_stall_since[line.key] = since

        if not stalled:
            return False

        stalled_set = set(stalled)
        healthy = [i for i in self._active_line_indices() if i not in stalled_set]
        if not healthy:
            return False

        moved = 0
        for s in stalled:
            self._line_stall_since.pop(self._lines[s].key, None)
            self._line_rescued_at[self._lines[s].key] = now
            pieces = self._remaining_pieces_for(s, merged_done)
            for k, p in enumerate(pieces):
                self._piece_owner[p] = healthy[k % len(healthy)]
                moved += 1
        for i in stalled_set | set(healthy):
            self._apply_owner_priorities(i)
        if moved:
            logger.info('BT 卡死救援：線路 %s 的 %d 塊剩餘 piece 重派給 %s',
                        sorted(stalled), moved, healthy)
        return moved > 0

    def _fan_out(self):
        """metadata 已知後，把 piece 空間分片給各 session（主要供 magnet 延遲扇出）。

        不帶 have_pieces：讓 libtorrent 以 checking_files 對磁碟上的既有資料重新做
        hash 校驗（已正確的 piece 不重下、損壞/缺漏的由負責線路補下），從根本保證
        「任務 100% = 檔案真正完整」。primary 已在單線路階段加入以取得 metadata，
        此處僅需重設其 piece 優先權並加入其餘線路。
        """
        if self._fanned_out or not self._can_multi() or self._ti is None:
            return
        self._fanned_out = True

        # 決定 piece 所有權：優先用上次的合併位元圖只分派剩餘 piece（重校後
        # 有效不重下、損壞補下），沒有位元圖則全新靜態分片。
        self._piece_owner = self._initial_piece_owner()

        # primary 已在單線路階段加入，動態改其 piece 優先權
        try:
            self._line.handle.prioritize_pieces(self._priorities_for(0, self._piece_owner))
        except Exception as e:
            logger.warning('重設 primary piece 優先權失敗: %s', e)

        # 其餘線路逐一加入同一 torrent（共用 save_path，piece 不重疊）
        for i in range(1, len(self._lines)):
            line = self._lines[i]
            params = self._make_params_with_ti(self._ti)
            params.piece_priorities = self._priorities_for(i, self._piece_owner)
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
        # 重設卡死偵測狀態：避免上次執行殘留的快照／時間戳干擾新一輪判斷。
        self._line_progress_snapshot = {}
        self._line_stall_since = {}
        self._line_rescued_at = {}
        # 重新計時放棄判定，避免沿用上一輪的停滯時間
        self._give_up_done = None
        self._give_up_since = None

        if self.kind == 'torrent':
            try:
                ti = lt.torrent_info(self.source)
            except Exception as e:
                self.status = 'error'
                self.error_message = f'解析 .torrent 失敗: {e}'
                return False
            self._ti = ti
            self._num_pieces = ti.num_pieces()
            self._init_piece_progress()
            self._total_size = self._wanted_size(ti)
            self._info_hash_hex = _hash_hex(ti.info_hashes())
            if self._info_hash_hex:
                self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)
            # 目錄已存在 = 上次留下的續傳狀態；必須在 _save_state() 建立目錄前判斷，
            # 否則新任務也會被誤認成續傳而沿用 magnet 的顯示名。
            self._resuming = bool(self._work_root and os.path.isdir(self._work_root))
            # 先解析根名再建 params：同名種子要在「加入前」就用 renamed_files 指定好
            # 路徑，加入後才改等於先建一批同名檔案再搬。_resolve_root_name 需要
            # _work_root 判斷是否為續傳，所以 info hash 必須先算。
            self.filename = self._resolve_root_name(ti)
            self.filepath = os.path.join(self.save_dir, self.filename)
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
        # 同 torrent 分支：目錄存在才算續傳，且必須在 _save_state() 之前判斷。
        self._resuming = bool(self._work_root and os.path.isdir(self._work_root))

        # resume data 續傳僅用於單線路；多線路不帶 have_pieces，交由 libtorrent 的
        # checking_files 從磁碟重新校驗（見下方 torrent 分支註解）
        if not self._can_multi():
            params = self._try_load_resume(params)

        if self._info_hash_hex:
            self._save_state()

        if self.kind == 'torrent' and self._can_multi():
            # 公開種子 + 整包：決定 piece 所有權（優先用上次的合併位元圖，只把
            # 剩餘 piece 平均分給各線），不帶 have_pieces。libtorrent 會以
            # checking_files 對磁碟上的既有資料重新做 hash 校驗：已正確的 piece
            # 不重下、損壞/缺漏的由負責線路補下。這是唯一能保證「回報 100% 一定
            # 代表檔案真正完整」的 resume 方式。
            self._piece_owner = self._initial_piece_owner()
            self._fanned_out = True
            for i, line in enumerate(self._lines):
                p = self._make_params_with_ti(self._ti)
                p.piece_priorities = self._priorities_for(i, self._piece_owner)
                try:
                    line.add_torrent(p)
                    self._active_lines.append(line)
                except Exception as e:
                    if i == 0:
                        self.status = 'error'
                        self.error_message = f'啟動 BT 下載失敗: {e}'
                        return False
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
            if not self._can_multi():
                try:
                    h.save_resume_data(
                        lt.save_resume_flags_t.flush_disk_cache | lt.save_resume_flags_t.save_info_dict)
                except Exception:
                    pass
            try:
                h.pause()
            except Exception as e:
                logger.warning('BT pause 失敗: %s', e)
        # 不在這裡同步等 save_resume_data_alert。pause() 由 UI 執行緒呼叫，過去等
        # 3 秒等於整個視窗凍結（實測固定 3.03 秒）；而且 alert 幾乎都被協調迴圈的
        # _collect_block_progress 先收走並持久化，這裡根本等不到。
        # resume data 交給協調迴圈處理，task.json 由下面立即寫入。
        self._save_state()
        return True

    def resume(self):
        if self.status not in ('paused', 'initialized'):
            return False
        if not self._active_lines:
            return self.start()
        self.status = 'seeding' if self._download_completed_at is not None else 'downloading'
        # 重設卡死計時：暫停期間時間照跑、進度凍結，避免恢復後立即誤判卡死。
        self._line_progress_snapshot = {}
        self._line_stall_since = {}
        self._line_rescued_at = {}
        # 重新計時放棄判定，避免沿用上一輪的停滯時間
        self._give_up_done = None
        self._give_up_since = None
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
        # 尚未下載完成（未進入做種/已完成）時，把已下載的部分檔案一併刪除，呼應
        # 刪除對話框「已下載的暫存資料也會一併刪除」；完成/做種中的檔案是成品，
        # 交由歷史記錄「連同檔案刪除」處理。
        if self._download_completed_at is None:
            self._remove_partial_files()
            # 檔案已刪，順便釋放根名登記，後續同名種子才能用回原名（否則會一直
            # 往上加 " (1)"、" (2)"）。已完成的任務保留登記：檔案還在磁碟上。
            self._release_root_claim()
        self._cleanup_work()
        self.status = 'canceled'
        return True

    # ------------------------------------------------------------------ #
    # 協調迴圈
    # ------------------------------------------------------------------ #
    def _on_metadata(self, ti):
        self._ti = ti
        self._num_pieces = ti.num_pieces()
        self._init_piece_progress()
        self._total_size = self._wanted_size(ti)
        self._mark_private(ti)
        if not self._info_hash_hex:
            self._info_hash_hex = _hash_hex(ti.info_hashes())
            self._work_root = os.path.join(self.save_dir, '.bt_tmp', self._info_hash_hex)
        # magnet 加入時還沒有 metadata，無法預先用 params.renamed_files 指定路徑，
        # 只能等 metadata 到達後逐檔 rename_file（此時資料量極少，搬移成本可忽略）。
        self.filename = self._resolve_root_name(ti)
        self.filepath = os.path.join(self.save_dir, self.filename)
        self._apply_root_rename(ti)
        self._save_state()
        self._fan_out()

    def _check_give_up(self, total_done):
        """長時間完全沒有進度時主動放棄（HTTP 有停滯判定，BT 過去會永遠掛著）。

        只看「位元組有沒有增加」而不是速率：慢速 swarm 也可能只有幾 KB/s，用速率
        會誤殺；但連續 30 分鐘一個位元組都沒拿到，實務上就是已經沒有可用來源。
        放棄時保留 .bt_tmp 與已下載資料，使用者按「重試」可從原進度繼續。
        """
        if self.status != 'downloading':
            return
        now = time.time()
        if self._give_up_done is None or total_done > self._give_up_done:
            self._give_up_done = total_done
            self._give_up_since = now
            return
        if self._give_up_since is None:
            self._give_up_since = now
            return
        idle = now - self._give_up_since
        if idle < BT_GIVE_UP_SECONDS:
            return
        logger.warning('BT 任務連續 %d 秒沒有任何進度，放棄: %s', int(idle), self.filename)
        self.error_message = (
            '連續 %d 分鐘沒有收到任何資料，可能已無可用來源（peers 全部離線）。'
            '可按「重試」重新嘗試。' % (BT_GIVE_UP_SECONDS // 60))
        self.status = 'error'
        # 只暫停 handle、不結束協調迴圈：retry() 會重建 handle 從原進度繼續
        for line in self._active_lines:
            h = line.handle
            if h is None:
                continue
            try:
                h.pause()
            except Exception:
                pass

    def _coordinator_loop(self):
        while not self._stop.is_set():
            try:
                for line in self._active_lines:
                    line.session.post_torrent_updates()
                self._collect_block_progress()

                # resume data 定期保存：僅單線路（多線路留待後續）
                if not self._can_multi():
                    now = time.time()
                    if now - self._last_resume_save >= self._resume_interval:
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

                # 暫停期間完全凍結：不做 metadata 扇出、peer 分享、動態派工與完成偵測。
                # 過去只對「按下暫停當下已存在的 handle」呼叫 pause()，若 magnet 在暫停
                # 之後才收到 metadata，_fan_out() 會新建未暫停的 session，於是畫面顯示
                # 「已暫停」卻繼續下載（實測暫停後仍從 0% 長到 21%）。
                if self.status == 'paused':
                    with self._lock:
                        self._last_speed = 0
                        self._last_upload = 0
                    time.sleep(0.5)
                    continue

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
                num_pieces = self._num_pieces
                merged_done = [False] * num_pieces
                # line_idx -> (total_wanted_done, download_rate)，供卡死偵測判斷
                # 各線路是否有實質進度（完成新 piece，或正在收 block）。
                line_progress = {}

                for line_idx, line in enumerate(self._lines):
                    if line not in self._active_lines:
                        continue
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
                    line_progress[line_idx] = (done, st.download_rate)
                    # st.pieces 是「每 piece 一個 0/1」的 list（libtorrent 的 bitfield
                    # 實作），不是 packed byte bitfield；逐 piece OR 進合併位元圖即可，
                    # 不能再用 _bit 去拆 byte（否則多線路下會把位元圖讀錯）。
                    bits = getattr(st, 'pieces', None)
                    if bits:
                        for i in range(num_pieces):
                            if i < len(bits) and bits[i]:
                                merged_done[i] = True

                pieces_frac = [1.0 if d else 0.0 for d in merged_done]

                # 跨線路 peer 共享：把「能靠 DHT/tracker 找到 peer 的線路」拿到的
                # peer 位址餵給其他線路（尤其是 enable_dht=False 的 SOCKS5 代理
                # 線路），讓它跳過 DHT 直接按 IP:port 建立 TCP 連線。對無 tracker
                # 的 DHT-only 種子特別有用；連線會因 force_proxy 走代理出去。
                if self._can_multi() and len(self._active_lines) > 1 \
                        and time.time() - self._last_peer_share >= 5.0:
                    self._last_peer_share = time.time()
                    all_peers = set()
                    for line in self._active_lines:
                        h = line.handle
                        if h is None or not h.is_valid():
                            continue
                        try:
                            for p in h.get_peer_info():
                                ip = p.ip[0]
                                port = int(p.ip[1])
                                # 過濾空/未知位址；尚未連線的 peer 會回 ('0.0.0.0', 0)
                                if not ip or ip in ('0.0.0.0', '::') or port <= 0:
                                    continue
                                all_peers.add((ip, port))
                        except Exception:
                            pass
                    for line in self._active_lines:
                        h = line.handle
                        if h is None or not h.is_valid():
                            continue
                        try:
                            current = {(p.ip[0], int(p.ip[1]))
                                       for p in h.get_peer_info()}
                        except Exception:
                            current = set()
                        missing = [ep for ep in all_peers
                                   if ep not in current and ep not in self._shared_peers]
                        for ip, port in missing[:50]:
                            try:
                                h.connect_peer((ip, port), 0)
                                self._shared_peers.add((ip, port))
                            except Exception:
                                pass
                    # 防 set 無限成長（單顆種子 peer 數量有限，門檻僅保險用）
                    if len(self._shared_peers) > 5000:
                        self._shared_peers.clear()

                # 動態重新派工（僅多線路）：同一協調週期內先做卡死救援、再做靜態均衡。
                # 卡死救援：進度停滯的線路把剩餘 piece 讓給仍在前進的線路，避免
                #   慢速／斷連線路把任務拖在 99.x%；收尾階段用更短時間窗加速救援。
                # 靜態均衡：閒置線路接手最忙碌線路的一半剩餘 piece，平衡負載。
                now = time.time()
                if self._can_multi() and num_pieces > 0 and now - self._last_rebalance >= 3.0:
                    self._last_rebalance = now
                    self._stall_rescue(line_progress, merged_done, now)
                    self._rebalance(merged_done)

                # 多線路續傳：定期保存合併 piece 位元圖（只在確實有進度時才寫入，
                # 避免 resume 初期正在 hash 校驗、位元圖全 0 時覆蓋掉上次的續傳快照）。
                if self._can_multi() and any(merged_done) and now - self._last_resume_save >= self._resume_interval:
                    self._last_resume_save = now
                    self._persist_merged_pieces(merged_done)

                with self._lock:
                    if not self._can_multi() and total_wanted > 0:
                        self._total_size = total_wanted
                    self._total_done = total_done
                    self._line_done = line_done
                    self._last_speed = total_rate
                    self._last_upload = total_upload
                    if pieces_frac:
                        self._pieces = pieces_frac

                # 任務層級的放棄判定：swarm 真的沒來源時，過去會永遠停在 downloading
                # （HTTP 有停滯判定會報錯，BT 沒有）。
                self._check_give_up(total_done)

                # 完成偵測：一律以 libtorrent 的「verified」狀態為準。st.pieces 是
                # have 位元圖，hash 校驗尚未通過時就可能置位（甚至校驗失敗、待重下
                # 時也短暫置位），不能拿來判完成——過去用 all(merged_done) 會在檔案
                # 尚未真正通過校驗時就誤報 100%。
                # 單線路：該 session 進入 finished/seeding 即完成。
                # 多線路：每個 session 只下自己分派的 piece（其餘 priority=0），
                # 各自的 finished/seeding 只反映「自己那段全部通過校驗」；必須所有
                # active session 都 verified 才算整包真正完成。
                download_done = False
                if self._can_multi():
                    active = [ln for ln in self._active_lines
                              if ln.handle is not None and ln.handle.is_valid()]
                    download_done = bool(active) and all(
                        _verified_done(ln.handle.status()) for ln in active)
                else:
                    for line in self._active_lines:
                        h = line.handle
                        if h is None or not h.is_valid():
                            continue
                        if _verified_done(h.status()):
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

    def _init_piece_progress(self):
        """metadata 已知後，依 piece 大小與固定 block 大小建立部分進度陣列。"""
        n = self._num_pieces
        per_piece = []
        if self._ti is not None:
            per_piece = [
                (self._ti.piece_size(i) + BT_BLOCK_SIZE - 1) // BT_BLOCK_SIZE
                for i in range(n)
            ]
        with self._lock:
            self._blocks_done = [0] * n
            self._last_block_ts = [0.0] * n
            self._blocks_per_piece = per_piece

    def _collect_block_progress(self):
        """收集所有 active session 的 block_finished_alert，累算每 piece 的部分進度。

        block_finished_alert 每個 16 KiB block 完成時觸發一次，據此得到比「完成位元圖」
        更細的 piece 內完成比例；順便處理 save_resume_data_alert，避免被本方法排空後
        單線 resume 流程漏收。
        """
        if self._num_pieces <= 0:
            return
        now = time.time()
        block_hits = {}
        last_ts = {}
        resume_params = []
        for line in list(self._active_lines):
            h = line.handle
            if h is None or not h.is_valid():
                continue
            try:
                alerts = line.session.pop_alerts()
            except Exception:
                continue
            for a in alerts:
                if isinstance(a, lt.block_finished_alert):
                    pi = a.piece_index
                    if 0 <= pi < self._num_pieces:
                        block_hits[pi] = block_hits.get(pi, 0) + 1
                        last_ts[pi] = now
                elif isinstance(a, lt.save_resume_data_alert):
                    resume_params.append(a.params)
        if block_hits:
            with self._lock:
                for pi, cnt in block_hits.items():
                    self._blocks_done[pi] += cnt
                    self._last_block_ts[pi] = last_ts[pi]
        for rp in resume_params:
            self._persist_resume_data(rp)

    def _compute_piece_states(self, now=None):
        """合併完成位元圖與 block 計數，算出每 piece 的 frac（0~1）與 active。"""
        now = time.time() if now is None else now
        n = self._num_pieces
        states = []
        for i in range(n):
            if i < len(self._pieces) and self._pieces[i] >= 0.999:
                states.append({'frac': 1.0, 'active': False})
                continue
            denom = self._blocks_per_piece[i] if i < len(self._blocks_per_piece) else 0
            done_blocks = self._blocks_done[i] if i < len(self._blocks_done) else 0
            frac = min(1.0, done_blocks / denom) if denom > 0 else 0.0
            active = (
                frac < 1.0
                and done_blocks > 0
                and i < len(self._last_block_ts)
                and now - self._last_block_ts[i] <= BT_PIECE_ACTIVE_WINDOW
            )
            states.append({'frac': frac, 'active': active})
        return states

    def get_progress(self):
        with self._lock:
            total = self._total_size
            done = self._total_done
            speed = self._last_speed
            upload_speed = self._last_upload
            line_done = dict(self._line_done)
            states = self._compute_piece_states()

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
            # 磁力尚未取得 metadata（種子資訊）時為 True，供 UI 顯示「等待種子資訊」
            'waiting_metadata': (self.kind == 'magnet' and self._ti is None),
            'elapsed_time': elapsed,
            'thread_count': max(1, len(self._lines)),
            'block_count': len(states),
            'blocks': _downsample_blocks(states),
            'line_bytes': line_bytes,
            'line_labels': dict(self._line_labels),
            'is_private': self.is_private,
            'seed_hours': self.seed_hours,
            'seeding_remaining': seeding_remaining,
        }
