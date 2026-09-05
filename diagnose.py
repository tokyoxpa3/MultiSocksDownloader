#!/usr/bin/env python3
"""URL 下載診斷工具（多代理下載器）

對任意 HTTP(S) 網址執行與 MultiSocksDownloader 相同的「探測」邏輯，
報告：

  1. 重導向鏈（302 跳去哪、最終 URL 為何）
  2. 最終回應是否支援 Range（決定 App 走多線還是單線整檔）
  3. 檔案大小與檔名（Content-Disposition）
  4. 各下載線路（直連 + config.json 中狀態為「可用/有限可用」的 SOCKS5 代理）
     各自的結果——有些網站對不同出口 IP 回不同內容（被牆／geo 分流）。

用法：

  python diagnose.py <url>
  python diagnose.py <url> --config /path/to/config.json
  python diagnose.py <url> --direct-only    # 只測直連，不測代理

本工具僅依賴 requests / urllib3，不載入 libtorrent 與 GUI，可直接在
未啟動應用程式的情況下快速定位「這個網站為什麼下載異常」。

注意：擴充套件（chrome_extension）因為 MV3 host_permissions 只授權 localhost，
對站外網址發 fetch 會被 CORS 擋掉；但 requests 沒有 CORS 限制，所以本工具
才是診斷「網站實際行為」的正確位置。
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import quote, unquote

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def format_size(size_bytes):
    if size_bytes == 0:
        return "0 B"
    names = ("B", "KB", "MB", "GB", "TB")
    i = 0
    value = float(size_bytes)
    while value >= 1024 and i < len(names) - 1:
        value /= 1024
        i += 1
    return f"{value:.2f} {names[i]}"


DEFAULT_UA = {
    "User-Agent": "Multi-Socks-Downloader/1.1",
    "Accept-Encoding": "identity",
}


def _config_path(override=None):
    if override:
        return override
    return os.path.join(
        os.path.expanduser("~"), ".multi_socks_downloader", "config.json"
    )


def load_available_proxies(config_path):
    """從 config.json 讀出狀態為「可用／有限可用」的 SOCKS5 代理。

    與 DownloadManager.get_available_proxies() 的篩選邏輯一致。
    """
    if not os.path.isfile(config_path):
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return []
    proxies = cfg.get("socks_proxies") or {}
    out = []
    for p in proxies.values():
        status = p.get("status", "")
        if status.startswith("可用") or status.startswith("有限可用"):
            out.append(p)
    return out


def _proxy_url(p):
    host, port = p["host"], int(p["port"])
    user = p.get("username") or ""
    pwd = p.get("password") or ""
    if user or pwd:
        auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}"
        return f"socks5://{auth}@{host}:{port}"
    return f"socks5://{host}:{port}"


def _filename_from_cd(cd):
    """複製 DownloadTask._filename_from_headers 的 Content-Disposition 解析。"""
    if not cd:
        return None
    m = re.search(r'filename="([^"]+)"', cd) or re.search(r"filename=([^;,\s]+)", cd)
    if m:
        return m.group(1).strip('"')
    m = re.search(r"filename\*=UTF-8''([^;,\s]+)", cd)
    if m:
        return unquote(m.group(1))
    return None


def _new_session(proxy=None):
    s = requests.Session()
    s.headers.update(DEFAULT_UA)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.verify = False
    return s


def dump_redirect_chain(url, session):
    """用 GET 跟隨重導向，印出每一跳與最終 URL。"""
    r = session.get(
        url, headers=DEFAULT_UA, stream=True, timeout=(10, 20),
        allow_redirects=True, verify=False,
    )
    if r.history:
        for h in r.history:
            loc = h.headers.get("Location", "(無 Location)")
            print(f"  {h.status_code} -> {loc}")
    print(f"  最終 URL: {r.url}")
    r.close()


def probe(url, proxy=None):
    """複製 DownloadTask._probe 的探測：Range bytes=0-0 → 判 Range 支援。"""
    s = _new_session(proxy)
    headers = dict(DEFAULT_UA)
    headers["Range"] = "bytes=0-0"
    r = s.get(
        url, headers=headers, stream=True, timeout=(10, 20),
        allow_redirects=True, verify=False,
    )
    info = {
        "status": r.status_code,
        "final_url": r.url,
        "content_disposition": r.headers.get("content-disposition"),
    }
    if r.status_code == 206:
        info["supports_range"] = True
        m = re.search(r"/(\d+)\s*$", r.headers.get("content-range", ""))
        if m:
            info["total_size"] = int(m.group(1))
        if not info.get("total_size"):
            info["total_size"] = int(r.headers.get("content-length", 0) or 0)
    else:
        info["supports_range"] = False
        info["total_size"] = int(r.headers.get("content-length", 0) or 0)
    info["filename"] = _filename_from_cd(info["content_disposition"])
    r.close()
    s.close()
    return info


def print_probe(info):
    mode = "多線（支援 Range）" if info["supports_range"] else "單線整檔（不支援 Range）"
    size = (
        f"{format_size(info['total_size'])}"
        if info["total_size"]
        else "未知（回應無 Content-Length）"
    )
    print(f"  HTTP 狀態: {info['status']}")
    print(f"  最終 URL: {info['final_url']}")
    print(f"  檔案大小: {size}")
    print(f"  檔名: {info['filename'] or '(無法從 Content-Disposition 取得)'}")
    print(f"  支援 Range: {'是' if info['supports_range'] else '否'}")
    print(f"  判定模式: {mode}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="URL 下載診斷（多代理下載器）")
    ap.add_argument("url", help="要診斷的下載網址（http/https）")
    ap.add_argument(
        "--config",
        help="config.json 路徑（預設 ~/.multi_socks_downloader/config.json）",
    )
    ap.add_argument(
        "--direct-only", action="store_true", help="只測直連，不測各 SOCKS5 代理"
    )
    args = ap.parse_args(argv)

    url = args.url
    if not url.lower().startswith(("http://", "https://")):
        print("只支援 HTTP/HTTPS 網址", file=sys.stderr)
        return 1

    print(f"診斷網址: {url}\n")

    print("【1】重導向鏈（GET 跟隨）")
    s = _new_session()
    try:
        dump_redirect_chain(url, s)
    except Exception as e:
        print(f"  失敗: {e}")
    finally:
        s.close()

    print("\n【2】直連探測（Range bytes=0-0）")
    try:
        print_probe(probe(url))
    except Exception as e:
        print(f"  失敗: {e}")

    if args.direct_only:
        return 0

    proxies = load_available_proxies(_config_path(args.config))
    if not proxies:
        print(
            "\n（未找到可用代理，略過逐線路探測。可用 --config 指定設定檔。"
            "代理須先在 App 中測試為「可用」才會被納入。）"
        )
    else:
        print(f"\n【3】逐線路探測（{len(proxies)} 個可用代理）")
        for p in proxies:
            label = f"{p.get('name', '')} ({p['host']}:{p['port']})"
            print(f"\n-- {label} --")
            try:
                print_probe(probe(url, _proxy_url(p)))
            except Exception as e:
                print(f"  失敗: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
