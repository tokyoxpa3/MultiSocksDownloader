# -*- coding: utf-8 -*-
"""netredirector_guard.py — 啟動時確保本程式位於 NetRedirector 的直連清單。

背景
    本程式自行掌握通往各 SOCKS5 代理的連線；若同時使用 NetRedirector 做流量
    轉送，這些代理連線會被 NetRedirector 再轉一次，多代理聚合就會失效（見
    README 開頭說明）。過去只能請使用者手動把本程式加入直連清單，改為啟動時
    自動向 NetRedirector 的本機控制 API 請求。

設計原則
    - 只做一次（啟動時）。連不上就放棄：不重試、不輪詢、不留背景執行緒。
      使用者可能根本沒在用 NetRedirector，持續探測只是浪費資源。
    - 逾時極短，避免拖慢啟動。
    - 認證用共享 token（``~/.netredirector/control.token``），與 NetRedirector
      的 control_api 約定。沒有 token 檔代表對方從未啟動過，直接放棄。
    - 只有打包後的 exe 才值得自動設定：原始碼執行時進程名是 python.exe，
      把它加入直連等於讓系統上所有 Python 程式一起直連，沒有意義。
      是否為打包版的判斷交給 runtime_env（v1.10.2 曾因這裡自己看 sys.frozen
      而漏判，讓打包版被當成開發模式、設定整個沒生效）。

為什麼是本機 HTTP
    NetRedirector 需以管理員身分執行，本程式通常是一般權限；Windows 具名管道
    受完整性等級 (IL) ACL 限制，管理員行程開的管道一般權限行程常連不上，
    loopback TCP 不受此限制。
"""

import json
import logging
import os
import sys
import urllib.error
import urllib.request

import runtime_env

logger = logging.getLogger(__name__)

# 與 NetRedirector 的 control_api 約定。兩邊各自定義常數（不同專案不互相
# import），改動其一時必須同步另一邊。
CONTROL_URL = "http://127.0.0.1:8767/ensure-direct"
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".netredirector", "control.token")

# 逾時極短：這是啟動路徑上的加分功能，不值得為它延遲啟動。
DEFAULT_TIMEOUT = 0.6


def own_process_name():
    """本程式目前的進程名（NetRedirector 的規則以此比對）。

    打包成 exe 時是 ``MultiSocksDownloader.exe``；直接跑原始碼時是
    ``python.exe``（此時 NetRedirector 端會提示這會讓所有 Python 程式一起直連）。
    """
    return runtime_env.process_name()


def _read_token(path=None):
    """讀取共享 token；不存在或為空時回傳 None。

    path 為 None 時讀模組層級的 TOKEN_FILE（測試會覆寫它）。
    """
    path = path or TOKEN_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    return token or None


def ensure_direct(timeout=DEFAULT_TIMEOUT, allow_dev=False):
    """向 NetRedirector 請求把本程式加入直連清單。

    allow_dev: 直接跑原始碼時（進程名為 python.exe）預設不加入 —— python.exe
        直連會波及系統上所有 Python 程式（包含 NetRedirector 自己），沒有
        意義。只有打包後的 exe（進程名 MultiSocksDownloader.exe）才值得自動
        設定。此參數僅供測試覆寫。

    回傳 dict，``status`` 為其一：
        ``added``    已新增直連規則
        ``existed``  原本就已在直連清單
        ``skipped``  開發模式、沒在用 NetRedirector 或連不上（不算錯誤）
        ``error``    其他非預期狀況
    """
    if not runtime_env.is_frozen() and not allow_dev:
        return {"status": "skipped", "reason": "dev_mode"}

    name = own_process_name()
    if not name:
        return {"status": "error", "reason": "無法判斷進程名"}

    token = _read_token()
    if token is None:
        # 沒有 token 檔 = NetRedirector 從未啟動過，直接放棄
        return {"status": "skipped", "reason": "找不到 NetRedirector token"}

    payload = json.dumps({"target": name}).encode("utf-8")
    req = urllib.request.Request(CONTROL_URL, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-NR-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        # 連不上/逾時/回應非 JSON：視為對方沒在跑，靜默放棄
        return {"status": "skipped", "reason": str(e)}

    if data.get("added"):
        return {"status": "added", "target": name}
    if data.get("existed"):
        return {"status": "existed", "target": name}
    return {"status": "error", "reason": data.get("error") or "未知回應"}
