#!/usr/bin/env python3
"""LLM 端到端測試介面：讓自動化工具像真人一樣操作真實的 Qt 視窗。

為什麼需要它：底層函式單獨呼叫都正常，但真實使用者的操作路徑（開啟對話框、
點「確認」、關閉視窗、連續觸發）常會踩到單元測試看不到的 bug。這個介面把
「點按鈕、填欄位、截圖、讀表格」變成可被 CLI 呼叫的動作，且一律透過 Qt 事件
（QTest）送給真正的 widget，走的就是真人的路徑。

安全：預設關閉，必須在啟動時加 `--test-api` 才會開啟，且只綁 127.0.0.1。

用法::

    python MultiSocksDownloader.py --test-api --test-api-port 8766
    python msd_cli.py windows
    python msd_cli.py click --window 新增下載 --text 確認
"""

import base64
import json
import logging
import os
import queue
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QCheckBox, QComboBox, QDialog, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QWidget,
)

logger = logging.getLogger('test_api')

# 命令在 UI 執行緒執行的預設等待上限（秒）。開對話框、探測網路等可能較久。
DEFAULT_COMMAND_TIMEOUT = 20.0
# 單筆命令最長等待，避免 HTTP 連線被永久佔住。
MAX_COMMAND_TIMEOUT = 120.0

# QMessageBox 標準按鈕對照（給 --std-button 使用）
_STD_BUTTONS = {
    'ok': QMessageBox.StandardButton.Ok,
    'cancel': QMessageBox.StandardButton.Cancel,
    'yes': QMessageBox.StandardButton.Yes,
    'no': QMessageBox.StandardButton.No,
    'close': QMessageBox.StandardButton.Close,
    'discard': QMessageBox.StandardButton.Discard,
    'apply': QMessageBox.StandardButton.Apply,
    'retry': QMessageBox.StandardButton.Retry,
    'ignore': QMessageBox.StandardButton.Ignore,
}

# QTest 用的具名鍵
_NAMED_KEYS = {
    'return': Qt.Key.Key_Return,
    'enter': Qt.Key.Key_Enter,
    'escape': Qt.Key.Key_Escape,
    'tab': Qt.Key.Key_Tab,
    'space': Qt.Key.Key_Space,
    'backspace': Qt.Key.Key_Backspace,
    'delete': Qt.Key.Key_Delete,
    'down': Qt.Key.Key_Down,
    'up': Qt.Key.Key_Up,
    'left': Qt.Key.Key_Left,
    'right': Qt.Key.Key_Right,
}


# --------------------------------------------------------------------------- #
# widget 尋找與描述
# --------------------------------------------------------------------------- #
def visible_windows():
    """目前可見的最上層視窗（含對話框、QMessageBox）。"""
    return [w for w in QApplication.topLevelWidgets() if w.isVisible()]


def _window_title(window):
    try:
        title = window.windowTitle()
    except Exception:
        title = ''
    return title or ''


def find_window(title):
    """依標題找可見視窗；先精確比對，找不到再退回子字串比對。"""
    if not title:
        return None
    windows = visible_windows()
    for w in windows:
        if _window_title(w) == title:
            return w
    for w in windows:
        if title in _window_title(w):
            return w
    return None


def _attr_map(window):
    """反向索引：widget 實例 -> 在 window 上的屬性名稱。

    讓 CLI 可以用 `--attr name_edit` 這種穩定名稱指定控件，而不必依賴畫面文字。
    """
    mapping = {}
    for name, value in vars(window).items():
        if isinstance(value, QWidget):
            mapping.setdefault(id(value), name)
    return mapping


def _widget_text(widget):
    """取出控件上可顯示的文字（按鈕／標籤／輸入框內容）。"""
    for getter in ('text', 'currentText', 'toPlainText', 'windowTitle'):
        fn = getattr(widget, getter, None)
        if callable(fn):
            try:
                value = fn()
            except Exception:
                continue
            if isinstance(value, str):
                return value
    return ''


def describe_widget(widget, window, attr_map=None):
    """把 widget 描述成 JSON 可序列化的 dict。"""
    attr_map = attr_map if attr_map is not None else _attr_map(window)
    try:
        geo = widget.geometry()
        geometry = [geo.x(), geo.y(), geo.width(), geo.height()]
    except Exception:
        geometry = None
    return {
        'attr': attr_map.get(id(widget)),
        'objectName': widget.objectName(),
        'class': type(widget).__name__,
        'text': _widget_text(widget),
        'visible': widget.isVisible(),
        'enabled': widget.isEnabled(),
        'readOnly': bool(getattr(widget, 'isReadOnly', lambda: False)()),
        'geometry': geometry,
    }


