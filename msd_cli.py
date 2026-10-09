#!/usr/bin/env python3
"""MSD 端到端測試 CLI：讓 LLM 直接操作真實視窗。

搭配 `python MultiSocksDownloader.py --test-api` 使用。所有輸出都是 JSON，
方便 LLM 直接解析；截圖會存成 PNG 並印出路徑，讓 LLM 能直接看畫面。

範例::

    python msd_cli.py windows
    python msd_cli.py widgets --window 新增下載
    python msd_cli.py click --window 新增下載 --text 確認
    python msd_cli.py screenshot --window 新增下載 --out shot.png
    python msd_cli.py state
    python msd_cli.py flood --url https://example.com/a.bin --count 5
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API_HOST = '127.0.0.1'
DEFAULT_API_PORT = 8766
DEFAULT_SERVER_PORT = 8765


def api_call(host, port, cmd, timeout=180):
    """把命令送到測試介面，回傳 (ok, payload)。"""
    url = f'http://{host}:{port}/cmd'
    body = json.dumps(cmd).encode('utf-8')
    req = urllib.request.Request(
        url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        return False, {'ok': False, 'error': f'無法連線測試介面 {url}：{e}。'
                                            '請確認 MSD 以 --test-api 啟動。'}
    except Exception as e:  # noqa: BLE001
        return False, {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def post_download(host, port, url, filename=None, resolve=False, headers=None):
    """模擬 Chrome 擴充端把下載請求 POST 給 MSD 的 8765 連接埠。"""
    endpoint = f'http://{host}:{port}/'
    payload = {
        'url': url,
        'downloadId': None,
        'filename': filename,
        'timestamp': int(time.time() * 1000),
        'headers': headers or {},
        'resolve': resolve,
    }
    body = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        endpoint, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:  # noqa: BLE001
        return {'status': 'error', 'error': f'{type(e).__name__}: {e}'}


def build_parser():
    p = argparse.ArgumentParser(
        prog='msd_cli.py',
        description='MSD 端到端測試 CLI（需搭配 --test-api 啟動的 MSD）')
    p.add_argument('--api-host', default=DEFAULT_API_HOST)
    p.add_argument('--api-port', type=int, default=DEFAULT_API_PORT)
    p.add_argument('--server-port', type=int, default=DEFAULT_SERVER_PORT,
                   help='MSD 接收 Chrome 下載請求的連接埠（預設 8765）')
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('ping', help='檢查測試介面是否在線')
    sub.add_parser('windows', help='列出目前可見的視窗')
    sub.add_parser('tasks', help='讀取主視窗任務表格')
    sub.add_parser('state', help='讀取整體狀態（對話框、佇列、任務）')
    sub.add_parser('quit', help='關閉 MSD')

    w = sub.add_parser('widgets', help='列出某視窗的控件')
    w.add_argument('--window', required=True)
    w.add_argument('--all', action='store_true', help='連純版面容器一起列出')

    def add_target_args(sp):
        sp.add_argument('--window', required=True)
        sp.add_argument('--text', help='用畫面文字指定控件（按鈕／選項）')
        sp.add_argument('--attr', help='用視窗屬性名稱指定控件（如 name_edit）')
        sp.add_argument('--object-name', dest='objectName', help='用 objectName 指定控件')
        sp.add_argument('--std-button', dest='stdButton',
                        help='QMessageBox 標準按鈕：ok/yes/no/cancel/close/...')
        sp.add_argument('--index', type=int, default=0, help='多個符合時取第幾個')

    c = sub.add_parser('click', help='點擊控件（送出真實滑鼠事件）')
    add_target_args(c)

    s = sub.add_parser('set-text', help='直接設定輸入框文字（不經鍵盤）')
    add_target_args(s)
    s.add_argument('--value', required=True)

    t = sub.add_parser('type-text', help='模擬鍵盤逐字輸入（真人路徑）')
    add_target_args(t)
    t.add_argument('--value', required=True)

    k = sub.add_parser('key', help='送出按鍵（return/escape/tab/down/...）')
    add_target_args(k)
    k.add_argument('--key', default='return')

    f = sub.add_parser('focus', help='把視窗帶到前景')
    f.add_argument('--window', required=True)

    sc = sub.add_parser('screenshot', help='視窗截圖存成 PNG，印出檔案路徑')
    sc.add_argument('--window', required=True)
    sc.add_argument('--out', help='輸出檔案路徑（預設存到暫存目錄）')

    cw = sub.add_parser('close-window', help='關閉對話框（等同按取消）')
    cw.add_argument('--window', required=True)

    ww = sub.add_parser('wait-window', help='等待某個視窗出現')
    ww.add_argument('--title', required=True)
    ww.add_argument('--timeout', type=float, default=10)

    pd = sub.add_parser('post-download',
                        help='模擬 Chrome 擴充端送一筆下載請求給 MSD')
    pd.add_argument('--url', required=True)
    pd.add_argument('--filename')
    pd.add_argument('--resolve', action='store_true')

    fl = sub.add_parser('flood',
                        help='模擬 Chrome 啟動時重播歷史：連續送 N 筆相同請求')
    fl.add_argument('--url', required=True)
    fl.add_argument('--filename')
    fl.add_argument('--count', type=int, default=5)
    fl.add_argument('--interval', type=float, default=0.05)
    fl.add_argument('--resolve', action='store_true')

    return p


def emit(payload, ok=True):
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == 'post-download':
        res = post_download(args.api_host, args.server_port, args.url,
                            filename=args.filename, resolve=args.resolve)
        return emit(res, res.get('status') in ('received', 'success'))

    if cmd == 'flood':
        results = []
        for i in range(max(1, args.count)):
            res = post_download(args.api_host, args.server_port, args.url,
                                filename=args.filename, resolve=args.resolve)
            results.append(res.get('status') or res.get('error'))
            time.sleep(max(0.0, args.interval))
        # 送完後等一下讓 UI 反應，再回報狀態，方便直接判斷有沒有視窗風暴。
        time.sleep(1.0)
        ok, state = api_call(args.api_host, args.api_port, {'action': 'state'})
        return emit({
            'sent': len(results),
            'responses': results,
            'state': state.get('result') if ok and state.get('ok') else state,
        })

    if cmd == 'ping':
        ok, res = api_call(args.api_host, args.api_port, {'action': 'ping'}, timeout=5)
        return emit(res, ok and res.get('ok', False))

    # 其餘命令都是測試介面的直接映射
    payload = {'action': cmd.replace('-', '_')}
    for key in ('window', 'text', 'attr', 'objectName', 'stdButton', 'index',
                'value', 'key', 'out', 'title', 'timeout', 'all'):
        if hasattr(args, key):
            value = getattr(args, key)
            if value is not None and value is not False:
                payload[key] = value

    if cmd == 'wait-window':
        payload['action'] = 'wait_window'

    ok, res = api_call(args.api_host, args.api_port, payload)
    return emit(res, ok and res.get('ok', False))


if __name__ == '__main__':
    sys.exit(main())
