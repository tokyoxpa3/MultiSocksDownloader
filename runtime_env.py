# -*- coding: utf-8 -*-
"""runtime_env.py — 「目前是打包版還是直接跑原始碼」的唯一判斷來源。

為什麼要獨立成一個模組
    這個判斷原本在 startup / file_association / updater / downloader /
    netredirector_guard 各寫一份，v1.10.2 就是漏改其中一份（netredirector_guard
    還在看 sys.frozen），讓打包版被當成開發模式、NetRedirector 直連設定整個
    靜默略過。集中一處之後，新增模組只要 import 本模組，不會再漏。

兩個必須知道的坑
    坑 1：Nuitka 不設定 ``sys.frozen``，而是對每個「編譯過的模組」注入
        ``__compiled__`` 全域變數。所以要判斷是否為 Nuitka 打包版，只能在
        被 Nuitka 編譯的模組裡讀 ``globals()["__compiled__"]`` —— 本模組
        正是為此存在，呼叫端不要自己重寫一份。
        （Nuitka 4.1.3：``tree/Building.py`` 對每個 module provider 發出
        ``StatementAssignmentVariableName("__compiled__", ...)``；
        ``code_generation/templates/CodeTemplatesModules.py`` 另外在
        standalone 下為非套件模組 pre-seed 同名值。）
    坑 2：Nuitka standalone 會把 ``sys.executable`` 設成 dist 內建的
        python.exe（本專案的 dist 裡甚至沒有這個檔案），真正的程式 exe 要用
        ``sys.argv[0]`` 才拿得到。

建置注意
    本模組不可被 ``--nofollow-import-to`` 排除。目前 build.bat 只排除
    yt_dlp，安全；若日後新增排除規則，務必確認本模組仍在編譯範圍內，
    否則打包版會全面誤判為原始碼模式。
"""

import os
import sys


def is_frozen():
    """回傳目前是否為打包後的獨立執行檔（涵蓋 PyInstaller / Nuitka）。"""
    if getattr(sys, "frozen", False):          # PyInstaller / cx_Freeze
        return True
    if hasattr(sys, "_MEIPASS"):               # PyInstaller onefile
        return True
    if globals().get("__compiled__", False):   # Nuitka 注入的模組全域變數
        return True
    return False


def frozen_exe_path():
    """取得真正執行中的程式 exe 路徑。

    打包後 ``sys.executable`` 指向 dist 內建的 python.exe，故優先採
    ``sys.argv[0]``；兩者都不是 .exe 時（原始碼執行）回傳 argv[0] 的絕對路徑。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    p = os.path.abspath(argv0) if argv0 else ""
    if p and p.lower().endswith(".exe"):
        return p
    exe = sys.executable or ""
    if exe.lower().endswith(".exe"):
        return os.path.abspath(exe)
    return p


def process_name():
    """目前進程名（不含路徑）。

    打包後是真正的 exe 名（MultiSocksDownloader.exe），原始碼執行時是
    python.exe / pythonw.exe。需要拿進程名給外部程式比對時用這個，
    不要直接用 ``sys.executable`` —— 打包版會拿到 dist 內建的 python.exe。
    """
    exe = frozen_exe_path() if is_frozen() else (sys.executable or "")
    return os.path.basename(exe)