def iter_widgets(window):
    yield window
    for child in window.findChildren(QWidget):
        yield child


def _interactive(widget):
    """只回傳「可以操作」的控件，過濾掉純版面容器。"""
    return isinstance(widget, (
        QAbstractButton, QLineEdit, QPlainTextEdit, QComboBox, QWidget,
    )) and widget.metaObject().className() not in (
        'QWidget', 'QFrame', 'QScrollArea', 'QSplitter', 'QStackedWidget',
    )


# --------------------------------------------------------------------------- #
# 目標解析
# --------------------------------------------------------------------------- #
class TargetError(Exception):
    pass


def resolve_target(window, cmd):
    """依 cmd 的 attr / objectName / text / stdButton 找出要操作的 widget。"""
    attr = cmd.get('attr')
    object_name = cmd.get('objectName')
    text = cmd.get('text')
    std_button = cmd.get('stdButton')
    index = int(cmd.get('index', 0) or 0)

    if std_button:
        if not isinstance(window, QMessageBox):
            raise TargetError(f'stdButton 只能用在 QMessageBox，目前視窗是 {type(window).__name__}')
        key = str(std_button).lower()
        if key not in _STD_BUTTONS:
            raise TargetError(f'未知的標準按鈕: {std_button}（可用：{", ".join(_STD_BUTTONS)}）')
        button = window.button(_STD_BUTTONS[key])
        if button is None:
            raise TargetError(f'此對話框沒有 {std_button} 按鈕')
        return button

    attr_map = _attr_map(window)

    if attr:
        for widget_id, name in attr_map.items():
            if name == attr:
                target = next(w for w in iter_widgets(window) if id(w) == widget_id)
                return target
        raise TargetError(f'視窗「{_window_title(window)}」上找不到屬性 {attr}')

    if object_name:
        matches = [w for w in iter_widgets(window) if w.objectName() == object_name]
        if not matches:
            raise TargetError(f'找不到 objectName={object_name} 的控件')
        return matches[min(index, len(matches) - 1)]

    if text is not None:
        matches = [w for w in iter_widgets(window)
                   if isinstance(w, (QAbstractButton, QCheckBox, QComboBox, QLineEdit,
                                     QPlainTextEdit))
                   and _widget_text(w) == text]
        if not matches:
            matches = [w for w in iter_widgets(window)
                       if isinstance(w, (QAbstractButton, QCheckBox))
                       and text in _widget_text(w)]
        if not matches:
            raise TargetError(f'視窗「{_window_title(window)}」上找不到文字為「{text}」的控件')
        return matches[min(index, len(matches) - 1)]

    raise TargetError('需要指定 attr、objectName、text 或 stdButton 其中一項')


# --------------------------------------------------------------------------- #
# 命令實作（一律在 UI 執行緒執行）
# --------------------------------------------------------------------------- #
def _require_window(cmd):
    title = cmd.get('window')
    if not title:
        raise TargetError('需要指定 --window')
    window = find_window(title)
    if window is None:
        titles = [_window_title(w) for w in visible_windows()]
        raise TargetError(f'找不到視窗「{title}」；目前可見視窗：{titles}')
    return window


def _bring_to_front(window):
    try:
        window.raise_()
        window.activateWindow()
    except Exception:
        pass


