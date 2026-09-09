"""單一 logging 設定入口。

全專案只允許此處設定 logging 基礎配置。任何模組都不應在頂層呼叫
`logging.basicConfig`，否則 log 行為會隨模組匯入順序而異（這是過去
難以定位問題的來源之一）。呼叫端一律在程式入口（MultiSocksDownloader、
repro 等）的 main 開始處呼叫 `setup_logging(debug)` 一次。
"""

import logging
import sys

_LOGGING_CONFIGURED = False

# 統一格式：時間 - 層級 - logger 名稱 - 訊息。logger 名稱保留，讓跨模組的
# 「事件」log 能一眼看出發生在哪個邊界（downloader / http_server / ui …）。
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(debug=False):
    """設定全專案唯一的 logging 基礎配置。

    debug=True 時 root logger 設為 DEBUG（verbose），否則 INFO。
    重複呼叫是冪等的：只會附加一次 handler，不會累積重複輸出。
    """
    global _LOGGING_CONFIGURED
    level = logging.DEBUG if debug else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    if not _LOGGING_CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
        _LOGGING_CONFIGURED = True

    # 第三方庫太吵：urllib3 的 InsecureRequestWarning 已在 downloader.py
    # 停用，連線層的冗餘 log 也一併降噪，避免淹沒真正的診斷輸出。
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("libtorrent").setLevel(logging.WARNING)


def is_debug_enabled():
    """回傳目前 root logger 是否處於 DEBUG 層級（供需要條件輸出的程式判斷）。"""
    return logging.getLogger().isEnabledFor(logging.DEBUG)
