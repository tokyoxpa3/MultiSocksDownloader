"""FTP 下載支援：讓控制通道與資料通道都走 SOCKS5 代理。

ftplib 預設只支援直連（socket.create_connection），本模組以 SocksFTP
子類別把「控制連線」與「被動模式資料連線」兩個建立點改走 PySocks 的
socks.create_connection，使每個 FTP 連線都能個別綁定一條 SOCKS5 線路，
達成多線路聚合（家用 PPPoE 直連 + 手機 Socks5 server 各為一條線）。

設計要點：
- 只支援被動模式（PASV），主動模式（PORT）直接拒絕。
- makepasv() 一律以使用者提供的 FTP 主機為資料連線目標，而非
  getpeername()：直連時兩者等價（即 ftplib 的 NAT 修補），
  SOCKS 時 getpeername() 會回傳 proxy 位址，故必須用 self.host，
  交由代理（proxy_rdns=True）做遠端 DNS 解析並連線。
"""

import socket
import ftplib
from ftplib import parse227, parse229, parse150, error_reply, error_perm, Error
from urllib.parse import urlparse, unquote

import socks


def parse_ftp_url(url):
    """解析 FTP URL，回傳 (host, port, user, passwd, path)。"""
    parsed = urlparse(url)
    if parsed.scheme.lower() != 'ftp':
        raise ValueError(f"不是 FTP URL: {url}")
    host = parsed.hostname or ''
    port = parsed.port or 21
    user = unquote(parsed.username) if parsed.username else ''
    passwd = unquote(parsed.password) if parsed.password else ''
    path = unquote(parsed.path) or '/'
    if not host:
        raise ValueError(f"FTP URL 缺少主機: {url}")
    return host, port, user, passwd, path


class SocksFTP(ftplib.FTP):
    """控制與資料通道都透過 SOCKS5 代理（proxy=None 時直連）的 FTP 用戶端。

    proxy 結構：{'host', 'port', 'username', 'password'}，與下載管理器的
    代理設定一致。username/password 可為空字串或省略。
    """

    def __init__(self, proxy=None, timeout=30.0):
        self._proxy = proxy
        super().__init__()
        self.timeout = timeout
        self.passiveserver = True   # 強制被動模式

    def _make_socket(self, host, port):
        """建立一條連線：有代理走 SOCKS5，否則直連。"""
        if self._proxy:
            p = self._proxy
            return socks.create_connection(
                (host, port),
                timeout=self.timeout,
                proxy_type=socks.SOCKS5,
                proxy_addr=p['host'],
                proxy_port=int(p['port']),
                proxy_rdns=True,   # 交由代理解析目標位址（SOCKS5 遠端 DNS）
                proxy_username=p.get('username') or None,
                proxy_password=p.get('password') or None,
            )
        return socket.create_connection((host, port), self.timeout)

    def connect(self, host='', port=0, timeout=-999, source_address=None):
        """建立控制連線（透過代理/直連）。"""
        if host != '':
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        if self.timeout is not None and not self.timeout:
            raise ValueError('Non-blocking socket (timeout=0) is not supported')
        self.sock = self._make_socket(self.host, self.port)
        self.af = self.sock.family
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def makepasv(self):
        """取得資料連線的 (host, port)：優先 EPSV、不支援時回退 PASV。

        不採用 ftplib 依 self.af 分支的寫法：SOCKS5 下 self.sock.family
        反映的是 proxy 位址的家族（幾乎都是 IPv4），無法代表 FTP 主機的
        真實家族。EPSV 在 IPv4 與 IPv6 皆通用，且 IPv6 控制連線強制要求
        EPSV，故優先嘗試；僅在伺服器不支援 EPSV 時回退 PASV。

        無論哪條路徑，資料連線一律以使用者提供的 self.host 為目標
        （而非 PASV/EPSV 回報的位址），交由 SOCKS 代理（proxy_rdns=True）
        解析連線，同時涵蓋內網 IP 的 NAT 修補。
        """
        try:
            _host, port = parse229(self.sendcmd('EPSV'), self.sock.getpeername())
        except Error:
            _host, port = parse227(self.sendcmd('PASV'))
        return self.host, port

    def ntransfercmd(self, cmd, rest=None):
        """建立資料連線並送出傳輸指令；僅支援被動模式。"""
        size = None
        if not self.passiveserver:
            raise error_perm("僅支援被動模式（PASV），不支援主動模式（PORT）")
        host, port = self.makepasv()
        conn = self._make_socket(host, port)
        try:
            if rest is not None:
                self.sendcmd("REST %s" % rest)
            resp = self.sendcmd(cmd)
            if resp[0] == '2':
                resp = self.getresp()
            if resp[0] != '1':
                raise error_reply(resp)
        except Exception:
            conn.close()
            raise
        if resp[:3] == '150':
            size = parse150(resp)
        return conn, size