def _dispatch(state, cmd):
    action = cmd.get('action')

    if action == 'ping':
        return {'pong': True}

    if action == 'windows':
        return [
            {
                'title': _window_title(w),
                'class': type(w).__name__,
                'modal': bool(getattr(w, 'isModal', lambda: False)()),
                'geometry': [w.geometry().x(), w.geometry().y(),
                             w.geometry().width(), w.geometry().height()],
            }
            for w in visible_windows()
        ]

    if action == 'widgets':
        window = _require_window(cmd)
        attr_map = _attr_map(window)
        out = []
        for w in iter_widgets(window):
            if cmd.get('all') or _interactive(w):
                out.append(describe_widget(w, window, attr_map))
        return out

    if action == 'click':
        window = _require_window(cmd)
        _bring_to_front(window)
        target = resolve_target(window, cmd)
        info = describe_widget(target, window)
        if not target.isEnabled():
            raise TargetError(f'控件未啟用，無法點擊：{info}')
        # 真人的點擊路徑：透過 Qt 事件送出 press + release，會觸發 widget 的
        # mousePressEvent / clicked 訊號，與實際滑鼠一致。
        QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        return {'clicked': info}

    if action == 'set_text':
        window = _require_window(cmd)
        target = resolve_target(window, cmd)
        value = cmd.get('value', '')
        if isinstance(target, QPlainTextEdit):
            target.setPlainText(value)
        elif isinstance(target, QLineEdit):
            target.setText(value)
        else:
            raise TargetError(f'控件不支援填字：{type(target).__name__}')
        return {'set': describe_widget(target, window)}

    if action == 'type_text':
        window = _require_window(cmd)
        _bring_to_front(window)
        target = resolve_target(window, cmd)
        value = cmd.get('value', '')
        if isinstance(target, QPlainTextEdit):
            target.clear()
        elif isinstance(target, QLineEdit):
            target.selectAll()
            target.del_()
        else:
            raise TargetError(f'控件不支援輸入：{type(target).__name__}')
        QTest.keyClicks(target, value)
        return {'typed': describe_widget(target, window)}

    if action == 'key':
        window = _require_window(cmd)
        _bring_to_front(window)
        name = str(cmd.get('key', 'return')).lower()
        if name not in _NAMED_KEYS:
            raise TargetError(f'未知按鍵 {name}（可用：{", ".join(_NAMED_KEYS)}）')
        if cmd.get('attr') or cmd.get('objectName') or cmd.get('text'):
            target = resolve_target(window, cmd)
        else:
            target = window
        QTest.keyClick(target, _NAMED_KEYS[name])
        return {'key': name, 'target': describe_widget(target, window)}

    if action == 'close_window':
        window = _require_window(cmd)
        if isinstance(window, QDialog):
            window.reject()
        else:
            window.close()
        return {'closed': _window_title(window)}

    if action == 'screenshot':
        window = _require_window(cmd)
        pixmap = window.grab()
        out_path = cmd.get('out')
        if not out_path:
            shot_dir = os.path.join(tempfile.gettempdir(), 'msd_e2e_shots')
            os.makedirs(shot_dir, exist_ok=True)
            out_path = os.path.join(shot_dir, f'shot_{int(time.time() * 1000)}.png')
        else:
            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        if not pixmap.save(out_path, 'PNG'):
            raise TargetError(f'截圖儲存失敗: {out_path}')
        return {'path': os.path.abspath(out_path),
                'size': [pixmap.width(), pixmap.height()]}

    if action == 'tasks':
        main = state.get('main_window')
        if main is None:
            raise TargetError('沒有主視窗')
        return _task_rows(main)

    if action == 'state':
        main = state.get('main_window')
        if main is None:
            raise TargetError('沒有主視窗')
        manager = state.get('download_manager')
        return {
            'main_window_visible': main.isVisible(),
            'add_dialog_open': bool(getattr(main, '_add_dialog_open', False)),
            'pending_requests': len(getattr(main, '_pending_download_requests', [])),
            'task_rows': main.task_table.rowCount() if main.task_table else 0,
            'visible_windows': [_window_title(w) for w in visible_windows()],
            'manager_tasks': [
                {'task_id': t['id'], 'status': t['status'],
                 'filename': t['filename'], 'url': t['url']}
                for t in (manager.get_all_tasks() if manager else [])
            ],
        }

    if action == 'focus':
        window = _require_window(cmd)
        _bring_to_front(window)
        return {'focused': _window_title(window)}

    if action == 'quit':
        # 走應用程式自己的關閉流程（停止監控執行緒、保存進度、收起系統匣圖示），
        # 與使用者從系統匣選「結束」一致。延後執行以確保 HTTP 回應先送出去。
        main = state.get('main_window')
        if main is not None and hasattr(main, 'quit_app'):
            QTimer.singleShot(300, main.quit_app)
        else:
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(300, app.quit)
        return {'quitting': True}

    raise TargetError(f'未知動作: {action}')


def _task_rows(main):
    table = main.task_table
    rows = []
    if table is None:
        return rows
    for r in range(table.rowCount()):
        item0 = table.item(r, 0)
        row = {
            'row': r,
            'task_id': item0.data(Qt.ItemDataRole.UserRole) if item0 is not None else None,
            'filename': item0.text() if item0 is not None else '',
        }
        for col, name in enumerate(['filename', 'size', 'progress', 'status',
                                    'speed', 'eta']):
            cell = table.item(r, col)
            if cell is not None:
                row[name] = cell.text()
        bar = table.cellWidget(r, 2)
        if isinstance(bar, QProgressBar):
            row['progress_value'] = bar.value()
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# UI 執行緒橋接
# --------------------------------------------------------------------------- #
class _CommandExecutor(QObject):
    """把 HTTP 執行緒的命令排到 Qt 主執行緒執行，並把結果帶回去。

    不用 Signal 是因為需要同步等待結果；用佇列 + QTimer 輪詢最單純也最不會
    踩到跨執行緒的坑。
    """

    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.queue = queue.Queue()
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._drain)
        self.timer.start()

    def submit(self, cmd, timeout=DEFAULT_COMMAND_TIMEOUT):
        item = {'cmd': cmd, 'done': threading.Event(), 'result': None, 'error': None}
        self.queue.put(item)
        if not item['done'].wait(timeout):
            return {'ok': False, 'error': f'等待 UI 執行緒逾時（{timeout}s）'}
        if item['error']:
            return {'ok': False, 'error': item['error']}
        return {'ok': True, 'result': item['result']}

    def _drain(self):
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return
            try:
                item['result'] = _dispatch(self.state, item['cmd'])
            except TargetError as e:
                item['error'] = str(e)
            except Exception as e:  # noqa: BLE001 - 任何例外都要回報給測試端
                logger.exception("執行測試命令失敗: %s", item['cmd'])
                item['error'] = f'{type(e).__name__}: {e}'
            finally:
                item['done'].set()


# --------------------------------------------------------------------------- #
# HTTP 伺服器
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = 'MSDTestApi/1.0'

    def log_message(self, fmt, *args):
        logger.debug("test_api %s", fmt % args)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith('/ping'):
            self._json(200, {'ok': True, 'result': {'pong': True}})
            return
        self._json(404, {'ok': False, 'error': 'not found'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            raw = self.rfile.read(length).decode('utf-8') if length else '{}'
            cmd = json.loads(raw or '{}')
        except Exception as e:  # noqa: BLE001
            self._json(400, {'ok': False, 'error': f'無法解析請求: {e}'})
            return

        executor = self.server.executor

        # wait_window 在 HTTP 執行緒輪詢，避免卡住 UI 執行緒。
        if cmd.get('action') == 'wait_window':
            self._json(200, self._wait_window(executor, cmd))
            return

        timeout = min(float(cmd.get('timeout', DEFAULT_COMMAND_TIMEOUT)),
                      MAX_COMMAND_TIMEOUT)
        self._json(200, executor.submit(cmd, timeout=timeout))

    def _wait_window(self, executor, cmd):
        title = cmd.get('title') or cmd.get('window') or ''
        deadline = time.time() + min(float(cmd.get('timeout', 10)), MAX_COMMAND_TIMEOUT)
        while time.time() < deadline:
            res = executor.submit({'action': 'windows'}, timeout=10)
            if res.get('ok'):
                for w in res['result']:
                    if title and (title == w['title'] or title in w['title']):
                        return {'ok': True, 'result': w}
            time.sleep(0.1)
        return {'ok': False, 'error': f'等待視窗「{title}」逾時'}


class TestApiServer:
    """內嵌於 MSD 的測試控制伺服器。僅綁 127.0.0.1，且需以 --test-api 明確開啟。"""

    def __init__(self, main_window, download_manager, host='127.0.0.1', port=8766):
        self.state = {'main_window': main_window, 'download_manager': download_manager}
        # executor 必須在 UI 執行緒建立，因此這裡由呼叫端（主執行緒）建立。
        self.executor = _CommandExecutor(self.state, parent=main_window)
        self.host = host
        self.port = port
        self.server = None
        self.thread = None
        self.is_running = False

    def start(self):
        try:
            self.server = ThreadingHTTPServer((self.host, self.port), _Handler)
            self.server.daemon_threads = True
            self.server.executor = self.executor
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.is_running = True
            logger.info("測試介面已啟動：http://%s:%s", self.host, self.port)
            return True
        except Exception:
            logger.exception("測試介面啟動失敗")
            return False

    def stop(self):
        if not self.is_running:
            return
        try:
            self.server.shutdown()
            self.server.server_close()
            self.is_running = False
            logger.info("測試介面已停止")
        except Exception:
            logger.exception("測試介面停止失敗")


def screenshot_base64(widget):
    """備用：需要把截圖直接塞進 JSON 時使用。"""
    from PySide6.QtCore import QBuffer, QByteArray  # 延後匯入，平常不需要
    buffer = QBuffer(QByteArray())
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    widget.grab().save(buffer, 'PNG')
    return base64.b64encode(bytes(buffer.data())).decode('ascii')
