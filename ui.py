import sys
import os
import time
import threading
import ctypes
import logging
import shutil
from collections import deque
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QMenu, QTabWidget,
    QCheckBox, QComboBox, QTreeWidget, QTreeWidgetItem, QSizePolicy,
    QScrollArea, QFrame, QSystemTrayIcon,
    QDialog, QPlainTextEdit, QStyle, QLayout
)
from PySide6.QtCore import Qt, QTimer, Signal, QThread, QSize, QEvent, QPointF, QRectF, QPoint, QRect
from PySide6.QtGui import QFont, QColor, QPainter, QPainterPath, QPen
from urllib.parse import urlparse, unquote, parse_qs

from downloader import DownloadManager, format_size, probe_url_metadata, _sanitize_filename
from stream_resolver import list_video_formats, is_direct_file
from app_icon import load_app_icon
import libtorrent as lt
from bt_downloader import torrent_file_tree, magnet_display_name
import file_association
import version
import updater

logger = logging.getLogger('ui')

# 格式化時間顯示
def format_time(seconds):
    if seconds < 60:
        return f"{seconds:.0f}秒"
    elif seconds < 3600:
        return f"{seconds/60:.0f}分{seconds%60:.0f}秒"
    else:
        return f"{seconds/3600:.0f}時{(seconds%3600)/60:.0f}分"


class WheelProtectedSpinBox(QSpinBox):
    """QSpinBox 子類：只有被滑鼠點擊進入過、且目前仍持有焦點時才回應滾輪。

    設定頁放在 QScrollArea 內，滑鼠停在 spinbox 上方滾動時若直接改值，
    使用者想捲動頁面卻會不小心改到設定。未點擊過時忽略滾輪事件，
    讓事件往上傳給捲動區域來捲動頁面。

    注意：不能只靠 hasFocus()。視窗/分頁顯示時 spinbox 會自動取得鍵盤焦點
    （FocusReason.ActiveWindowFocusReason），此時 hasFocus() 已為 True，
    導致「未點擊卻回應滾輪」。因此用滑鼠點擊旗標 _wheel_armed 來區分。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_armed = False

    def mousePressEvent(self, event):
        # 點擊（含文字區、上下箭頭）後才允許滾輪改值
        self._wheel_armed = True
        super().mousePressEvent(event)

    def focusOutEvent(self, event):
        # 失去焦點後關閉，需再次點擊才會重新啟用滾輪
        self._wheel_armed = False
        super().focusOutEvent(event)

    def wheelEvent(self, event):
        if self._wheel_armed and self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


# SOCKS5代理測試線程
class ProxyTester(QThread):
    """SOCKS5代理測試線程"""
    test_finished = Signal(str)  # 信號：測試完成，參數為代理ID

    def __init__(self, download_manager, proxy_id):
        super().__init__()
        self.download_manager = download_manager
        self.proxy_id = proxy_id
        self.is_canceled = False

    def run(self):
        """執行測試"""
        logger.debug("開始測試代理 %s", self.proxy_id)
        try:
            # 檢查是否被取消
            if self.is_canceled:
                logger.debug("代理 %s 測試已被取消", self.proxy_id)
                return

            # 調用下載管理器的測試方法
            result = self.download_manager.test_socks_proxy(self.proxy_id)
            success, message = result
            logger.debug("測試結果: success=%s, message=%s", success, message)

            # 檢查是否被取消
            if self.is_canceled:
                logger.debug("代理 %s 測試已被取消", self.proxy_id)
                return

            # 測試完成後發送信號
            self.test_finished.emit(self.proxy_id)
        except Exception as e:
            logger.exception("測試代理 %s 時出錯: %s", self.proxy_id, e)
            # 即使出錯也發送信號，確保UI更新
            if not self.is_canceled:
                self.test_finished.emit(self.proxy_id)

    def cancel(self):
        """取消測試"""
        self.is_canceled = True
        logger.debug("代理 %s 測試被標記為取消", self.proxy_id)

# 單行分段進度條：每一小段依 frac 顯示部分填充
class SegmentProgressBar(QWidget):
    """區塊進度視覺：blocks 為 list of {'frac': 0.0~1.0, 'active': bool}。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.blocks = []
        self.setMinimumHeight(24)

    def set_blocks(self, blocks):
        self.blocks = list(blocks or [])
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor(245, 245, 245))

        if not self.blocks:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "選擇下載任務以查看區塊進度")
            painter.end()
            return

        total = len(self.blocks)
        w = rect.width()
        h = rect.height()

        # 動態調整間距：當區塊數量很多時，縮小間距避免畫面被空白網格線佔滿而顯得碎裂
        gap = 2.0
        if total > 200:
            gap = 1.0
        if total > 1000:
            gap = 0.0

        # 依長寬比例決定欄數，讓方格近似正方形並填滿整塊區域
        aspect = w / max(1.0, h)
        cols = max(1, round((total * aspect) ** 0.5))
        cols = min(cols, total)
        rows = (total + cols - 1) // cols

        cell_w = max(1.0, (w - gap * (cols - 1)) / cols)
        cell_h = max(1.0, (h - gap * (rows - 1)) / rows)

        for i, b in enumerate(self.blocks):
            row = i // cols
            col = i % cols
            x = col * (cell_w + gap)
            y = row * (cell_h + gap)
            frac = min(1.0, max(0.0, float(b.get('frac', 0.0))))
            active = bool(b.get('active', False))

            # 未下載底色
            painter.fillRect(QRectF(x, y, cell_w, cell_h), QColor(224, 224, 224))

            if frac <= 0 and not active:
                continue

            # 統一顏色：下載中藍、已下載綠。不另外區分「完成」與「部分完成」的
            # 深淺綠——BT 降採樣會把 frac 平均成 0.x，幾乎不會出現 >= 0.999，
            # 若設定一道深綠門檻，完成格永遠不會被畫成深綠，整個進度條會變成一片淡綠。
            if active:
                color = QColor(52, 152, 219)   # 下載中 藍色
            else:
                color = QColor(39, 174, 96)    # 已下載 綠色

            # 解決 BT 碎片化在密集 2D 網格中變成「條碼破圖」：
            # 密集網格用透明度表示進度（frac 越高顏色越實/越深），寬格用由左至右填充。
            if cell_w < 12:
                if active:
                    # active 格子保留較高下限，讓「正在下載哪幾塊」可辨識
                    alpha = int(255 * (0.3 + 0.7 * frac))
                else:
                    alpha = int(255 * frac)
                    if 0 < alpha < 12:
                        alpha = 12  # 極低進度仍略可見，但不至於誤導
                color.setAlpha(alpha)
                painter.fillRect(QRectF(x, y, cell_w, cell_h), color)
            else:
                fill_w = cell_w * frac
                if active and fill_w < 2.0:
                    fill_w = 2.0
                painter.fillRect(QRectF(x, y, fill_w, cell_h), color)
        painter.end()


# 即時速度曲線圖
class SpeedChartWidget(QWidget):
    """即時速度曲線：保留最近取樣，畫出折線與漸層填充，並顯示目前/峰值速度。"""

    MAX_POINTS = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._samples = deque(maxlen=self.MAX_POINTS)  # (time, bytes/sec)
        self._max_speed = 0.0
        self.setMinimumHeight(80)

    def add_sample(self, speed):
        self._samples.append((time.time(), max(0.0, float(speed))))
        if speed > self._max_speed:
            self._max_speed = float(speed)
        self.update()

    def clear(self):
        self._samples.clear()
        self._max_speed = 0.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(255, 255, 255))

        if len(self._samples) < 2:
            painter.setPen(QColor(160, 160, 160))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "速度曲線（下載時顯示）")
            painter.end()
            return

        w = self.width()
        h = self.height()
        pad_l, pad_r, pad_t, pad_b = 8, 8, 10, 22
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w <= 0 or plot_h <= 0:
            painter.end()
            return

        speeds = [s for _, s in self._samples]
        top = max(self._max_speed, max(speeds), 1.0)

        pts = []
        n = len(self._samples)
        for i, (_, s) in enumerate(self._samples):
            x = pad_l + (i / (n - 1)) * plot_w
            y = pad_t + (1.0 - s / top) * plot_h
            pts.append(QPointF(x, y))

        # 漸層填充
        fill_path = QPainterPath()
        fill_path.moveTo(pts[0])
        for p in pts[1:]:
            fill_path.lineTo(p)
        fill_path.lineTo(QPointF(pts[-1].x(), pad_t + plot_h))
        fill_path.lineTo(QPointF(pts[0].x(), pad_t + plot_h))
        fill_path.closeSubpath()
        painter.fillPath(fill_path, QColor(66, 133, 244, 60))

        # 折線
        painter.setPen(QPen(QColor(33, 100, 200), 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(len(pts) - 1):
            painter.drawLine(pts[i], pts[i + 1])

        # 目前 / 峰值速度標籤
        painter.setPen(QColor(100, 100, 100))
        cur = speeds[-1]
        label = f"目前 {format_size(cur)}/s    峰值 {format_size(self._max_speed)}/s"
        painter.drawText(pad_l, h - 6, label)
        painter.end()


# 監控任務進度的線程
class MonitorThread(QThread):
    tasks_updated = Signal(list)

    def __init__(self, download_manager):
        super().__init__()
        self.download_manager = download_manager
        self.running = True

    def run(self):
        while self.running:
            tasks = self.download_manager.get_all_tasks()
            self.tasks_updated.emit(tasks)
            time.sleep(0.5)

    def stop(self):
        self.running = False

# 下載列表：按下 Delete/Backspace 鍵時發出刪除信號，等同右鍵選單的刪除
class TaskTableWidget(QTableWidget):
    deletePressed = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deletePressed.emit()
            return
        super().keyPressEvent(event)

# 從 URL 提取存檔名（供「新增下載」對話框自動帶出檔名）
def extract_filename_from_url(url):
    parsed = urlparse(url)
    path = unquote(parsed.path)
    name = os.path.basename(path)
    if name:
        return name
    query = parse_qs(parsed.query)
    for key in ('filename', 'name', 'file', 'title', 'download'):
        if key in query and query[key] and query[key][0]:
            return query[key][0]
    return ''


def disk_free(path):
    """回傳 path 所在磁碟的剩餘位元組；無法判斷時回傳 None。"""
    if not path:
        return None
    p = path.strip()
    # 目錄可能尚未建立，沿路徑向上找第一個已存在的節點，再取其磁碟用量。
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if not p:
        p = os.path.splitdrive(path)[0] or os.sep
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


# 分線 chip 配色（依序套用：直連綠、代理藍紫橙…）
LINE_COLORS = ["#2ecc71", "#3498db", "#9b59b6", "#e67e22", "#1abc9c", "#e74c3c"]

# 全域樣式（QSS）：統一表格、表頭、進度條與狀態徽章的現代化外觀。
# 重點：表格儲存格加上內邊距，避免文字貼邊、被縱向網格線截斷。
APP_QSS = """
QTableWidget {
    background-color: #ffffff;
    alternate-background-color: #f7f9fb;
    border: 1px solid #e3e7eb;
    border-radius: 6px;
    gridline-color: #edf0f3;
    outline: 0;
}
QTableWidget::item {
    padding: 6px 10px;
    border: none;
}
QTableWidget::item:selected {
    background-color: #d6e8ff;
    color: #16181c;
}
QHeaderView::section {
    background-color: #f2f4f7;
    color: #55606c;
    padding: 8px 10px;
    border: none;
    border-bottom: 1px solid #e3e7eb;
    font-weight: 600;
}
QProgressBar {
    border: none;
    background-color: #eef1f4;
    border-radius: 4px;
}
QProgressBar::chunk {
    background-color: #3b9bff;
    border-radius: 4px;
}
QLabel#statsBadge {
    background-color: #eef4fc;
    border: 1px solid #d5e4f3;
    border-radius: 14px;
    padding: 6px 14px;
    color: #3a5a78;
}
"""


# 簡易 Flow Layout：子元件依序排列，超出寬度自動換行（分線膠囊換行用）
class FlowLayout(QLayout):
    def __init__(self, parent=None, margin=0, spacing=4):
        super().__init__(parent)
        self._items = []
        self._margin = margin
        self._spacing = spacing
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        size += QSize(2 * self._margin, 2 * self._margin)
        return size

    def _do_layout(self, rect, test_only):
        x = rect.x() + self._margin
        y = rect.y() + self._margin
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() - self._margin and line_height > 0:
                x = rect.x() + self._margin
                y = y + line_height + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = x + hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + self._margin


# 畫質選項：(顯示文字, 最高畫素高度, 是否只要音訊)。
# 高度為 None 且 audio_only=False 表示「最高畫質」；「僅音訊」設 audio_only=True。
STREAM_QUALITY_OPTIONS = [
    ("最高畫質", None, False),
    ("2160p（4K）", 2160, False),
    ("1440p（2K）", 1440, False),
    ("1080p", 1080, False),
    ("720p", 720, False),
    ("480p", 480, False),
    ("360p", 360, False),
    ("僅音訊", None, True),
]

# FPS 選項：(顯示文字, 最高幀率)。None 表示自動。
STREAM_FPS_OPTIONS = [
    ("自動", None),
    ("60", 60),
    ("50", 50),
    ("30", 30),
    ("25", 25),
    ("24", 24),
]

# 高度 → 顯示標籤（其餘直接用 f"{h}p"）。
_HEIGHT_LABELS = {2160: "2160p（4K）", 1440: "1440p（2K）"}


def _height_label(h):
    return _HEIGHT_LABELS.get(h, f"{h}p")


# 新增下載對話框：貼 URL → 自動帶出檔名（可自訂）→ 確認加入佇列
class AddDownloadDialog(QDialog):
    _probe_result = Signal(int, object)
    _format_result = Signal(int, object)

    def __init__(self, parent=None, default_save_dir="", url=None, filename=None,
                 lock_url=True, show_resolve=True, download_manager=None,
                 show_stream_options=True, auto_detect=False):
        super().__init__(parent)
        self.setWindowTitle("新增下載")
        self.setMinimumWidth(520)

        self.download_manager = download_manager
        self._auto_detect = auto_detect
        self._detected_video = False
        self._probe_gen = 0
        self._probe_timer = QTimer(self)
        self._probe_timer.setSingleShot(True)
        self._probe_timer.setInterval(400)
        self._probe_timer.timeout.connect(self._start_probe)
        self._probe_result.connect(self._apply_probe_result)
        self._probed_size = 0
        self._format_gen = 0
        self._format_result.connect(self._apply_format_result)
        self._format_timer = QTimer(self)
        self._format_timer.setSingleShot(True)
        self._format_timer.setInterval(600)
        self._format_timer.timeout.connect(self._list_formats_now)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("載點 URL："))
        self.url_edit = QPlainTextEdit()
        self.url_edit.setPlaceholderText("貼上下載連結，可一次貼多行（每行一個）")
        self.url_edit.setMaximumHeight(80)
        layout.addWidget(self.url_edit)

        layout.addWidget(QLabel("存檔名稱："))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空則由程式自動判斷")
        layout.addWidget(self.name_edit)

        self.size_label = QLabel("檔案大小：—")
        self.size_label.setStyleSheet("color: #55606c;")
        layout.addWidget(self.size_label)

        # 「解析影片連結」選項：勾選＝先用 yt-dlp 解析出單檔直連網址再下載；
        # 未勾選＝把網址當一般檔案直接下載。遠端攔截（已由擴充套件決定）時隱藏。
        # 自動偵測模式下同樣隱藏：改由背景判斷自動決定是否解析。
        self.resolve_checkbox = QCheckBox("解析影片連結（YouTube 等串流網站）")
        self.resolve_checkbox.setToolTip(
            "勾選後會先用 yt-dlp 解析出單檔直連網址再下載；"
            "未勾選則把網址當作一般檔案直接下載。")
        self.resolve_checkbox.setVisible(show_resolve and not auto_detect)
        layout.addWidget(self.resolve_checkbox)

        # 自動偵測狀態提示（僅 auto_detect 模式顯示）。
        self.detect_label = QLabel("")
        self.detect_label.setWordWrap(True)
        self.detect_label.setVisible(auto_detect)
        layout.addWidget(self.detect_label)

        # 畫質 / FPS 選擇（僅在解析串流可用時顯示）。
        # 有解析核取框時隨核取框啟停；遠端攔截已決定要解析（核取框隱藏）時直接啟用；
        # 自動偵測模式則在偵測到影片後才顯示。
        self._show_resolve = show_resolve
        self._show_stream_options = show_stream_options

        self._stream_container = QWidget()
        stream_row = QHBoxLayout(self._stream_container)
        stream_row.setContentsMargins(0, 0, 0, 0)
        stream_row.addWidget(QLabel("畫質："))
        self.quality_combo = QComboBox()
        for label, height, audio_only in STREAM_QUALITY_OPTIONS:
            self.quality_combo.addItem(label, (height, audio_only))
        stream_row.addWidget(self.quality_combo)
        stream_row.addWidget(QLabel("FPS："))
        self.fps_combo = QComboBox()
        for label, fps in STREAM_FPS_OPTIONS:
            self.fps_combo.addItem(label, fps)
        stream_row.addWidget(self.fps_combo)
        stream_row.addStretch()
        self._stream_row_layout = stream_row

        # 初始可見性：非自動偵測且（有核取框或遠端已決定解析）時才顯示。
        self._stream_row_visible = (not auto_detect) and (show_resolve or show_stream_options)
        self._stream_container.setVisible(self._stream_row_visible)
        layout.addWidget(self._stream_container)

        def _sync_stream_options(checked=False):
            if auto_detect:
                enabled = self._detected_video
            else:
                enabled = checked if show_resolve else bool(show_stream_options)
            self.quality_combo.setEnabled(enabled)
            self.fps_combo.setEnabled(enabled)

        self._sync_stream_options = _sync_stream_options
        self.resolve_checkbox.toggled.connect(_sync_stream_options)
        self.resolve_checkbox.toggled.connect(lambda _c: self._maybe_list_formats())
        _sync_stream_options(self.resolve_checkbox.isChecked())

        self._stream_error_label = QLabel("")
        self._stream_error_label.setWordWrap(True)
        self._stream_error_label.setStyleSheet("color: #e74c3c;")
        self._stream_error_label.setVisible(False)
        layout.addWidget(self._stream_error_label)

        layout.addWidget(QLabel("儲存位置："))
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(default_save_dir or "")
        browse_btn = QPushButton("瀏覽...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(self.dir_edit)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        self.free_space_label = QLabel("")
        self.free_space_label.setStyleSheet("color: #55606c;")
        layout.addWidget(self.free_space_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        ok_btn = QPushButton("確認")
        ok_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_confirm)

        # 攔截下載模式：URL 已固定，鎖住編輯框；檔名可預填但仍可修改。
        if url:
            self.url_edit.setPlainText(url)
            if lock_url:
                self.url_edit.setReadOnly(True)
            if filename:
                self.name_edit.setText(filename)
            else:
                # 擴充程式未提供檔名時，仍從 URL 自動帶出，與手動新增下載一致
                self._on_url_changed()

        self.url_edit.textChanged.connect(self._on_url_changed)
        self.dir_edit.textChanged.connect(self._update_free_space)
        self._update_free_space()

    def _on_url_changed(self):
        urls = self.urls()
        self._probed_size = 0
        if not urls:
            self.size_label.setText("檔案大小：—")
            self._update_free_space()
            return
        name = extract_filename_from_url(urls[0])
        if name:
            self.name_edit.setText(name)
        self._probe_timer.start()
        self._maybe_list_formats()

    def _start_probe(self):
        urls = self.urls()
        if not urls:
            return
        if len(urls) > 1:
            self.size_label.setText(f"共 {len(urls)} 筆連結（不預覽大小）")
            return
        url = urls[0]
        if urlparse(url).scheme.lower() not in ('http', 'https'):
            self.size_label.setText("檔案大小：—")
            return
        self.size_label.setText("檔案大小：探測中…")
        self._probe_gen += 1
        gen = self._probe_gen
        manager = self.download_manager
        proxies = manager.get_available_proxies() if manager else []
        headers = dict(getattr(manager, 'custom_headers', {}) or {})

        def _run():
            try:
                result = probe_url_metadata(url, proxies=proxies, headers=headers)
            except Exception:
                result = None
            try:
                self._probe_result.emit(gen, result)
            except RuntimeError:
                pass  # 對話框已銷毀

        threading.Thread(target=_run, daemon=True).start()

    def _apply_probe_result(self, gen, result):
        if gen != self._probe_gen:
            return  # 過期結果，捨棄
        if not result:
            self._probed_size = 0
            self.size_label.setText("檔案大小：無法取得")
            self._update_free_space()
            return
        size = result.get('size') or 0
        self._probed_size = size
        if size > 0:
            self.size_label.setText(f"檔案大小：{format_size(size)}")
        else:
            self.size_label.setText("檔案大小：未知（串流或未提供）")
        self._update_free_space()
        fname = result.get('filename')
        urls = self.urls()
        if fname and urls:
            current = self.name_edit.text().strip()
            auto = extract_filename_from_url(urls[0])
            # 只在使用者尚未自訂檔名時，才用伺服器的 Content-Disposition 覆寫
            if not current or current == auto:
                self.name_edit.setText(fname)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "選擇儲存位置", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _update_free_space(self):
        d = self.dir_edit.text().strip()
        free = disk_free(d)
        if free is None:
            self.free_space_label.setText("磁碟剩餘空間：—")
            self.free_space_label.setStyleSheet("color: #55606c;")
            return
        text = f"磁碟剩餘空間：{format_size(free)}"
        if self._probed_size and self._probed_size > free:
            short = format_size(self._probed_size - free)
            self.free_space_label.setText(f"{text}  ⚠ 空間不足，還差 {short}")
            self.free_space_label.setStyleSheet("color: #e74c3c;")
        else:
            self.free_space_label.setText(text)
            self.free_space_label.setStyleSheet("color: #55606c;")

    def _on_confirm(self):
        if not self.urls():
            QMessageBox.warning(self, "錯誤", "請輸入下載 URL")
            return
        if not self.dir_edit.text().strip():
            QMessageBox.warning(self, "錯誤", "請選擇儲存位置")
            return
        self.accept()

    def urls(self):
        return [u.strip() for u in self.url_edit.toPlainText().splitlines() if u.strip()]

    def filename(self):
        return self.name_edit.text().strip() or None

    def resolve_stream(self):
        if self._auto_detect:
            return 'auto'
        return self.resolve_checkbox.isChecked()

    def stream_max_height(self):
        """畫質選擇對應的最高畫素高度；None 表示不限（最高畫質）。"""
        height, audio_only = self.quality_combo.currentData()
        return height

    def stream_audio_only(self):
        """是否只要音訊。"""
        height, audio_only = self.quality_combo.currentData()
        return bool(audio_only)

    def stream_max_fps(self):
        """最高幀率；None 表示自動。"""
        return self.fps_combo.currentData()

    # ------------------------------------------------------------------ #
    # 格式預覽：勾選解析後，背景抓取真實畫質/幀率填入選單，並提示可用範圍
    # ------------------------------------------------------------------ #
    def _maybe_list_formats(self):
        """串流解析啟用且有單一影片網址時，觸發（延遲）格式查詢。"""
        if self._auto_detect:
            # 自動偵測：貼上/改網址就嘗試偵測，不用等核取框。
            enabled = True
        else:
            enabled = ((self._show_resolve and self.resolve_checkbox.isChecked()) or
                       (not self._show_resolve and self._show_stream_options))
        if not enabled:
            self._clear_format_options()
            return
        self._format_timer.start()

    def _list_formats_now(self):
        """背景解析可用畫質/幀率，不阻塞 UI。"""
        urls = self.urls()
        if len(urls) != 1:
            self._clear_format_options()
            return
        url = urls[0]
        if urlparse(url).scheme.lower() not in ('http', 'https'):
            self._clear_format_options()
            return
        if is_direct_file(url):
            self._clear_format_options()
            return

        if self._auto_detect:
            self.detect_label.setText("偵測中…（判斷是否為影片）")
            self.detect_label.setStyleSheet("color: #55606c;")

        self._format_gen += 1
        gen = self._format_gen
        self._stream_error_label.setText("")
        self._stream_error_label.setVisible(False)

        headers = dict(getattr(self.download_manager, 'custom_headers', {}) or {})

        def _run():
            result = list_video_formats(url, headers=headers)
            try:
                self._format_result.emit(gen, result)
            except RuntimeError:
                pass  # 對話框已銷毀

        threading.Thread(target=_run, daemon=True).start()

    def _apply_format_result(self, gen, result):
        if gen != self._format_gen:
            return
        is_video = bool(result and result.ok and (result.heights or result.has_audio))
        if self._auto_detect:
            self._detected_video = is_video
            self._stream_container.setVisible(is_video)
            self._sync_stream_options()
            if is_video:
                self.detect_label.setText("已偵測到影片（自動解析）")
                self.detect_label.setStyleSheet("color: #1a9c5c;")
                self._stream_error_label.setText("")
                self._stream_error_label.setVisible(False)
                self._populate_format_options(result)
                self._maybe_prefill_title(result)
            else:
                self.detect_label.setText("一般檔案下載（未偵測到影片）")
                self.detect_label.setStyleSheet("color: #55606c;")
                self._reset_quality_options()
            return
        if not result or not result.ok:
            err = (result.error if result else '') or '未知錯誤'
            self._stream_error_label.setText(
                f"無法解析影片：{err}（可能不存在、已刪除、私人或需登入）")
            self._stream_error_label.setVisible(True)
            self._reset_quality_options()
            return
        self._stream_error_label.setText("")
        self._stream_error_label.setVisible(False)
        self._populate_format_options(result)
        self._maybe_prefill_title(result)

    def _populate_format_options(self, result):
        heights = result.heights or []
        fpses = result.fps_values or []
        has_audio = bool(result.has_audio)

        # 畫質下拉：保留「最高畫質」與實際可用的高度；有音訊軌才加「僅音訊」。
        prev_q = self.quality_combo.currentData()
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        self.quality_combo.addItem("最高畫質", (None, False))
        for h in heights:
            self.quality_combo.addItem(_height_label(h), (h, False))
        if has_audio:
            self.quality_combo.addItem("僅音訊", (None, True))
        for i in range(self.quality_combo.count()):
            if self.quality_combo.itemData(i) == prev_q:
                self.quality_combo.setCurrentIndex(i)
                break
        self.quality_combo.blockSignals(False)

        # FPS 下拉：保留「自動」與實際可用的幀率。
        prev_fps = self.fps_combo.currentData()
        self.fps_combo.blockSignals(True)
        self.fps_combo.clear()
        self.fps_combo.addItem("自動", None)
        for f in fpses:
            self.fps_combo.addItem(str(f), f)
        for i in range(self.fps_combo.count()):
            if self.fps_combo.itemData(i) == prev_fps:
                self.fps_combo.setCurrentIndex(i)
                break
        self.fps_combo.blockSignals(False)

    def _maybe_prefill_title(self, result):
        """解析成功後，用影片標題預填「存檔名稱」（僅在使用者尚未自訂時）。

        只填標題本身、不含副檔名——副檔名在開始下載時由解析結果補上，
        使用者不需要在此決定副檔名。
        """
        title = getattr(result, 'title', '') or ''
        if not title:
            return
        urls = self.urls()
        if len(urls) != 1:
            return
        current = self.name_edit.text().strip()
        auto = extract_filename_from_url(urls[0])
        if not current or current == auto:
            self.name_edit.setText(_sanitize_filename(title))

    def _clear_format_options(self):
        """清除格式預覽與錯誤訊息，並還原預設選單。"""
        self._format_gen += 1
        self._stream_error_label.setText("")
        self._stream_error_label.setVisible(False)
        self._reset_quality_options()
        if self._auto_detect:
            self._detected_video = False
            self._stream_container.setVisible(False)
            self._sync_stream_options()
            self.detect_label.setText("一般檔案下載" if self.urls() else "")
            self.detect_label.setStyleSheet("color: #55606c;")

    def _reset_quality_options(self):
        self.quality_combo.blockSignals(True)
        self.quality_combo.clear()
        for label, height, audio_only in STREAM_QUALITY_OPTIONS:
            self.quality_combo.addItem(label, (height, audio_only))
        self.quality_combo.blockSignals(False)
        self.fps_combo.blockSignals(True)
        self.fps_combo.clear()
        for label, fps in STREAM_FPS_OPTIONS:
            self.fps_combo.addItem(label, fps)
        self.fps_combo.blockSignals(False)

    def save_dir(self):
        return self.dir_edit.text().strip()

# 種子下載對話框：選擇線路、儲存位置，並以檔案樹勾選要下載的檔案（類 uTorrent）。
class TorrentDialog(QDialog):
    def __init__(self, source, download_manager, parent=None):
        super().__init__(parent)
        self.source = source
        self.download_manager = download_manager

        self._ti = None
        self._is_torrent = False
        if not source.startswith('magnet:'):
            try:
                self._ti = lt.torrent_info(source)
                self._is_torrent = True
            except Exception as e:
                QMessageBox.warning(self, "錯誤", f"解析種子失敗:\n{e}")

        self.setWindowTitle("新增種子下載")
        self.setMinimumSize(600, 520)

        layout = QVBoxLayout(self)

        # 名稱
        name = self._ti.name() if self._is_torrent else magnet_display_name(source)
        layout.addWidget(QLabel(f"名稱: {name or '磁力連結下載'}"))

        # 儲存位置
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("儲存位置:"))
        self.dir_edit = QLineEdit(download_manager.save_dir or "")
        browse_btn = QPushButton("瀏覽...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(self.dir_edit)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

        # 線路選擇：直連、多線聚合，或任一 SOCKS5 代理
        line_row = QHBoxLayout()
        line_row.addWidget(QLabel("線路:"))
        self.line_combo = QComboBox()
        self.line_combo.addItem("直連", None)
        self.line_combo.addItem("自動（多線聚合）", 'auto')
        for p in download_manager.socks_proxies.values():
            host = p.get('host', '')
            port = p.get('port', '')
            pname = (p.get('name') or '').strip()
            label = f"{pname} ({host}:{port})" if pname else f"{host}:{port}"
            self.line_combo.addItem(label, {
                'host': host,
                'port': int(port),
                'username': p.get('username') or '',
                'password': p.get('password') or '',
            })
        # 有多個可用代理時，預設選「自動（多線聚合）」
        if download_manager.get_available_proxies():
            self.line_combo.setCurrentIndex(1)
        line_row.addWidget(self.line_combo)
        line_row.addStretch()
        layout.addLayout(line_row)

        # 做種時數設定（預設帶出全域設定值）
        seed_row = QHBoxLayout()
        seed_row.addWidget(QLabel("做種時數:"))
        self.seed_spinbox = QSpinBox()
        self.seed_spinbox.setRange(0, 720)
        self.seed_spinbox.setValue(int(download_manager.bt_seed_hours))
        self.seed_spinbox.setSpecialValueText("不做種")
        self.seed_spinbox.setSuffix(" 小時")
        seed_row.addWidget(self.seed_spinbox)
        seed_row.addStretch()
        layout.addLayout(seed_row)

        # 檔案清單：.torrent 可立即解析；magnet 需連上 peers 後才有資訊
        layout.addWidget(QLabel("檔案:"))
        self._file_items = []
        self._updating_tree = False
        if self._is_torrent:
            self.file_tree = QTreeWidget()
            self.file_tree.setHeaderLabels(["名稱", "大小"])
            header = self.file_tree.header()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(1, 90)
            header.setStretchLastSection(False)
            self.file_tree.setIndentation(18)
            self.file_tree.itemChanged.connect(self._on_file_item_changed)

            self.selected_size_label = QLabel("")
            self.selected_size_label.setStyleSheet("color: #333;")

            self._populate_files()
            layout.addWidget(self.file_tree)
            layout.addWidget(self.selected_size_label)

            sel_row = QHBoxLayout()
            all_btn = QPushButton("全選")
            none_btn = QPushButton("全不選")
            all_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Checked))
            none_btn.clicked.connect(lambda: self._set_all(Qt.CheckState.Unchecked))
            sel_row.addStretch()
            sel_row.addWidget(all_btn)
            sel_row.addWidget(none_btn)
            layout.addLayout(sel_row)
        else:
            hint = QLabel("磁力連結需連上 peers 取得種子資訊後，才能列出檔案清單；\n目前將下載整包內容。")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #999;")
            layout.addWidget(hint)

        # 按鈕
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        ok_btn = QPushButton("開始下載")
        ok_btn.setDefault(True)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._on_confirm)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "選擇儲存位置", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

    def _populate_files(self):
        self.file_tree.clear()
        self._file_items = []
        if self._ti is None:
            return
        root = self.file_tree.invisibleRootItem()
        dir_map = {}
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        for i, (rel, size) in enumerate(torrent_file_tree(self._ti)):
            parts = rel.replace('\\', '/').split('/')
            parent = root
            prefix = ()
            for part in parts[:-1]:
                prefix = prefix + (part,)
                if prefix not in dir_map:
                    d = QTreeWidgetItem([part, ''])
                    d.setFlags(d.flags() | Qt.ItemFlag.ItemIsUserCheckable
                               | Qt.ItemFlag.ItemIsAutoTristate)
                    d.setCheckState(0, Qt.CheckState.Checked)
                    d.setIcon(0, folder_icon)
                    d.setData(0, Qt.ItemDataRole.UserRole, None)
                    d.setData(0, Qt.ItemDataRole.UserRole + 1, 'dir')
                    parent.addChild(d)
                    dir_map[prefix] = d
                parent = dir_map[prefix]
            item = QTreeWidgetItem([parts[-1], format_size(size)])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setData(0, Qt.ItemDataRole.UserRole, i)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, 'file')
            item.setData(1, Qt.ItemDataRole.UserRole + 2, size)
            parent.addChild(item)
            self._file_items.append(item)
        self._aggregate_dir_sizes(root)
        self.file_tree.expandAll()
        self._update_selected_total()

    def _aggregate_dir_sizes(self, node):
        """由下往上累加：資料夾的「大小」欄顯示其內所有檔案的總位元組數。"""
        total = 0
        for i in range(node.childCount()):
            child = node.child(i)
            kind = child.data(0, Qt.ItemDataRole.UserRole + 1)
            if kind == 'dir':
                child_total = self._aggregate_dir_sizes(child)
                child.setText(1, format_size(child_total))
                child.setData(1, Qt.ItemDataRole.UserRole + 2, child_total)
                total += child_total
            else:
                total += child.data(1, Qt.ItemDataRole.UserRole + 2) or 0
        return total

    def _update_selected_total(self):
        """更新「已勾選總大小」標籤：累加目前勾選檔案（非目錄）的原始位元組數。"""
        if not self._is_torrent:
            return
        selected = 0
        total = 0
        count = 0
        for it in self._file_items:
            sz = it.data(1, Qt.ItemDataRole.UserRole + 2) or 0
            total += sz
            if it.checkState(0) != Qt.CheckState.Unchecked:
                selected += sz
                count += 1
        self.selected_size_label.setText(
            f"已勾選 {count} 個檔案，共 {format_size(selected)} / 總共 {format_size(total)}")

    def _set_all(self, state):
        if self._ti is None:
            return
        self._updating_tree = True
        try:
            for it in self._file_items:
                it.setCheckState(0, state)
            self._refresh_dir_recur(self.file_tree.invisibleRootItem())
        finally:
            self._updating_tree = False
        self._update_selected_total()

    def _refresh_dir_recur(self, node):
        for i in range(node.childCount()):
            child = node.child(i)
            if child.data(0, Qt.ItemDataRole.UserRole + 1) == 'dir':
                self._refresh_dir(child)
                self._refresh_dir_recur(child)

    def _on_file_item_changed(self, item, column):
        if self._updating_tree:
            return
        self._updating_tree = True
        try:
            state = item.checkState(0)
            self._apply_to_children(item, state)
            self._refresh_parents(item.parent())
        finally:
            self._updating_tree = False
        self._update_selected_total()

    def _apply_to_children(self, item, state):
        # 「部分勾選」是目錄的彙總狀態，不能往下套用；否則取消某個檔案時，
        # 父目錄變成半選再被套到所有子項目，整棵樹會連鎖成「-」。
        if state == Qt.CheckState.PartiallyChecked:
            return
        for i in range(item.childCount()):
            child = item.child(i)
            if child.checkState(0) != state:
                child.setCheckState(0, state)
            self._apply_to_children(child, state)

    def _refresh_parents(self, parent):
        while parent is not None:
            self._refresh_dir(parent)
            parent = parent.parent()

    @staticmethod
    def _refresh_dir(item):
        checked = 0
        unchecked = 0
        total = item.childCount()
        for i in range(total):
            cs = item.child(i).checkState(0)
            if cs == Qt.CheckState.Checked:
                checked += 1
            elif cs == Qt.CheckState.Unchecked:
                unchecked += 1
        if total == 0:
            return
        if checked == total:
            item.setCheckState(0, Qt.CheckState.Checked)
        elif unchecked == total:
            item.setCheckState(0, Qt.CheckState.Unchecked)
        else:
            item.setCheckState(0, Qt.CheckState.PartiallyChecked)

    def line(self):
        return self.line_combo.currentData()

    def seed_hours(self):
        return self.seed_spinbox.value()

    def save_dir(self):
        return self.dir_edit.text().strip()

    def filename(self):
        if self._is_torrent and self._ti is not None:
            return self._ti.name()
        return None

    def selected_files(self):
        """回傳勾選的檔案 index 列表；全選時回傳 None（等同整包下載）。"""
        if not self._is_torrent:
            return None
        sel = [it.data(0, Qt.ItemDataRole.UserRole)
               for it in self._file_items
               if it.checkState(0) != Qt.CheckState.Unchecked]
        if len(sel) == len(self._file_items):
            return None
        return sel

    def _on_confirm(self):
        if not self.save_dir():
            QMessageBox.warning(self, "錯誤", "請選擇儲存位置")
            return
        if self._is_torrent:
            sel = self.selected_files()
            if sel is not None and not sel:
                QMessageBox.warning(self, "錯誤", "請至少勾選一個要下載的檔案")
                return
        self.accept()


# 主窗口
class MainWindow(QMainWindow):
    # 攔截下載請求信號：由 HTTP 伺服器 worker 執行緒發射，跨執行緒排入 UI
    # 執行緒，觸發「選擇儲存位置」對話框。參數為 request dict。
    download_requested = Signal(dict)

    # 自動更新結果信號：由背景執行緒發射，跨執行緒排入 UI 執行緒
    update_check_done = Signal(object)       # ("error", msg) 或 ("ok", info|None)
    update_stage_done = Signal(bool, str)    # (success, error_message)

    def __init__(self, download_manager=None):
        super().__init__()

        # 使用傳入的 download_manager 或創建新的
        self.download_manager = download_manager if download_manager is not None else DownloadManager()
        self.task_table = None  # 初始化為 None

        # 存儲正在運行的代理測試線程，避免被過早釋放
        self.proxy_testers = {}

        # 已通知完成的任務 ID，避免重複通知
        self.notified_completed = set()

        # 全域聚合分線速度狀態：(上次時間, 上次聚合位元組快照)
        self._global_line_state = None

        # 分線 chip 元件快取：line_key -> QLabel
        self._line_chips = {}

        # 系統匣圖示與相關狀態
        self._tray_icon = None
        self._force_quit = False
        self._tray_hint_shown = False

        # 「新增下載」視窗開啟狀態；避免 Chrome / 剪貼簿等多個觸發同時彈出多個視窗。
        self._add_dialog_open = False
        # 開啟「新增下載」視窗期間累積的遠端下載請求，待目前視窗關閉後逐一處理。
        self._pending_download_requests = []

        self.setup_ui()  # 首先設置 UI，確保 task_table 被初始化

        # 更新保存目錄顯示
        self.dir_input.setText(self.download_manager.save_dir)

        # 載入已保存的全域限速設定（blockSignals 避免啟動時觸發多餘儲存/提示）
        self.speed_limit_spinbox.blockSignals(True)
        self.speed_limit_spinbox.setValue(self.download_manager.speed_limit // 1024)
        self.speed_limit_spinbox.blockSignals(False)

        # 載入已保存的 BT 做種與上傳限速設定
        self.bt_seed_spinbox.blockSignals(True)
        self.bt_seed_spinbox.setValue(int(self.download_manager.bt_seed_hours))
        self.bt_seed_spinbox.blockSignals(False)

        self.bt_upload_limit_spinbox.blockSignals(True)
        self.bt_upload_limit_spinbox.setValue(self.download_manager.bt_upload_rate // 1024)
        self.bt_upload_limit_spinbox.blockSignals(False)

        self.bt_resume_interval_spinbox.blockSignals(True)
        self.bt_resume_interval_spinbox.setValue(int(self.download_manager.bt_resume_interval))
        self.bt_resume_interval_spinbox.blockSignals(False)

        self.bt_max_connections_spinbox.blockSignals(True)
        self.bt_max_connections_spinbox.setValue(int(self.download_manager.bt_max_connections))
        self.bt_max_connections_spinbox.blockSignals(False)

        self.bt_proxy_max_connections_spinbox.blockSignals(True)
        self.bt_proxy_max_connections_spinbox.setValue(int(self.download_manager.bt_proxy_max_connections))
        self.bt_proxy_max_connections_spinbox.blockSignals(False)

        self.bt_max_tasks_per_line_spinbox.blockSignals(True)
        self.bt_max_tasks_per_line_spinbox.setValue(int(self.download_manager.bt_max_tasks_per_line))
        self.bt_max_tasks_per_line_spinbox.blockSignals(False)

        self.bt_force_tcp_checkbox.blockSignals(True)
        self.bt_force_tcp_checkbox.setChecked(bool(self.download_manager.bt_force_tcp))
        self.bt_force_tcp_checkbox.blockSignals(False)

        self.bt_listen_port_spinbox.blockSignals(True)
        self.bt_listen_port_spinbox.setValue(int(self.download_manager.bt_listen_port))
        self.bt_listen_port_spinbox.blockSignals(False)

        # 載入目前的 .torrent 檔案關聯狀態（blockSignals 避免啟動時觸發寫入）
        self.assoc_checkbox.blockSignals(True)
        self.assoc_checkbox.setChecked(file_association.is_registered())
        self.assoc_checkbox.blockSignals(False)

        # 載入「啟動時自動檢查更新」狀態
        self.auto_check_update_checkbox.blockSignals(True)
        self.auto_check_update_checkbox.setChecked(bool(self.download_manager.auto_check_update))
        self.auto_check_update_checkbox.blockSignals(False)

        self.monitor_thread = MonitorThread(self.download_manager)
        self.monitor_thread.tasks_updated.connect(self.on_tasks_updated)
        self.monitor_thread.start()

        # 攔截下載請求 → 彈出「選擇儲存位置」對話框（跨執行緒排入 UI 執行緒）
        self.download_requested.connect(self._on_remote_download_request)

        # 自動更新：背景執行緒結果排入 UI 執行緒
        self.update_check_done.connect(self._on_update_check_done)
        self.update_stage_done.connect(self._on_update_stage_done)

        # 啟動時自動檢查更新（若使用者已啟用）
        self.maybe_auto_check_update()

        # 剪貼簿自動偵測計時器（每秒檢查一次）
        self._last_clipboard_url = None
        self.clipboard_timer = QTimer(self)
        self.clipboard_timer.timeout.connect(self._check_clipboard)
        self.clipboard_timer.start(1000)

        # 恢復未完成的任務
        count = self.download_manager.scan_unfinished_tasks()
        if count > 0:
            # 不再顯示確認對話框，直接恢復
            logger.info("已自動恢復 %d 個未完成的下載任務", count)
            # 將恢復的任務添加到任務列表
            self.display_restored_tasks()

        # 載入已保存的SOCKS5代理
        self.load_socks_proxies()

        # 載入歷史下載紀錄
        self.load_history()

        # 建立系統匣圖示
        self._setup_tray()

    def setup_ui(self):
        self.setWindowTitle("多線程下載器")
        self.setWindowIcon(load_app_icon())
        self.setMinimumSize(800, 600)
        self.setStyleSheet(APP_QSS)

        # 主佈局
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)

        # 創建標籤頁
        self.tab_widget = QTabWidget()

        # === 下載標籤頁 ===
        download_tab = QWidget()
        download_layout = QVBoxLayout(download_tab)

        # 頂部：新增下載按鈕 + 全域統計（迅雷式簡潔主畫面）
        top_layout = QHBoxLayout()
        add_button = QPushButton("＋ 新增下載")
        add_button.setMinimumHeight(36)
        add_button.clicked.connect(self.add_download)
        self.stats_label = QLabel()
        self.stats_label.setObjectName("statsBadge")
        self.stats_label.setText(
            '<span style="color:#1a9c5c;">↓ 0 B/s</span>&nbsp;&nbsp;'
            '<span style="color:#3b9bff;">↑ 0 B/s</span>')
        top_layout.addWidget(add_button)
        top_layout.addStretch()
        top_layout.addWidget(self.stats_label)

        # 批量動作按鈕
        batch_layout = QHBoxLayout()
        pause_all_button = QPushButton("全部暫停")
        pause_all_button.clicked.connect(self.pause_all_tasks)
        resume_all_button = QPushButton("全部恢復")
        resume_all_button.clicked.connect(self.resume_all_tasks)
        clear_completed_button = QPushButton("清除已完成")
        clear_completed_button.clicked.connect(self.clear_completed_tasks)
        batch_layout.addStretch()
        batch_layout.addWidget(pause_all_button)
        batch_layout.addWidget(resume_all_button)
        batch_layout.addWidget(clear_completed_button)

        # 下載列表
        self.task_table = TaskTableWidget(0, 6)
        self.task_table.setHorizontalHeaderLabels(["檔案名", "大小", "進度", "狀態", "速度", "剩餘時間"])
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # 固定欄位給足預設寬度，避免文字被切斷；名稱與進度欄維持自動伸展
        self.task_table.setColumnWidth(1, 150)   # 大小
        self.task_table.setColumnWidth(3, 90)    # 狀態
        self.task_table.setColumnWidth(4, 190)   # 速度（↓/↑ 兩段）
        self.task_table.setColumnWidth(5, 120)   # 剩餘時間
        # 關閉硬邊網格，改用隔行底色，消除縱向邊線截斷文字的視覺問題
        self.task_table.setShowGrid(False)
        self.task_table.setAlternatingRowColors(True)
        self.task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.task_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.task_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(self.show_context_menu)
        self.task_table.cellDoubleClicked.connect(self.on_task_double_clicked)
        self.task_table.deletePressed.connect(self.delete_selected_task)

        # 將所有元素添加到下載標籤頁佈局
        download_layout.addLayout(top_layout)
        download_layout.addLayout(batch_layout)
        download_layout.addWidget(self.task_table)

        # 區塊進度視覺（雷霆式叢集網格，依比例顯示各區塊完成程度）
        self.block_map = SegmentProgressBar()
        self.block_map.setFixedHeight(72)

        # 分線速度面板：卡片式膠囊顯示各線路即時速度
        self.lines_panel = QFrame()
        self.lines_panel.setStyleSheet(
            "QFrame { background-color: #f5f6f8; border: 1px solid #e6e8eb; border-radius: 6px; }")
        lines_panel_layout = QVBoxLayout(self.lines_panel)
        lines_panel_layout.setContentsMargins(10, 8, 10, 8)
        lines_panel_layout.setSpacing(6)

        self.lines_title = QLabel("分線速度")
        self.lines_title.setStyleSheet("color: #999; font-size: 11px; border: none; background: transparent;")
        lines_panel_layout.addWidget(self.lines_title)

        self.lines_chips_container = QWidget()
        self.lines_chips_container.setStyleSheet("background: transparent; border: none;")
        self.lines_chips_flow = FlowLayout(self.lines_chips_container, margin=4, spacing=8)
        lines_panel_layout.addWidget(self.lines_chips_container)

        # 速度曲線圖，置於分段進度條下方
        self.speed_chart = SpeedChartWidget()
        self.speed_chart.setFixedHeight(130)
        self.speed_chart.setVisible(False)

        # 視覺化標題列：可切換速度曲線顯示與否
        viz_header = QHBoxLayout()
        viz_header.addWidget(QLabel("區塊進度"))
        viz_header.addStretch()
        self.show_chart_checkbox = QCheckBox("顯示速度曲線")
        self.show_chart_checkbox.setChecked(False)
        self.show_chart_checkbox.toggled.connect(self.speed_chart.setVisible)
        viz_header.addWidget(self.show_chart_checkbox)

        viz_layout = QVBoxLayout()
        viz_layout.addLayout(viz_header)
        viz_layout.addWidget(self.block_map)
        viz_layout.addWidget(self.lines_panel)
        viz_layout.addWidget(self.speed_chart)
        download_layout.addLayout(viz_layout)

        # 選取任務時更新區塊視覺
        self.task_table.itemSelectionChanged.connect(self.update_block_map_from_selection)

        # === SOCKS5 代理管理標籤頁 ===
        socks_tab = QWidget()
        socks_layout = QVBoxLayout(socks_tab)

        # SOCKS5 伺服器列表
        self.socks_table = QTableWidget(0, 6)
        self.socks_table.setHorizontalHeaderLabels(["名稱", "主機", "埠", "帳號", "狀態", "操作"])
        self.socks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.socks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 設置狀態列有更大的寬度以顯示詳細信息
        self.socks_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # 調整操作列寬度
        self.socks_table.setColumnWidth(5, 80)
        self.socks_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.socks_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.socks_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.socks_table.customContextMenuRequested.connect(self.show_socks_context_menu)

        # SOCKS5 伺服器添加區域
        socks_form_layout = QHBoxLayout()

        # 伺服器名稱輸入
        socks_name_label = QLabel("名稱:")
        self.socks_name_input = QLineEdit()
        self.socks_name_input.setPlaceholderText("為此代理起個名字...")
        socks_form_layout.addWidget(socks_name_label)
        socks_form_layout.addWidget(self.socks_name_input)

        # 伺服器主機輸入
        socks_host_label = QLabel("主機:")
        self.socks_host_input = QLineEdit()
        self.socks_host_input.setPlaceholderText("127.0.0.1")
        socks_form_layout.addWidget(socks_host_label)
        socks_form_layout.addWidget(self.socks_host_input)

        # 伺服器埠輸入
        socks_port_label = QLabel("埠:")
        self.socks_port_input = QSpinBox()
        self.socks_port_input.setRange(1, 65535)
        self.socks_port_input.setValue(1080)
        socks_form_layout.addWidget(socks_port_label)
        socks_form_layout.addWidget(self.socks_port_input)

        # 帳號輸入
        socks_username_label = QLabel("帳號:")
        self.socks_username_input = QLineEdit()
        self.socks_username_input.setPlaceholderText("可選")
        socks_form_layout.addWidget(socks_username_label)
        socks_form_layout.addWidget(self.socks_username_input)

        # 密碼輸入
        socks_password_label = QLabel("密碼:")
        self.socks_password_input = QLineEdit()
        self.socks_password_input.setPlaceholderText("可選")
        self.socks_password_input.setEchoMode(QLineEdit.EchoMode.Password)
        socks_form_layout.addWidget(socks_password_label)
        socks_form_layout.addWidget(self.socks_password_input)

        # 添加按鈕
        socks_add_button = QPushButton("添加代理")
        socks_add_button.clicked.connect(self.add_socks_proxy)
        socks_form_layout.addWidget(socks_add_button)

        # 說明文字
        socks_info_label = QLabel("添加SOCKS5代理伺服器後，單個下載任務將同時使用所有可用的代理伺服器，每個線程使用不同的代理，提高下載速度和穩定性。")
        socks_info_label.setWordWrap(True)

        # 將所有元素添加到SOCKS5標籤頁佈局
        socks_layout.addLayout(socks_form_layout)
        socks_layout.addWidget(self.socks_table)
        socks_layout.addWidget(socks_info_label)

        # === 歷史紀錄標籤頁 ===
        history_tab = QWidget()
        history_layout = QVBoxLayout(history_tab)

        self.history_table = TaskTableWidget(0, 4)
        self.history_table.setHorizontalHeaderLabels(["檔案名", "大小", "存放位置", "完成時間"])
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_table.customContextMenuRequested.connect(self.show_history_context_menu)
        self.history_table.deletePressed.connect(self.delete_selected_history)
        self.history_table.cellDoubleClicked.connect(self.on_history_double_clicked)
        history_layout.addWidget(self.history_table)

        # === 設置標籤頁 ===
        # 將設置內容包進 QScrollArea：視窗高度縮小時改以捲動呈現，
        # 避免 QFormLayout 的標籤列被壓扁、上下交疊而看不到部分設定文字。
        settings_tab = QWidget()
        settings_outer_layout = QVBoxLayout(settings_tab)
        settings_outer_layout.setContentsMargins(0, 0, 0, 0)

        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_container = QWidget()
        settings_layout = QVBoxLayout(settings_container)
        settings_scroll.setWidget(settings_container)
        settings_outer_layout.addWidget(settings_scroll)

        # 預設儲存目錄：路徑可能很長，獨立成一個整列欄位置於最上方。
        dir_form = QFormLayout()
        self.dir_input = QLineEdit()
        self.dir_input.setReadOnly(True)
        dir_button = QPushButton("瀏覽...")
        dir_button.clicked.connect(self.select_save_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_input)
        dir_row.addWidget(dir_button)
        dir_form.addRow("預設儲存目錄:", dir_row)
        settings_layout.addLayout(dir_form)

        # 下方分左右兩欄：左為「下載預設值」，右為「其他」。
        cols_row = QHBoxLayout()
        cols_row.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 下載預設值（左欄）
        dl_group = QGroupBox("下載預設值")
        dl_form = QFormLayout(dl_group)

        self.speed_limit_spinbox = WheelProtectedSpinBox()
        self.speed_limit_spinbox.setRange(0, 1024 * 1024)
        self.speed_limit_spinbox.setValue(0)
        self.speed_limit_spinbox.setSpecialValueText("不限速")
        self.speed_limit_spinbox.valueChanged.connect(self.on_speed_limit_changed)
        dl_form.addRow("全域限速 (KB/s):", self.speed_limit_spinbox)

        self.chunks_spinbox = WheelProtectedSpinBox()
        self.chunks_spinbox.setRange(0, 2000)
        self.chunks_spinbox.setValue(0)
        self.chunks_spinbox.setSpecialValueText("自動")
        dl_form.addRow("分片數:", self.chunks_spinbox)

        self.threads_per_proxy_spinbox = WheelProtectedSpinBox()
        self.threads_per_proxy_spinbox.setRange(1, 32)
        self.threads_per_proxy_spinbox.setValue(6)
        dl_form.addRow("每代理線程數:", self.threads_per_proxy_spinbox)

        self.bt_seed_spinbox = WheelProtectedSpinBox()
        self.bt_seed_spinbox.setRange(0, 720)  # 最多 30 天
        self.bt_seed_spinbox.setValue(0)
        self.bt_seed_spinbox.setSpecialValueText("不做種")
        self.bt_seed_spinbox.setSuffix(" 小時")
        self.bt_seed_spinbox.valueChanged.connect(self.on_bt_seed_hours_changed)
        dl_form.addRow("BT 做種時數:", self.bt_seed_spinbox)

        self.bt_upload_limit_spinbox = WheelProtectedSpinBox()
        self.bt_upload_limit_spinbox.setRange(0, 1024 * 1024)
        self.bt_upload_limit_spinbox.setValue(0)
        self.bt_upload_limit_spinbox.setSpecialValueText("不限速")
        self.bt_upload_limit_spinbox.valueChanged.connect(self.on_bt_upload_limit_changed)
        dl_form.addRow("BT 上傳限速 (KB/s):", self.bt_upload_limit_spinbox)

        self.bt_resume_interval_spinbox = WheelProtectedSpinBox()
        self.bt_resume_interval_spinbox.setRange(1, 600)
        self.bt_resume_interval_spinbox.setValue(10)
        self.bt_resume_interval_spinbox.setSuffix(" 秒")
        self.bt_resume_interval_spinbox.valueChanged.connect(self.on_bt_resume_interval_changed)
        dl_form.addRow("BT 續傳保存間隔:", self.bt_resume_interval_spinbox)

        self.bt_max_connections_spinbox = WheelProtectedSpinBox()
        self.bt_max_connections_spinbox.setRange(0, 1000)
        self.bt_max_connections_spinbox.setValue(200)
        self.bt_max_connections_spinbox.setSpecialValueText("不限")
        self.bt_max_connections_spinbox.setToolTip(
            "BT 直連線路的最大連線數。直連有公網 IP、可雙向連線，"
            "可設較高（如 200-500）以拉高下載速度。0 = 用 libtorrent 預設。")
        self.bt_max_connections_spinbox.valueChanged.connect(self.on_bt_max_connections_changed)
        dl_form.addRow("BT 連線數上限（直連）:", self.bt_max_connections_spinbox)

        self.bt_proxy_max_connections_spinbox = WheelProtectedSpinBox()
        self.bt_proxy_max_connections_spinbox.setRange(0, 500)
        self.bt_proxy_max_connections_spinbox.setValue(30)
        self.bt_proxy_max_connections_spinbox.setSpecialValueText("不限")
        self.bt_proxy_max_connections_spinbox.setToolTip(
            "BT SOCKS5 代理線路的最大連線數。代理多在 CGNAT 後可達 peer 少，"
            "連線設太高只會製造死連線 churn、拖慢整體下載（建議 20-40）。")
        self.bt_proxy_max_connections_spinbox.valueChanged.connect(self.on_bt_proxy_max_connections_changed)
        dl_form.addRow("BT 連線數上限（SOCKS5）:", self.bt_proxy_max_connections_spinbox)

        self.bt_max_tasks_per_line_spinbox = WheelProtectedSpinBox()
        self.bt_max_tasks_per_line_spinbox.setRange(0, 20)
        self.bt_max_tasks_per_line_spinbox.setValue(5)
        self.bt_max_tasks_per_line_spinbox.setSpecialValueText("不提醒")
        self.bt_max_tasks_per_line_spinbox.setToolTip(
            "每條 SOCKS5 線路同時進行的 BT 任務數上限。"
            "5G-Proxy-Pro 的 SOCKS5 握手是阻塞式（執行緒池 192 條、握手逾時 3 秒），"
            "同一條 5G 線路開太多 BT 任務會互相拖慢甚至丟連線（建議 5）。0 = 不提醒。")
        self.bt_max_tasks_per_line_spinbox.valueChanged.connect(self.on_bt_max_tasks_per_line_changed)
        dl_form.addRow("每線同時 BT 任務上限:", self.bt_max_tasks_per_line_spinbox)

        self.bt_listen_port_spinbox = WheelProtectedSpinBox()
        self.bt_listen_port_spinbox.setRange(0, 65535)
        self.bt_listen_port_spinbox.setValue(6881)
        self.bt_listen_port_spinbox.setSpecialValueText("動態埠")
        self.bt_listen_port_spinbox.setToolTip(
            "BT 直連線路的監聽埠。固定埠＋UPnP/NAT-PMP 可讓外部 peer 主動連入、"
            "提升可連 peer 數；0 = 動態埠。僅直連線路生效，代理線路不受影響。")
        self.bt_listen_port_spinbox.valueChanged.connect(self.on_bt_listen_port_changed)
        dl_form.addRow("BT 監聽埠（直連）:", self.bt_listen_port_spinbox)

        self.bt_force_tcp_checkbox = QCheckBox("BT 僅用 TCP 連線（停用 uTP/UDP）")
        self.bt_force_tcp_checkbox.setToolTip(
            "適用於 UDP 被封的環境（如 5G 行動網路 + SOCKS5 轉接）。"
            "停用 uTP 後所有 peer 資料傳輸走 TCP，避免無效的 UDP 連線嘗試佔用資源、拖慢下載。")
        self.bt_force_tcp_checkbox.toggled.connect(self.on_bt_force_tcp_changed)
        dl_form.addRow(self.bt_force_tcp_checkbox)

        # 還原預設設定（移至下載預設值群組內）
        self.reset_preferences_button = QPushButton("還原預設設定")
        self.reset_preferences_button.setToolTip(
            "還原儲存目錄、限速、下載預設值、BT 各項數值、自訂表頭與自動更新"
            "到預設值；SOCKS5 代理與下載歷史紀錄不會被清除。")
        self.reset_preferences_button.clicked.connect(self.on_reset_preferences)
        reset_row = QHBoxLayout()
        reset_row.addWidget(self.reset_preferences_button)
        reset_row.addStretch()
        dl_form.addRow(reset_row)

        cols_row.addWidget(dl_group, 1)

        # 其他（右欄）
        misc_group = QGroupBox("其他")
        misc_layout = QVBoxLayout(misc_group)

        self.header_button = QPushButton("自訂 HTTP 表頭")
        self.header_button.clicked.connect(self.open_header_dialog)
        misc_layout.addWidget(self.header_button)

        self.clipboard_checkbox = QCheckBox("剪貼簿自動偵測（複製連結即自動下載）")
        self.clipboard_checkbox.setToolTip("開啟後，複製網址/連結會自動加入下載")
        misc_layout.addWidget(self.clipboard_checkbox)

        self.tray_checkbox = QCheckBox("關閉時縮到系統匣")
        self.tray_checkbox.setToolTip("開啟後，點右上角關閉鈕會直接縮到系統匣繼續下載，不再詢問")
        self.tray_checkbox.setChecked(False)
        misc_layout.addWidget(self.tray_checkbox)

        self.assoc_checkbox = QCheckBox("關聯 .torrent 檔案（設為預設開啟程式）")
        self.assoc_checkbox.setToolTip(
            "勾選後，雙擊 .torrent 檔會由本程式開啟。"
            "若 Windows 已指定其他預設程式，可能需要經「開啟檔案」對話框確認。")
        self.assoc_checkbox.toggled.connect(self.on_torrent_assoc_changed)
        misc_layout.addWidget(self.assoc_checkbox)

        self.auto_check_update_checkbox = QCheckBox("啟動時自動檢查更新")
        self.auto_check_update_checkbox.setToolTip("開啟後，每次啟動會於背景檢查是否有新版本")
        self.auto_check_update_checkbox.toggled.connect(self.on_auto_check_update_changed)
        misc_layout.addWidget(self.auto_check_update_checkbox)

        self.update_check_button = QPushButton("立即檢查更新")
        self.update_check_button.clicked.connect(self.on_check_update_clicked)
        misc_layout.addWidget(self.update_check_button)

        self.update_status_label = QLabel("")
        self.update_status_label.setWordWrap(True)
        misc_layout.addWidget(self.update_status_label)

        cols_row.addWidget(misc_group, 1)

        settings_layout.addLayout(cols_row)
        # 把多餘的垂直空間推到最底部，避免「預設儲存目錄」列下方被撐出一大片空白
        settings_layout.addStretch(1)

        # 將標籤頁添加到標籤頁小部件
        self.tab_widget.addTab(download_tab, "下載管理")
        self.tab_widget.addTab(history_tab, "歷史紀錄")
        self.tab_widget.addTab(socks_tab, "SOCKS5 代理")
        self.tab_widget.addTab(settings_tab, "設置")

        # 將標籤頁小部件添加到主佈局
        main_layout.addWidget(self.tab_widget)

        self.setCentralWidget(main_widget)
        self.setAcceptDrops(True)

    def select_save_dir(self):
        dir_path = QFileDialog.getExistingDirectory(self, "選擇保存目錄", self.dir_input.text())
        if dir_path:
            if self.download_manager.set_save_dir(dir_path):
                self.dir_input.setText(dir_path)
            else:
                QMessageBox.warning(self, "錯誤", "無法設置保存目錄，請確保目錄存在且有寫入權限")

    @staticmethod
    def _is_valid_url(url):
        if url.startswith(('http://', 'https://', 'ftp://', 'magnet:')):
            return True
        return url.lower().endswith('.torrent') and os.path.isfile(url)

    def add_download(self):
        """開啟「新增下載」對話框，確認後加入任務佇列。"""
        if self._add_dialog_open:
            return
        self._add_dialog_open = True
        try:
            dialog = AddDownloadDialog(self, self.download_manager.save_dir,
                                      download_manager=self.download_manager,
                                      auto_detect=True)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._add_urls(
                dialog.urls(),
                filename=dialog.filename(),
                save_dir=dialog.save_dir(),
                resolve_stream=dialog.resolve_stream(),
                stream_max_height=dialog.stream_max_height(),
                stream_max_fps=dialog.stream_max_fps(),
                stream_audio_only=dialog.stream_audio_only(),
            )
        finally:
            self._add_dialog_open = False
            self._drain_pending_requests()

    def _on_remote_download_request(self, request):
        """攔截下載請求：跳出「新增下載」對話框（URL 鎖住），讓使用者改檔名與儲存位置。

        由 download_requested 信號觸發（已排入 UI 執行緒）。儲存路徑預設為全域
        儲存目錄，使用者可改到任意位置、也可改存檔名稱；取消則不建立任何任務。
        """
        # 攔截到 Chrome 下載：先把主視窗帶到前景並切到下載管理分頁，否則對話框
        # 可能被埋在瀏覽器後面，或主視窗縮到系統匣時根本看不到。
        self.bring_to_front()
        self._switch_to_download_tab()

        url = request.get('url', '')
        if not url:
            return

        # 已有一個「新增下載」視窗開啟時，先排入佇列，待目前視窗關閉後再依序處理，
        # 避免同時跳出多個視窗互相遮蔽。
        if self._add_dialog_open:
            self._pending_download_requests.append(request)
            return

        self._process_remote_download_request(request)

    def _process_remote_download_request(self, request):
        """開啟「新增下載」對話框處理單一遠端下載請求（URL 鎖住）。"""
        url = request.get('url', '')
        filename = request.get('filename')
        headers = request.get('headers')
        chunks_per_part = request.get('chunks_per_part', 0)
        threads_per_proxy = request.get('threads_per_proxy', 6)
        resolve_stream = request.get('resolve_stream', False)
        default_dir = request.get('save_dir') or self.download_manager.save_dir

        self._add_dialog_open = True
        try:
            # URL 已固定（鎖住不可編輯），檔名與儲存位置留給使用者設定。
            # 「是否解析串流」由擴充套件在右鍵選單已決定，這裡不顯示核取框。
            dialog = AddDownloadDialog(self, default_dir, url=url, filename=filename,
                                      show_resolve=False, download_manager=self.download_manager,
                                      show_stream_options=resolve_stream)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            save_dir = dialog.save_dir()
            filename = dialog.filename()

            try:
                task_id = self.download_manager.add_task(
                    url,
                    filename,
                    save_dir=save_dir,
                    use_proxy=True,
                    chunks_per_part=chunks_per_part,
                    threads_per_proxy=threads_per_proxy,
                    headers=headers,
                    resolve_stream=resolve_stream,
                    stream_max_height=dialog.stream_max_height(),
                    stream_max_fps=dialog.stream_max_fps(),
                    stream_audio_only=dialog.stream_audio_only(),
                )
            except Exception as e:
                QMessageBox.critical(self, "錯誤", f"添加下載任務失敗:\n{e}")
                return

            self.add_task_to_table(task_id, self.download_manager.task_ids[task_id])
            threading.Thread(
                target=self._start_task_in_background,
                args=(task_id, url),
                daemon=True,
            ).start()
            self.statusBar().showMessage(f"已加入下載: {url[:60]}", 4000)
        finally:
            self._add_dialog_open = False
            self._drain_pending_requests()

    def _switch_to_download_tab(self):
        """切換到下載管理分頁（index 0）。"""
        if getattr(self, 'tab_widget', None) is not None and self.tab_widget.currentIndex() != 0:
            self.tab_widget.setCurrentIndex(0)

    def _drain_pending_requests(self):
        """「新增下載」視窗關閉後，依序處理期間累積的遠端下載請求。

        只處理一個，其餘交由該請求關閉後的遞迴鏈接續；flag 避免重入時多開視窗。
        """
        if self._add_dialog_open:
            return
        if not self._pending_download_requests:
            return
        next_request = self._pending_download_requests.pop(0)
        self._process_remote_download_request(next_request)

    def _add_urls(self, urls, filename=None, silent=False, save_dir=None,
                  resolve_stream=False, stream_max_height=None,
                  stream_max_fps=None, stream_audio_only=False):
        """新增一批 URL 下載任務。回傳是否至少新增了一個任務。"""
        if not urls:
            return False

        chunks_per_part = self.chunks_spinbox.value()
        threads_per_proxy = self.threads_per_proxy_spinbox.value()
        save_dir = save_dir or self.dir_input.text().strip()

        # 批次輸入多個 URL 時，檔案名稱不應套用到所有任務
        if len(urls) > 1:
            filename = None

        invalid = [u for u in urls if not self._is_valid_url(u)]

        if invalid:
            if not silent:
                QMessageBox.warning(self, "錯誤", f"無效的URL格式\nURL: {invalid[0]}")
            return False

        added = 0
        for url in urls:
            try:
                # add_task 只做簿記、不碰網路，可在 UI 執行緒安全執行
                task_id = self.download_manager.add_task(
                    url,
                    filename,
                    save_dir=save_dir,
                    use_proxy=True,
                    chunks_per_part=chunks_per_part,
                    threads_per_proxy=threads_per_proxy,
                    resolve_stream=resolve_stream,
                    stream_max_height=stream_max_height,
                    stream_max_fps=stream_max_fps,
                    stream_audio_only=stream_audio_only,
                )

                # 立即把任務加到表格（狀態先顯示為初始化）
                self.add_task_to_table(task_id, self.download_manager.task_ids[task_id])

                # start_task 會對目標網址做探測請求，可能耗時數十秒，
                # 改在背景執行緒啟動，避免阻塞 UI；結果由 MonitorThread 同步到狀態列。
                threading.Thread(
                    target=self._start_task_in_background,
                    args=(task_id, url),
                    daemon=True,
                ).start()
                added += 1

            except Exception as e:
                logger.exception("下載任務添加失敗: %s", e)
                if not silent:
                    QMessageBox.critical(self, "錯誤", f"添加下載任務失敗:\n{e}\n\nURL: {url}")
        return added > 0

    def _add_bt_interactive(self, source):
        """拖入 / 開啟 .torrent 或 magnet 時，跳出對話框選線路與檔案後加入 BT 任務。"""
        dialog = TorrentDialog(source, self.download_manager, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        line = dialog.line()
        selected = dialog.selected_files()
        save_dir = dialog.save_dir()
        filename = dialog.filename()
        seed_hours = dialog.seed_hours()

        try:
            task_id = self.download_manager.add_task(
                source, filename, save_dir=save_dir, use_proxy=True,
                line=line, selected_files=selected, seed_hours=seed_hours)
        except Exception as e:
            QMessageBox.critical(self, "錯誤", f"添加種子下載失敗:\n{e}")
            return

        task = self.download_manager.task_ids[task_id]
        self._warn_bt_concurrency(task)
        self.add_task_to_table(task_id, task)
        threading.Thread(
            target=self._start_task_in_background,
            args=(task_id, source),
            daemon=True,
        ).start()

    def _warn_bt_concurrency(self, task):
        """BT 任務新增後，若任一 SOCKS5 線路超出同時任務上限，跳軟提醒（不阻止）。"""
        line_proxies = getattr(task, '_line_proxies', [])
        overloaded = self.download_manager.bt_overloaded_lines(line_proxies)
        if not overloaded:
            return
        lines = "、".join(overloaded)
        QMessageBox.warning(
            self, "BT 同時任務過多",
            f"以下 SOCKS5 線路已達每線同時 BT 任務上限"
            f"（{self.download_manager.bt_max_tasks_per_line} 個）：\n{lines}\n\n"
            "5G-Proxy-Pro 的 SOCKS5 握手執行緒有限，同一條線開太多 BT 任務會互相拖慢、"
            "甚至丟連線。建議等既有任務完成後再新增。\n\n本次仍會繼續新增。")

    # --- 拖放支援 ---
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        md = event.mimeData()
        bt_sources = []
        others = []

        def _classify(text):
            text = text.strip()
            if not text:
                return
            if text.startswith('magnet:'):
                bt_sources.append(text)
            else:
                others.append(text)

        if md.hasUrls():
            for u in md.urls():
                local = u.toLocalFile()
                if local and os.path.isfile(local):
                    if local.lower().endswith('.torrent'):
                        # .torrent 檔：跳出對話框選線路與檔案
                        bt_sources.append(local)
                    else:
                        # 讀取拖入的文字檔內容（每行一個 URL）
                        try:
                            with open(local, 'r', encoding='utf-8', errors='ignore') as f:
                                for line in f.read().splitlines():
                                    _classify(line)
                        except Exception:
                            pass
                else:
                    _classify(u.toString())

        if md.hasText():
            for line in md.text().splitlines():
                _classify(line)

        # .torrent / magnet 逐一跳出對話框（選線路 + 勾選檔案）
        for src in bt_sources:
            self._add_bt_interactive(src)

        valid = [u for u in others if self._is_valid_url(u)]
        if valid:
            self._add_urls(valid, silent=True)
        event.acceptProposedAction()

    # --- 剪貼簿自動偵測 ---
    def _check_clipboard(self):
        if not getattr(self, 'clipboard_checkbox', None):
            return
        if not self.clipboard_checkbox.isChecked():
            return
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        first = text.splitlines()[0].strip()
        if not self._is_valid_url(first):
            return
        if first == getattr(self, '_last_clipboard_url', None):
            return
        self._last_clipboard_url = first

        # 偵測到下載連結：把主視窗帶到前景並切到下載管理分頁，再彈出「新增下載」
        # 對話框讓使用者確認，而非直接下載。
        self.bring_to_front()
        self._switch_to_download_tab()

        # 已有「新增下載」視窗開啟時不再重複彈出，避免多個視窗互相遮蔽。
        if self._add_dialog_open:
            return
        self._open_clipboard_add_dialog(first)

    def _open_clipboard_add_dialog(self, url):
        """剪貼簿偵測到的連結：跳出「新增下載」對話框，確認後加入佇列。"""
        self._add_dialog_open = True
        try:
            # 剪貼簿來源 URL 預填但可編輯（貼上的可能是多行，讓使用者自行調整）。
            dialog = AddDownloadDialog(self, self.download_manager.save_dir, url=url,
                                      lock_url=False, download_manager=self.download_manager,
                                      auto_detect=True)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self._add_urls(
                dialog.urls(),
                filename=dialog.filename(),
                save_dir=dialog.save_dir(),
                resolve_stream=dialog.resolve_stream(),
                stream_max_height=dialog.stream_max_height(),
                stream_max_fps=dialog.stream_max_fps(),
                stream_audio_only=dialog.stream_audio_only(),
            )
        finally:
            self._add_dialog_open = False
            self._drain_pending_requests()

    def _start_task_in_background(self, task_id, url):
        """在背景執行緒啟動下載任務，避免 start_task 的網路探測阻塞 UI。"""
        try:
            if not self.download_manager.start_task(task_id):
                task = self.download_manager.task_ids.get(task_id)
                status = getattr(task, 'status', '?') if task else '?'
                err = getattr(task, 'error_message', '') if task else ''
                logger.error(
                    "event=start_task_failed task_id=%s url=%s status=%s error_reason=%s error=%s",
                    task_id, url, status, getattr(task, 'error_reason', '') if task else '',
                    err)
                # 失敗時 start_task 已把 task.status 設為 'error'，
                # MonitorThread 會自動把狀態同步到表格。
        except Exception as e:
            logger.exception("任務 %s 執行失敗", task_id)

    def add_task_to_table(self, task_id, task):
        # 去重：同一個 task_id 只應在表格中出現一次。相同磁力/URL 加入時，
        # download_manager 會去重返還同一個 id，若不檢查便會產生重複列。
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                return
        row = self.task_table.rowCount()
        self.task_table.insertRow(row)

        # 存儲任務ID
        self.task_table.setItem(row, 0, QTableWidgetItem(task.filename))
        self.task_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, task_id)

        # 進度條
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        self.task_table.setCellWidget(row, 2, progress_bar)

        # 設置其他列
        self.task_table.setItem(row, 1, QTableWidgetItem("計算中..."))
        self.task_table.setItem(row, 3, QTableWidgetItem(task.status))
        self._set_speed_cell(row, '<span style="color:#98a2ad;">0 B/s</span>')
        self.task_table.setItem(row, 5, QTableWidgetItem("計算中..."))

    def _set_speed_cell(self, row, html):
        """速度欄位以 QLabel 呈現富文字（下載綠 ↓、上傳藍 ↑）；不存在則建立。"""
        w = self.task_table.cellWidget(row, 4)
        if isinstance(w, QLabel):
            w.setText(html)
        else:
            lbl = QLabel(html)
            lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            lbl.setContentsMargins(4, 0, 4, 0)
            self.task_table.setCellWidget(row, 4, lbl)

    def update_task_progress(self, task_data):
        # 確保 task_table 已經初始化
        if self.task_table is None:
            return

        task_id = task_data['id']
        progress = task_data['progress']

        # 檢查任務是否已顯示在表格中，如果不在則添加
        found = False
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                found = True
                break

        # 如果任務不在表格中並且任務存在於下載管理器中，則添加到表格
        if not found and task_id in self.download_manager.task_ids:
            task = self.download_manager.task_ids[task_id]
            # 已完成任務已移入歷史紀錄，不再顯示於下載管理列表
            if task.status == 'completed':
                return
                logger.info("檢測到新任務 (可能來自 HTTP 伺服器): %s，添加到 UI 表格",
                            task.filename)
            self.add_task_to_table(task_id, task)

        # 查找對應的行（可能是剛添加的）
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                # 更新大小
                if progress['total_size'] > 0:
                    size_text = f"{format_size(progress['downloaded_size'])}/{format_size(progress['total_size'])}"
                else:
                    size_text = f"{format_size(progress['downloaded_size'])} / 未知"
                self.task_table.setItem(row, 1, QTableWidgetItem(size_text))

                # 更新進度條：總量未知（chunked 回應無 Content-Length）時改用忙碌
                # 樣式，否則顯示 0~100 的實際百分比。
                progress_bar = self.task_table.cellWidget(row, 2)
                if progress['total_size'] > 0:
                    progress_bar.setRange(0, 100)
                    progress_bar.setValue(int(progress['percentage']))
                elif progress['status'] == 'downloading':
                    progress_bar.setRange(0, 0)  # 忙碌指示（總量未知仍在下載）
                else:
                    progress_bar.setRange(0, 100)
                    progress_bar.setValue(0)

                # 更新狀態
                status = progress['status']
                self.task_table.setItem(row, 3, QTableWidgetItem(self.get_status_text(status)))

                # 更新速度（下載綠 ↓、上傳藍 ↑）
                if status in ['paused', 'error', 'completed', 'canceled']:
                    speed_html = '<span style="color:#98a2ad;">0 B/s</span>'
                elif status == 'seeding':
                    ul = progress.get('upload_speed', 0) or 0
                    speed_html = f'<span style="color:#3b9bff;">↑ {format_size(ul)}/s</span>'
                else:
                    dl = progress.get('speed', 0) or 0
                    ul = progress.get('upload_speed', 0) or 0
                    dl_part = f'<span style="color:#1a9c5c;">↓ {format_size(dl)}/s</span>'
                    ul_part = f'<span style="color:#3b9bff;">↑ {format_size(ul)}/s</span>' if ul > 0 else ''
                    speed_html = dl_part + (f'&nbsp;&nbsp;{ul_part}' if ul_part else '')
                self._set_speed_cell(row, speed_html)

                # 更新剩餘時間
                if status in ['paused', 'error', 'completed', 'canceled']:
                    # 暫停、錯誤或完成狀態下沒有剩餘時間
                    if status == 'completed':
                        time_text = "已完成"
                    elif status == 'paused':
                        time_text = "已暫停"
                    elif status == 'error':
                        time_text = "出錯"
                    else:
                        time_text = "--"
                elif status == 'seeding':
                    rem = progress.get('seeding_remaining', 0)
                    time_text = f"做種中 ({format_time(rem)})" if rem > 0 else "做種中"
                elif progress.get('waiting_metadata'):
                    time_text = "等待種子資訊…"
                elif progress['total_size'] <= 0:
                    # 總量未知（如 chunked 回應無 Content-Length），無法估算剩餘時間
                    time_text = "未知"
                elif progress['speed'] > 0:
                    remaining_bytes = progress['total_size'] - progress['downloaded_size']
                    remaining_time = remaining_bytes / progress['speed']
                    time_text = format_time(remaining_time)
                else:
                    time_text = "計算中..."
                self.task_table.setItem(row, 5, QTableWidgetItem(time_text))

                # 設置字體顏色
                status_item = self.task_table.item(row, 3)
                if status == 'completed':
                    status_item.setForeground(QColor(Qt.GlobalColor.green))
                elif status == 'seeding':
                    status_item.setForeground(QColor(46, 139, 87))  # 海綠色
                elif status == 'error':
                    status_item.setForeground(QColor(Qt.GlobalColor.red))
                    # 錯誤任務在狀態格顯示錯誤訊息 tooltip
                    err_msg = progress.get('error_message') or '未知錯誤'
                    status_item.setToolTip(err_msg)
                elif status == 'paused':
                    status_item.setForeground(QColor(Qt.GlobalColor.blue))

                # 完成時記錄歷史並通知；做種中的任務仍停留在列表上，等真正完成才移除
                if status == 'completed':
                    if task_id not in self.notified_completed:
                        self.notified_completed.add(task_id)
                        task = self.download_manager.task_ids.get(task_id)
                        if task:
                            self._record_history(task)
                            self._notify_completed(task.filename)
                        else:
                            self._notify_completed("")
                    self.task_table.removeRow(row)
                    break

                # 若為目前選中的任務，即時刷新區塊視覺
                if self.task_table.currentRow() == row:
                    self.block_map.set_blocks(progress.get('blocks', []))

                break

    def update_block_map_from_selection(self):
        """根據目前選中的任務更新區塊進度視覺"""
        row = self.task_table.currentRow()
        if row < 0:
            self.block_map.set_blocks([])
            return
        item = self.task_table.item(row, 0)
        if not item:
            self.block_map.set_blocks([])
            return
        task_id = item.data(Qt.ItemDataRole.UserRole)
        task = self.download_manager.task_ids.get(task_id)
        if not task:
            self.block_map.set_blocks([])
            return
        prog = task.get_progress()
        self.block_map.set_blocks(prog.get('blocks', []))

    def get_status_text(self, status):
        status_map = {
            'initialized': '初始化',
            'downloading': '下載中',
            'seeding': '做種中',
            'paused': '已暫停',
            'completed': '已完成',
            'error': '錯誤',
            'canceled': '已取消'
        }
        return status_map.get(status, status)

    def _notify_completed(self, filename):
        """顯示下載完成通知（非阻塞）"""
        msg = f"下載完成: {filename}"
        try:
            if self._tray_icon is not None and QSystemTrayIcon.isSystemTrayAvailable():
                self._tray_icon.show()
                self._tray_icon.showMessage("下載完成", msg, QSystemTrayIcon.MessageIcon.Information, 5000)
                return
        except Exception as e:
            logger.debug("系統列通知失敗: %s", e)
        # 退回：警告主視窗 + 狀態列訊息
        QApplication.instance().alert(self)
        self.statusBar().showMessage(msg, 5000)

    def on_tasks_updated(self, tasks):
        """監控線程單次批次更新：逐列刷新後，全域統計與分線速度只算一次。"""
        total_speed = 0.0
        total_upload = 0.0
        active_count = 0
        agg_bytes = {}
        agg_labels = {}
        for task_data in tasks:
            self.update_task_progress(task_data)
            prog = task_data['progress']
            if prog['status'] in ('downloading', 'seeding'):
                total_speed += prog.get('speed') or 0
                total_upload += prog.get('upload_speed') or 0
                active_count += 1
                for key, b in (prog.get('line_bytes') or {}).items():
                    agg_bytes[key] = agg_bytes.get(key, 0) + b
                for key, label in (prog.get('line_labels') or {}).items():
                    agg_labels.setdefault(key, label)
        line_items = self._compute_global_line_speeds(agg_bytes, agg_labels)
        self._update_line_chips(line_items)
        self._update_global_stats(total_speed, active_count, total_upload)

    def _update_global_stats(self, total_speed=None, active_count=None, total_upload=None):
        """更新全域統計：合計下載/上傳速度與進行中任務數（可傳入已算好的值避免重複計算）"""
        if total_speed is None or active_count is None:
            total_speed = 0.0
            total_upload = 0.0
            active_count = 0
            for task in self.download_manager.get_all_tasks():
                prog = task['progress']
                if prog['status'] in ('downloading', 'seeding'):
                    total_speed += prog.get('speed') or 0
                    total_upload += prog.get('upload_speed') or 0
                    active_count += 1
        if total_upload is None:
            total_upload = 0.0
        dht_nodes = self.download_manager.get_dht_node_count()
        self.stats_label.setText(
            f'<span style="color:#1a9c5c;font-weight:600;">↓ {format_size(total_speed)}/s</span>'
            f'&nbsp;&nbsp;<span style="color:#3b9bff;font-weight:600;">↑ {format_size(total_upload)}/s</span>'
            f'&nbsp;&nbsp;<span style="color:#8a97a3;">· {active_count} 任務</span>'
            f'&nbsp;&nbsp;<span style="color:#8a97a3;">· DHT {dht_nodes} 節點</span>'
        )
        self.speed_chart.add_sample(total_speed)

    def _compute_global_line_speeds(self, line_bytes, line_labels):
        """計算全域各線聚合即時速度，回傳 [(key, speed, 顯示名稱), ...]，依速度排序。"""
        now = time.time()
        prev = self._global_line_state
        self._global_line_state = (now, dict(line_bytes))
        if prev is None:
            return []
        last_t, last_b = prev
        dt = now - last_t
        if dt <= 0:
            return []
        items = []
        for key, label in line_labels.items():
            b = line_bytes.get(key, 0)
            speed = (b - last_b.get(key, 0)) / dt
            items.append((key, speed, self._line_display_name(key, label)))
        items.sort(key=lambda x: -x[1])
        return items

    def _line_display_name(self, key, fallback):
        """把線路 key 轉成帶類別前綴的可讀名稱：直連線路 / SOCKS5 代理。"""
        if key == 'direct':
            return '直連線路'
        addr = key[6:]  # 去掉 'proxy:' 前綴，得到 host:port
        for p in self.download_manager.socks_proxies.values():
            if f"{p.get('host')}:{p.get('port')}" == addr:
                return f"SOCKS5 代理 · {p.get('name') or addr}"
        return f"SOCKS5 代理 · {fallback}"

    def _update_line_chips(self, items):
        """依 (key, speed, name) 清單建立/更新分線速度膠囊。"""
        active_keys = {key for key, _, _ in items}
        # 移除已消失的 chip
        for key in list(self._line_chips):
            if key not in active_keys:
                chip = self._line_chips.pop(key)
                self.lines_chips_flow.removeWidget(chip)
                chip.deleteLater()
        # 建立/更新 chip
        for i, (key, speed, name) in enumerate(items):
            chip = self._line_chips.get(key)
            if chip is None:
                color = LINE_COLORS[i % len(LINE_COLORS)]
                chip = QLabel("")
                chip.setStyleSheet(
                    f"background-color: {color}; color: #fff; border-radius: 12px; "
                    f"padding: 5px 12px; font-size: 12px; font-weight: bold;")
                self._line_chips[key] = chip
                self.lines_chips_flow.addWidget(chip)
            chip.setText(f"{name}　{format_size(speed)}/s")
        self.lines_title.setText("分線速度" if items else "分線速度（下載中顯示）")

    def show_context_menu(self, position):
        row = self.task_table.rowAt(position.y())
        if row < 0:
            return

        # 右鍵點到未選取的列時，先只選取該列（與一般檔案管理員一致）
        model = self.task_table.selectionModel()
        index = self.task_table.model().index(row, 0)
        if not model.isSelected(index):
            self.task_table.selectRow(row)

        item = self.task_table.item(row, 0)
        if not item:
            return

        task_id = item.data(Qt.ItemDataRole.UserRole)
        if not task_id:
            return

        task = self.download_manager.task_ids.get(task_id)
        if not task:
            return

        menu = QMenu(self)

        # 添加複製下載連結選項
        copy_url_action = menu.addAction("複製下載連結")
        copy_url_action.triggered.connect(lambda: self.copy_download_url(task.url))

        # 添加分隔線
        menu.addSeparator()

        # 根據任務狀態顯示不同的菜單項
        if task.status in ('downloading', 'seeding'):
            pause_action = menu.addAction("暫停")
            pause_action.triggered.connect(lambda: self.pause_task(task_id))
        elif task.status == 'paused':
            resume_action = menu.addAction("恢復")
            resume_action.triggered.connect(lambda: self.resume_task(task_id))

        # 對錯誤或暫停的任務提供「重試」動作
        if task.status in ('error', 'paused'):
            retry_action = menu.addAction("重試")
            retry_action.triggered.connect(lambda: self.retry_task(task_id))

        # 對錯誤任務提供「查看錯誤」動作
        if task.status == 'error':
            view_error_action = menu.addAction("查看錯誤")
            view_error_action.triggered.connect(lambda: self.view_task_error(task_id))

        cancel_action = menu.addAction("刪除")
        cancel_action.triggered.connect(self.delete_selected_task)

        if task.status == 'completed':
            open_folder_action = menu.addAction("打開所在資料夾")
            open_folder_action.triggered.connect(lambda: self.open_folder(task.filepath))

        menu.exec(self.task_table.mapToGlobal(position))

    def pause_task(self, task_id):
        if self.download_manager.pause_task(task_id):
            # 更新會自動透過監控線程完成
            pass
        else:
            QMessageBox.warning(self, "錯誤", "無法暫停下載任務")

    def on_task_double_clicked(self, row, column):
        """雙擊任務切換狀態：下載中 → 暫停、已暫停 → 恢復、錯誤 → 重試。"""
        first_item = self.task_table.item(row, 0)
        if not first_item:
            return
        task_id = first_item.data(Qt.ItemDataRole.UserRole)
        if not task_id:
            return
        task = self.download_manager.task_ids.get(task_id)
        if not task:
            return
        if task.status in ('downloading', 'seeding'):
            self.pause_task(task_id)
        elif task.status == 'paused':
            self.resume_task(task_id)
        elif task.status == 'error':
            self.retry_task(task_id)

    def resume_task(self, task_id):
        if self.download_manager.resume_task(task_id):
            # 更新會自動透過監控線程完成
            pass
        else:
            QMessageBox.warning(self, "錯誤", "無法恢復下載任務")

    def _selected_task_ids(self):
        """回傳目前所有選取列的任務 ID（依列序，去重複）。"""
        rows = sorted({idx.row() for idx in self.task_table.selectionModel().selectedRows(0)})
        ids = []
        for row in rows:
            item = self.task_table.item(row, 0)
            if not item:
                continue
            task_id = item.data(Qt.ItemDataRole.UserRole)
            if task_id:
                ids.append(task_id)
        return ids

    def delete_selected_task(self):
        """刪除所有選取的任務；做種中（已完成下載）的任務直接移除，其餘先確認。"""
        ids = self._selected_task_ids()
        if not ids:
            return
        seeding_ids = []
        pending_ids = []
        pending_names = []
        for tid in ids:
            task = self.download_manager.task_ids.get(tid)
            if not task:
                continue
            if task.status == 'seeding':
                # 做種中的任務已完成下載，取消只會停做種、保留檔案，沒有可反悔的
                # 破壞性動作，直接移除、不彈確認視窗。
                seeding_ids.append(tid)
            else:
                pending_ids.append(tid)
                pending_names.append(task.filename)
        for tid in seeding_ids:
            task = self.download_manager.task_ids.get(tid)
            if task:
                # 成品已完整下載，先記一筆歷史再移除；cancel 後 task 會從管理器移除，
                # 無法再取用，故需在移除前完成記錄。
                self._record_history(task)
            self._remove_task(tid)
        if pending_ids:
            self._confirm_delete_tasks(pending_ids, pending_names)

    def _confirm_delete_tasks(self, task_ids, filenames):
        """列出檔名並詢問是否確認刪除，確認後逐一刪除。"""
        if len(task_ids) == 1:
            msg = f"確定要刪除「{filenames[0]}」嗎？\n已下載的暫存資料也會一併刪除。"
        else:
            listing = "\n".join(f"• {name}" for name in filenames)
            msg = (f"確定要刪除以下 {len(task_ids)} 個任務嗎？\n\n{listing}\n\n"
                   "已下載的暫存資料也會一併刪除。")
        reply = QMessageBox.question(
            self, "確認刪除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        failed = 0
        for tid in task_ids:
            if not self._remove_task(tid):
                failed += 1
        if failed:
            QMessageBox.warning(self, "錯誤", f"{failed} 個任務無法刪除")

    def _remove_task(self, task_id):
        """取消任務並從表格移除該列；回傳是否成功。"""
        if not self.download_manager.cancel_task(task_id):
            return False
        # 移除所有指向該 task_id 的列（含舊版遺留的重複列），避免孤兒列殘留
        rows = [
            row for row in range(self.task_table.rowCount())
            if self.task_table.item(row, 0) is not None
            and self.task_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == task_id
        ]
        for row in reversed(rows):
            self.task_table.removeRow(row)
        return True

    def cancel_task(self, task_id):
        """取消並刪除單一任務，刪除前先向使用者確認。"""
        task = self.download_manager.task_ids.get(task_id)
        if not task:
            return
        self._confirm_delete_tasks([task_id], [task.filename])

    def retry_task(self, task_id):
        """重試錯誤或暫停的下載任務"""
        if not self.download_manager.retry_task(task_id):
            QMessageBox.warning(self, "錯誤", "無法重試下載任務")

    def view_task_error(self, task_id):
        """顯示任務的錯誤訊息"""
        task = self.download_manager.task_ids.get(task_id)
        if not task:
            return
        prog = task.get_progress()
        err_msg = prog.get('error_message') or '未知錯誤'
        QMessageBox.warning(self, "錯誤詳情", err_msg)

    def pause_all_tasks(self):
        """暫停所有下載中與做種中的任務"""
        paused = 0
        for task_id, task in self.download_manager.task_ids.items():
            if task.status in ('downloading', 'seeding'):
                if self.download_manager.pause_task(task_id):
                    paused += 1
        self.statusBar().showMessage(f"已暫停 {paused} 個任務", 2000)

    def resume_all_tasks(self):
        """恢復所有已暫停的任務"""
        resumed = 0
        for task_id, task in self.download_manager.task_ids.items():
            if task.status == 'paused':
                if self.download_manager.resume_task(task_id):
                    resumed += 1
        self.statusBar().showMessage(f"已恢復 {resumed} 個任務", 2000)

    def clear_completed_tasks(self):
        """清除所有已完成的任務"""
        cleared = 0
        for task_id, task in list(self.download_manager.task_ids.items()):
            if task.status == 'completed':
                # 對已完成任務呼叫 cancel_task 是安全的，僅清理暫存檔
                self.download_manager.cancel_task(task_id)
                for row in range(self.task_table.rowCount()):
                    item = self.task_table.item(row, 0)
                    if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                        self.task_table.removeRow(row)
                        break
                cleared += 1
        self.statusBar().showMessage(f"已清除 {cleared} 個已完成任務", 2000)

    def open_folder(self, filepath):
        import subprocess
        import platform

        folder_path = os.path.dirname(filepath)

        if platform.system() == "Windows":
            os.startfile(folder_path)
        elif platform.system() == "Darwin":  # macOS
            subprocess.call(["open", folder_path])
        else:  # Linux
            subprocess.call(["xdg-open", folder_path])

    def open_file_location(self, filepath):
        """開啟檔案所在資料夾，並在檔案管理員中選取（聚焦）該檔案。"""
        import subprocess
        import platform

        if platform.system() == "Windows":
            # explorer /select 會開啟資料夾並選取該檔案
            subprocess.run(['explorer', '/select,', os.path.normpath(filepath)])
        elif platform.system() == "Darwin":  # macOS
            subprocess.call(["open", "-R", filepath])
        else:  # Linux 無統一選取檔案方式，退回開啟資料夾
            self.open_folder(filepath)

    def open_file(self, filepath):
        """用系統預設程式直接開啟檔案。"""
        import subprocess
        import platform

        if platform.system() == "Windows":
            os.startfile(filepath)
        elif platform.system() == "Darwin":  # macOS
            subprocess.call(["open", filepath])
        else:  # Linux
            subprocess.call(["xdg-open", filepath])

    def on_history_double_clicked(self, row, column):
        """歷史紀錄雙擊：直接開啟檔案。"""
        filepath_item = self.history_table.item(row, 2)
        if not filepath_item:
            return
        filepath = filepath_item.text()
        if filepath:
            self.open_file(filepath)

    # --- 系統匣（System Tray）支援 ---

    def _setup_tray(self):
        """建立系統匣圖示與右鍵選單；系統不支援時不建立。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray_icon = None
            return

        tray = QSystemTrayIcon(load_app_icon(), self)
        tray.setToolTip("多線程下載器")

        menu = QMenu(self)
        show_action = menu.addAction("顯示主視窗")
        show_action.triggered.connect(self.show_main_window)
        menu.addSeparator()
        quit_action = menu.addAction("退出")
        quit_action.triggered.connect(self.quit_app)
        tray.setContextMenu(menu)

        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray_icon = tray

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_main_window()

    def show_main_window(self):
        """從系統匣還原主視窗。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def bring_to_front(self):
        """把主視窗帶到前景並取得輸入焦點。

        當攔截到 Chrome 下載連結時呼叫，讓「選擇儲存位置」對話框能立即出現在
        使用者眼前。除 Qt 的還原／抬升／啟用外，再補上 Win32 ShowWindow /
        SetForegroundWindow，克服 Windows 對背景程序搶佔前景的限制。
        """
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if sys.platform == 'win32':
            try:
                hwnd = int(self.winId())
                user32 = ctypes.windll.user32
                kernel32 = ctypes.windll.kernel32
                # SW_RESTORE = 9：先確保視窗非最小化（縮到系統匣時靠這步還原）。
                user32.ShowWindow(hwnd, 9)

                # Windows 前景鎖會擋下背景程序直接搶佔焦點——Chrome 未縮小時
                # 正是這種情況。把本執行緒的輸入佇列掛接到目前前景視窗的執行緒，
                # 即可讓 SetForegroundWindow 放行。
                foreground = user32.GetForegroundWindow()
                fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
                cur_thread = kernel32.GetCurrentThreadId()
                attached = bool(fg_thread) and fg_thread != cur_thread
                if attached:
                    user32.AttachThreadInput(fg_thread, cur_thread, True)
                try:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        user32.AttachThreadInput(fg_thread, cur_thread, False)
            except Exception:
                pass

    def quit_app(self):
        """真正結束程式（觸發 closeEvent 執行清理）。"""
        self._force_quit = True
        self.close()
        # 視窗縮到系統匣時已用 hide() 隱藏，不再是「可見視窗」；Qt 的
        # quitOnLastWindowClosed 只會在「最後一個可見視窗」關閉時才退出，
        # 因此 close() 關閉隱藏視窗並不會讓事件迴圈結束。這裡補一次
        # quit() 確保行程真正終止（closeEvent 已同步完成清理）。
        if self._tray_icon is not None:
            self._tray_icon.hide()
        QApplication.quit()

    def _minimize_to_tray(self):
        self.hide()
        self._show_tray_hint()

    def _show_tray_hint(self):
        """首次縮到系統匣時以氣泡訊息提示仍在背景執行。"""
        if self._tray_hint_shown:
            return
        self._tray_hint_shown = True
        if self._tray_icon is not None:
            self._tray_icon.showMessage(
                "多線程下載器",
                "程式仍在背景執行，點擊此圖示可重新開啟主視窗",
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def _ask_close_action(self):
        """關閉時詢問要縮到系統匣或完全關閉，回傳 'tray' / 'quit' / 'cancel'。"""
        box = QMessageBox(self)
        box.setWindowTitle("關閉程式")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要縮到系統匣繼續下載，還是完全關閉程式？")
        box.setInformativeText("選擇「縮到系統匣」會記住此選擇，之後點關閉都直接縮到系統匣。")
        tray_btn = box.addButton("縮到系統匣", QMessageBox.ButtonRole.AcceptRole)
        quit_btn = box.addButton("完全關閉", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(tray_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked == tray_btn:
            return "tray"
        if clicked == quit_btn:
            return "quit"
        return "cancel"

    def closeEvent(self, event):
        # 點右上角關閉時：已勾選「關閉時縮到系統匣」就直接縮到系統匣；
        # 否則詢問要縮到系統匣或完全關閉，選「縮到系統匣」就記住此選擇。
        if not self._force_quit and QSystemTrayIcon.isSystemTrayAvailable():
            close_to_tray = (getattr(self, 'tray_checkbox', None)
                             and self.tray_checkbox.isChecked())
            if close_to_tray:
                self._minimize_to_tray()
                event.ignore()
                return

            action = self._ask_close_action()
            if action == "tray":
                self.tray_checkbox.setChecked(True)
                self._minimize_to_tray()
                event.ignore()
                return
            if action == "cancel":
                event.ignore()
                return
            # action == "quit"：繼續往下真正關閉
            self._force_quit = True

        # 真正關閉：保存進度並清理執行緒

        # 停止監控線程
        self.monitor_thread.stop()
        self.monitor_thread.wait()

        # 先嘗試優雅地取消所有測試線程
        for proxy_id, tester in list(self.proxy_testers.items()):
            logger.debug("嘗試取消代理 %s 的測試...", proxy_id)
            tester.cancel()

        # 然後等待它們完成
        for proxy_id, tester in list(self.proxy_testers.items()):
            logger.debug("等待代理 %s 的測試線程完成...", proxy_id)
            if not tester.wait(2000):  # 最多等待2秒
                logger.warning("代理 %s 的測試線程無法在2秒內完成，將被強制終止", proxy_id)
                try:
                    # 斷開連接信號以避免在對象被銷毀後調用
                    tester.test_finished.disconnect()
                except Exception as e:
                    logger.debug("斷開信號連接時出錯: %s", e)

        # 暫停所有仍在下載的任務，確保進度保存
        for task_id, task in self.download_manager.task_ids.items():
            if task.status == 'downloading':
                logger.info("關閉應用程式時自動暫停下載任務: %s", task.filename)
                self.download_manager.pause_task(task_id)

        # 保存配置文件
        self.download_manager.save_config()

        event.accept()

    def display_restored_tasks(self):
        """將恢復的未完成任務顯示到任務列表中"""
        logger.info("添加恢復的任務到列表中...")
        tasks = self.download_manager.get_all_tasks()
        for task_info in tasks:
            task_id = task_info['id']
            task = self.download_manager.task_ids.get(task_id)
            if task:
                logger.info("添加恢復的任務到列表: %s", task.filename)
                self.add_task_to_table(task_id, task)
                # 如果任務狀態是暫停的，保持暫停狀態
                # 如果是下載中或初始化狀態的，則自動開始下載
                if task.status in ['downloading', 'initialized']:
                    logger.info("自動開始恢復的任務: %s", task.filename)
                    self.download_manager.start_task(task_id)

    def copy_download_url(self, url):
        """複製下載URL到剪貼板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(url)

    def on_speed_limit_changed(self, value_kbs):
        """全域限速輸入變更時套用（0 = 不限速）。"""
        bytes_per_sec = value_kbs * 1024
        self.download_manager.set_speed_limit(bytes_per_sec)
        self.download_manager.save_config()
        if bytes_per_sec > 0:
            self.statusBar().showMessage(f"已設定全域限速: {format_size(bytes_per_sec)}/s", 2000)
        else:
            self.statusBar().showMessage("已取消全域限速", 2000)

    def on_bt_seed_hours_changed(self, hours):
        """BT 預設做種時數變更時套用（0 = 不做種）。"""
        self.download_manager.set_bt_seed_hours(hours)
        self.download_manager.save_config()
        if hours > 0:
            self.statusBar().showMessage(f"已設定 BT 預設做種時數: {hours} 小時", 2000)
        else:
            self.statusBar().showMessage("已設為 BT 下載完成後不做種", 2000)

    def on_bt_resume_interval_changed(self, seconds):
        """BT resume 自動保存間隔變更時套用（最小 1 秒）。"""
        self.download_manager.set_bt_resume_interval(seconds)
        self.download_manager.save_config()
        self.statusBar().showMessage(f"已設定 BT 續傳保存間隔: {seconds} 秒", 2000)

    def on_bt_max_connections_changed(self, value):
        """BT 直連線最大連線數變更時套用（0 = 不限）。"""
        self.download_manager.set_bt_max_connections(value)
        self.download_manager.save_config()
        if value > 0:
            self.statusBar().showMessage(f"已設定 BT 直連連線數上限: {value}", 2000)
        else:
            self.statusBar().showMessage("BT 直連連線數上限已改為不限（libtorrent 預設）", 2000)

    def on_bt_proxy_max_connections_changed(self, value):
        """BT SOCKS5 代理線最大連線數變更時套用（0 = 不限）。"""
        self.download_manager.set_bt_proxy_max_connections(value)
        self.download_manager.save_config()
        if value > 0:
            self.statusBar().showMessage(f"已設定 BT SOCKS5 線連線數上限: {value}", 2000)
        else:
            self.statusBar().showMessage("BT SOCKS5 線連線數上限已改為不限（libtorrent 預設）", 2000)

    def on_bt_max_tasks_per_line_changed(self, value):
        """每線同時 BT 任務上限變更時套用（0 = 不提醒）。"""
        self.download_manager.set_bt_max_tasks_per_line(value)
        self.download_manager.save_config()
        if value > 0:
            self.statusBar().showMessage(f"已設定每線同時 BT 任務上限: {value}", 2000)
        else:
            self.statusBar().showMessage("每線同時 BT 任務上限已關閉（不提醒）", 2000)

    def on_bt_force_tcp_changed(self, checked):
        """BT 僅用 TCP（停用 uTP）變更時套用。"""
        self.download_manager.set_bt_force_tcp(checked)
        self.download_manager.save_config()
        if checked:
            self.statusBar().showMessage("已啟用 BT 僅用 TCP（停用 uTP/UDP）", 2000)
        else:
            self.statusBar().showMessage("已恢復 BT 同時使用 TCP 與 uTP", 2000)

    def on_bt_upload_limit_changed(self, value_kbs):
        """BT 上傳限速變更時套用（0 = 不限速）。"""
        bytes_per_sec = value_kbs * 1024
        self.download_manager.set_bt_upload_rate(bytes_per_sec)
        self.download_manager.save_config()
        if bytes_per_sec > 0:
            self.statusBar().showMessage(f"已設定 BT 上傳限速: {format_size(bytes_per_sec)}/s", 2000)
        else:
            self.statusBar().showMessage("已取消 BT 上傳限速", 2000)

    def on_bt_listen_port_changed(self, port):
        """BT 直連監聽埠變更時套用（0 = 動態埠）。"""
        self.download_manager.set_bt_listen_port(port)
        self.download_manager.save_config()
        if port > 0:
            self.statusBar().showMessage(f"已設定 BT 監聽埠: {port}", 2000)
        else:
            self.statusBar().showMessage("BT 監聽埠已改為動態（自動）", 2000)

    def on_torrent_assoc_changed(self, checked):
        """勾選/取消 .torrent 檔案關聯，並處理 Win10/11 的預設程式確認。"""
        if checked:
            if not file_association.register():
                QMessageBox.warning(
                    self, "錯誤", "無法設定 .torrent 檔案關聯，請稍後再試。")
                self.assoc_checkbox.blockSignals(True)
                self.assoc_checkbox.setChecked(False)
                self.assoc_checkbox.blockSignals(False)
                return

            if file_association.is_registered():
                self.statusBar().showMessage("已設定 .torrent 檔案關聯", 3000)
                return

            # Win10/11：UserChoice 尚未指向本程式，需使用者經系統對話框確認一次
            QMessageBox.information(
                self, "設定預設程式",
                "Windows 需要你確認一次預設程式。\n"
                "請在接下來的「你要如何開啟此檔案」畫面中，\n"
                "點選「多線程下載器」並勾選「永遠使用此應用程式」。")
            if not file_association.trigger_system_dialog():
                QMessageBox.warning(
                    self, "提示",
                    "無法自動開啟系統設定畫面。\n"
                    "請手動對任意 .torrent 檔按右鍵 → 開啟方式 → 選擇其他應用程式。")
        else:
            file_association.unregister()
            self.statusBar().showMessage("已移除 .torrent 檔案關聯", 3000)

    def maybe_auto_check_update(self):
        """啟動時若使用者啟用「自動檢查更新」，於背景檢查一次。"""
        if not self.download_manager.auto_check_update:
            return
        if not updater.is_frozen():
            return  # 原始碼執行時沒有 .dist 可替換，略過
        threading.Thread(target=self._do_check_update, daemon=True).start()

    def on_auto_check_update_changed(self, checked):
        """切換「啟動時自動檢查更新」並儲存。"""
        self.download_manager.auto_check_update = checked
        self.download_manager.save_config()

    def on_reset_preferences(self):
        """確認後把偏好設定還原為預設值，並同步設置頁所有控制項。"""
        reply = QMessageBox.question(
            self, "還原預設設定",
            "確定要還原所有偏好設定到預設值嗎？\n\n"
            "會重置：儲存目錄、全域限速、下載預設值、BT 各項數值、\n"
            "自訂表頭、自動更新等。\n"
            "不會清除：SOCKS5 代理、下載歷史紀錄。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.download_manager.reset_preferences()

        # 同步 UI 控制項；blockSignals 避免觸發各自的 on_*_changed 再寫設定檔。
        self.dir_input.setText(self.download_manager.save_dir)
        self.speed_limit_spinbox.blockSignals(True)
        self.speed_limit_spinbox.setValue(0)
        self.speed_limit_spinbox.blockSignals(False)
        self.chunks_spinbox.blockSignals(True)
        self.chunks_spinbox.setValue(0)
        self.chunks_spinbox.blockSignals(False)
        self.threads_per_proxy_spinbox.blockSignals(True)
        self.threads_per_proxy_spinbox.setValue(6)
        self.threads_per_proxy_spinbox.blockSignals(False)
        self.bt_seed_spinbox.blockSignals(True)
        self.bt_seed_spinbox.setValue(0)
        self.bt_seed_spinbox.blockSignals(False)
        self.bt_upload_limit_spinbox.blockSignals(True)
        self.bt_upload_limit_spinbox.setValue(0)
        self.bt_upload_limit_spinbox.blockSignals(False)
        self.bt_resume_interval_spinbox.blockSignals(True)
        self.bt_resume_interval_spinbox.setValue(10)
        self.bt_resume_interval_spinbox.blockSignals(False)
        self.bt_max_connections_spinbox.blockSignals(True)
        self.bt_max_connections_spinbox.setValue(200)
        self.bt_max_connections_spinbox.blockSignals(False)
        self.bt_proxy_max_connections_spinbox.blockSignals(True)
        self.bt_proxy_max_connections_spinbox.setValue(30)
        self.bt_proxy_max_connections_spinbox.blockSignals(False)
        self.bt_max_tasks_per_line_spinbox.blockSignals(True)
        self.bt_max_tasks_per_line_spinbox.setValue(5)
        self.bt_max_tasks_per_line_spinbox.blockSignals(False)
        self.bt_force_tcp_checkbox.blockSignals(True)
        self.bt_force_tcp_checkbox.setChecked(False)
        self.bt_force_tcp_checkbox.blockSignals(False)
        self.bt_listen_port_spinbox.blockSignals(True)
        self.bt_listen_port_spinbox.setValue(6881)
        self.bt_listen_port_spinbox.blockSignals(False)
        self.auto_check_update_checkbox.blockSignals(True)
        self.auto_check_update_checkbox.setChecked(True)
        self.auto_check_update_checkbox.blockSignals(False)

        self.statusBar().showMessage("已還原預設設定", 3000)

    def on_check_update_clicked(self):
        """使用者手動觸發「立即檢查更新」。"""
        if not updater.is_frozen():
            self.update_status_label.setText("開發模式下不支援自動更新")
            return
        self.update_check_button.setEnabled(False)
        self.update_status_label.setText("檢查中...")
        threading.Thread(target=self._do_check_update, daemon=True).start()

    def _do_check_update(self):
        try:
            info = updater.check_update(version.APP_VERSION)
        except Exception as e:
            self.update_check_done.emit(("error", str(e)))
            return
        self.update_check_done.emit(("ok", info))

    def _on_update_check_done(self, result):
        self.update_check_button.setEnabled(True)
        kind, payload = result
        if kind == "error":
            self.update_status_label.setText("檢查失敗：" + payload)
            return
        if payload is None:
            self.update_status_label.setText("已是最新版本 " + version.APP_VERSION)
            return
        self.update_status_label.setText("發現新版本 " + payload["version"])
        reply = QMessageBox.question(
            self, "發現新版本",
            "發現新版本 {}，是否下載更新？\n\n"
            "下載會在背景進行，完成後再詢問是否重啟。".format(payload["version"]))
        if reply == QMessageBox.Yes:
            self._start_stage_update(payload)

    def _start_stage_update(self, info):
        self.update_status_label.setText("下載更新中...")
        threading.Thread(target=self._stage_worker, args=(info,), daemon=True).start()

    def _stage_worker(self, info):
        try:
            paths = updater.pending_paths()
            updater.stage_update(info, paths["new"])
        except Exception as e:
            self.update_stage_done.emit(False, str(e))
            return
        self.update_stage_done.emit(True, "")

    def _on_update_stage_done(self, success, err):
        if not success:
            self.update_status_label.setText("更新失敗：" + err)
            QMessageBox.warning(self, "更新失敗", err)
            return
        reply = QMessageBox.question(
            self, "更新已就緒",
            "更新已下載完成。關閉並重新啟動以完成更新？")
        if reply == QMessageBox.Yes:
            # 繞過系統匣的關閉攔截，確保真正退出，讓背景替換腳本得以交換目錄並重啟
            self._force_quit = True
            updater.spawn_apply_script()
            QApplication.quit()

    def open_header_dialog(self):
        """開啟自訂 HTTP 表頭對話框（每行一組 Key: Value）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("自訂 HTTP 表頭")
        dialog.resize(520, 320)
        layout = QVBoxLayout(dialog)

        hint = QLabel("每行一組表頭，格式「Key: Value」，例如：\n"
                      "Cookie: session=abc123\n"
                      "Referer: https://example.com/")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        text_edit = QPlainTextEdit()
        lines = [f"{k}: {v}" for k, v in self.download_manager.custom_headers.items()]
        text_edit.setPlainText("\n".join(lines))
        layout.addWidget(text_edit)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        ok_btn = QPushButton("儲存")
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        cancel_btn.clicked.connect(dialog.reject)
        ok_btn.clicked.connect(dialog.accept)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            headers = {}
            for line in text_edit.toPlainText().splitlines():
                line = line.strip()
                if not line or ':' not in line:
                    continue
                key, _, value = line.partition(':')
                key = key.strip()
                value = value.strip()
                if key:
                    headers[key] = value
            self.download_manager.set_custom_headers(headers)
            self.download_manager.save_config()
            self.statusBar().showMessage(f"已儲存 {len(headers)} 組自訂表頭", 3000)

    def on_task_added(self, task_id, task):
        """HTTP伺服器通知新增了任務時的回調"""
        # 在 UI 線程中執行添加操作
        QApplication.instance().postEvent(self, QEvent(QEvent.Type.User))

    def event(self, event):
        """處理事件，主要用於在應用激活時更新下載列表"""
        if event.type() == QEvent.Type.WindowActivate:
            logger.debug("窗口激活，刷新任務列表")
            tasks = self.download_manager.get_all_tasks()
            for task in tasks:
                self.update_task_progress(task)
        elif event.type() == QEvent.Type.User:
            # 刷新任務列表
            logger.debug("處理自定義事件：刷新任務列表")
            tasks = self.download_manager.get_all_tasks()
            for task_info in tasks:
                task_id = task_info['id']
                if task_id in self.download_manager.task_ids:
                    task = self.download_manager.task_ids[task_id]
                    # 檢查任務是否已在表格中
                    found = False
                    for row in range(self.task_table.rowCount()):
                        item = self.task_table.item(row, 0)
                        if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                            found = True
                            break

                    # 如果任務不在表格中，添加它
                    if not found:
                        logger.info("添加新任務到表格: ID=%s, 檔案名=%s",
                                    task_id, task.filename)
                        self.add_task_to_table(task_id, task)
            return True

        return super().event(event)

    # === SOCKS5 代理管理相關方法 ===

    def add_socks_proxy(self):
        """添加新的SOCKS5代理服務器"""
        name = self.socks_name_input.text().strip()
        host = self.socks_host_input.text().strip()
        port = self.socks_port_input.value()
        username = self.socks_username_input.text().strip() or None
        password = self.socks_password_input.text().strip() or None

        if not name:
            QMessageBox.warning(self, "錯誤", "請輸入代理名稱")
            return

        if not host:
            QMessageBox.warning(self, "錯誤", "請輸入代理主機地址")
            return

        # 添加到下載管理器
        proxy_id = self.download_manager.add_socks_proxy(name, host, port, username, password)
        if proxy_id:
            # 添加到表格
            self.add_proxy_to_table(proxy_id, {
                "name": name, "host": host, "port": port,
                "username": username or "", "status": "未測試"
            })

            # 清空輸入框
            self.socks_name_input.clear()
            self.socks_host_input.clear()
            self.socks_username_input.clear()
            self.socks_password_input.clear()
            self.socks_port_input.setValue(1080)
        else:
            QMessageBox.warning(self, "錯誤", "添加代理失敗，可能存在同名代理")

    def add_proxy_to_table(self, proxy_id, proxy):
        """將代理添加到表格中"""
        row = self.socks_table.rowCount()
        self.socks_table.insertRow(row)

        # 存儲代理ID
        self.socks_table.setItem(row, 0, QTableWidgetItem(proxy["name"]))
        self.socks_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, proxy_id)

        # 設置其他列
        self.socks_table.setItem(row, 1, QTableWidgetItem(proxy["host"]))
        self.socks_table.setItem(row, 2, QTableWidgetItem(str(proxy["port"])))
        self.socks_table.setItem(row, 3, QTableWidgetItem(proxy.get("username", "")))
        self.socks_table.setItem(row, 4, QTableWidgetItem(proxy["status"]))

        # 添加測試按鈕
        test_button = QPushButton("測試")
        test_button.clicked.connect(lambda: self.test_socks_proxy(proxy_id))
        self.socks_table.setCellWidget(row, 5, test_button)

    def update_proxy_status(self, proxy_id, status):
        """更新代理狀態"""
        # 查找對應的行
        for row in range(self.socks_table.rowCount()):
            item = self.socks_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == proxy_id:
                status_item = QTableWidgetItem(status)

                # 根據狀態設置顏色
                if status.startswith("可用"):
                    status_item.setForeground(QColor(Qt.GlobalColor.green))
                elif status.startswith("有限可用"):
                    # 有限可用使用黃色
                    status_item.setForeground(QColor(255, 165, 0))  # 橙色
                elif status.startswith("不可用"):
                    status_item.setForeground(QColor(Qt.GlobalColor.red))
                elif status == "測試中...":
                    status_item.setForeground(QColor(Qt.GlobalColor.blue))

                self.socks_table.setItem(row, 4, status_item)
                break

    def test_socks_proxy(self, proxy_id):
        """測試SOCKS5代理連接"""
        # 檢查是否已有測試線程在運行
        if proxy_id in self.proxy_testers and self.proxy_testers[proxy_id].isRunning():
            logger.debug("代理 %s 測試已在進行中，忽略請求", proxy_id)
            return

        # 先標記為測試中狀態
        self.update_proxy_status(proxy_id, "測試中...")

        # 禁用測試按鈕，避免重複點擊
        for row in range(self.socks_table.rowCount()):
            item = self.socks_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == proxy_id:
                test_button = self.socks_table.cellWidget(row, 5)
                if test_button:
                    test_button.setEnabled(False)
                    test_button.setText("測試中...")
                break

        # 在單獨的線程中運行測試
        proxy_tester = ProxyTester(self.download_manager, proxy_id)
        proxy_tester.test_finished.connect(self.on_proxy_test_finished)

        # 保存測試線程的引用，避免被過早釋放
        self.proxy_testers[proxy_id] = proxy_tester
        proxy_tester.start()

    def on_proxy_test_finished(self, proxy_id):
        """代理測試完成的回調"""
        logger.debug("代理 %s 測試完成，刷新UI顯示", proxy_id)
        # 直接從下載管理器獲取最新狀態
        self.refresh_proxy_status(proxy_id)

        # 從字典中移除測試線程的引用，允許線程正常結束
        if proxy_id in self.proxy_testers:
            # 確保線程完全結束
            self.proxy_testers[proxy_id].wait()
            # 移除線程引用
            self.proxy_testers.pop(proxy_id, None)
            logger.debug("代理 %s 的測試線程已安全結束", proxy_id)

    def refresh_proxy_status(self, proxy_id):
        """從下載管理器刷新代理狀態"""
        # 獲取最新狀態
        if proxy_id in self.download_manager.socks_proxies:
            status = self.download_manager.socks_proxies[proxy_id]['status']
            logger.debug("從下載管理器獲取到代理 %s 的最新狀態: %s", proxy_id, status)

            # 更新UI顯示
            self.update_proxy_status(proxy_id, status)

            # 恢復測試按鈕
            for row in range(self.socks_table.rowCount()):
                item = self.socks_table.item(row, 0)
                if item and item.data(Qt.ItemDataRole.UserRole) == proxy_id:
                    test_button = self.socks_table.cellWidget(row, 5)
                    if test_button:
                        test_button.setEnabled(True)
                        test_button.setText("測試")
                        logger.debug("測試按鈕已恢復")
                    break
        else:
            logger.debug("代理 %s 不存在於下載管理器中", proxy_id)

    def show_socks_context_menu(self, position):
        """顯示SOCKS5代理右鍵功能表"""
        menu = QMenu()

        # 獲取選中的行
        indexes = self.socks_table.selectedIndexes()
        if indexes:
            row = indexes[0].row()
            proxy_id = self.socks_table.item(row, 0).data(Qt.ItemDataRole.UserRole)

            # 添加功能表項
            test_action = menu.addAction("測試")
            delete_action = menu.addAction("刪除")

            # 顯示功能表
            action = menu.exec(self.socks_table.viewport().mapToGlobal(position))

            # 處理功能表選擇
            if action == test_action:
                self.test_socks_proxy(proxy_id)
            elif action == delete_action:
                self.delete_socks_proxy(proxy_id)

    def delete_socks_proxy(self, proxy_id):
        """刪除SOCKS5代理"""
        # 詢問用戶是否確定要刪除
        reply = QMessageBox.question(self, "確認刪除",
                                    "確定要刪除這個代理嗎？",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No)

        if reply == QMessageBox.StandardButton.Yes:
            # 從下載管理器中刪除代理
            if self.download_manager.delete_socks_proxy(proxy_id):
                # 從表格中刪除代理
                for row in range(self.socks_table.rowCount()):
                    item = self.socks_table.item(row, 0)
                    if item and item.data(Qt.ItemDataRole.UserRole) == proxy_id:
                        self.socks_table.removeRow(row)
                        break
            else:
                QMessageBox.warning(self, "錯誤", "刪除代理失敗")

    def load_socks_proxies(self):
        """載入所有已保存的SOCKS5代理到表格"""
        # 清空表格
        self.socks_table.setRowCount(0)

        # 獲取所有代理
        proxies = self.download_manager.get_all_proxies()

        # 添加到表格
        for proxy_id, proxy in proxies.items():
            self.add_proxy_to_table(proxy_id, proxy)

    # === 歷史下載紀錄相關方法 ===

    def load_history(self):
        """從下載管理器載入歷史下載紀錄到表格。"""
        self.history_table.setRowCount(0)
        for entry in self.download_manager.get_history():
            self.add_history_to_table(entry)

    def _record_history(self, task):
        """下載完成時記錄一筆歷史，並同步到表格。"""
        history_id = self.download_manager.add_history(
            task.filename, task.filepath, task.total_size, task.url)
        self.add_history_to_table({
            'id': history_id,
            'filename': task.filename,
            'filepath': task.filepath,
            'size': task.total_size,
            'completed_time': time.time(),
        })

    def add_history_to_table(self, entry):
        """把一筆歷史紀錄加到歷史表格。"""
        row = self.history_table.rowCount()
        self.history_table.insertRow(row)
        name_item = QTableWidgetItem(entry.get('filename', ''))
        name_item.setData(Qt.ItemDataRole.UserRole, entry.get('id'))
        self.history_table.setItem(row, 0, name_item)
        self.history_table.setItem(row, 1, QTableWidgetItem(format_size(entry.get('size', 0))))
        self.history_table.setItem(row, 2, QTableWidgetItem(entry.get('filepath', '')))
        self.history_table.setItem(row, 3, QTableWidgetItem(self._format_history_time(entry.get('completed_time'))))

    @staticmethod
    def _format_history_time(ts):
        if not ts:
            return ''
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
        except Exception:
            return ''

    def _selected_history_entries(self):
        """回傳目前選取的歷史紀錄（依列序）。"""
        rows = sorted({idx.row() for idx in self.history_table.selectionModel().selectedRows(0)})
        by_id = {e.get('id'): e for e in self.download_manager.get_history()}
        entries = []
        for row in rows:
            item = self.history_table.item(row, 0)
            if not item:
                continue
            history_id = item.data(Qt.ItemDataRole.UserRole)
            if history_id in by_id:
                entries.append(by_id[history_id])
        return entries

    def show_history_context_menu(self, position):
        row = self.history_table.rowAt(position.y())
        if row < 0:
            return
        model = self.history_table.selectionModel()
        index = self.history_table.model().index(row, 0)
        if not model.isSelected(index):
            self.history_table.selectRow(row)

        filepath_item = self.history_table.item(row, 2)
        filepath = filepath_item.text() if filepath_item else ''

        menu = QMenu(self)
        menu.addAction("只刪除紀錄").triggered.connect(
            lambda: self.delete_history_records(delete_files=False))
        menu.addAction("連同檔案刪除").triggered.connect(
            lambda: self.delete_history_records(delete_files=True))
        menu.addSeparator()
        if filepath:
            open_action = menu.addAction("打開所在資料夾")
            open_action.triggered.connect(lambda: self.open_folder(filepath))
        menu.exec(self.history_table.mapToGlobal(position))

    def delete_selected_history(self):
        """Delete 鍵：跳出視窗詢問要只刪紀錄或連同檔案刪除。"""
        entries = self._selected_history_entries()
        if not entries:
            return
        names = [e.get('filename', '') for e in entries]
        if len(entries) == 1:
            text = f"確定要刪除「{names[0]}」的紀錄嗎？"
        else:
            text = f"已選取 {len(entries)} 筆紀錄"
        text += "\n\n請選擇刪除方式："

        box = QMessageBox(self)
        box.setWindowTitle("刪除歷史紀錄")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(text)
        record_btn = box.addButton("只刪除紀錄", QMessageBox.ButtonRole.DestructiveRole)
        file_btn = box.addButton("連同檔案刪除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked == record_btn:
            self.delete_history_records(delete_files=False)
        elif clicked == file_btn:
            self.delete_history_records(delete_files=True)

    def delete_history_records(self, delete_files):
        """刪除選取的歷史紀錄；delete_files=True 時連同檔案刪除。"""
        entries = self._selected_history_entries()
        if not entries:
            return
        names = [e.get('filename', '') for e in entries]
        listing = "\n".join(f"• {n}" for n in names)
        if delete_files:
            msg = (f"確定要刪除以下 {len(entries)} 筆紀錄及其檔案嗎？\n\n{listing}\n\n"
                   "檔案刪除後無法復原。")
        else:
            msg = f"確定要刪除以下 {len(entries)} 筆紀錄嗎？\n\n{listing}\n\n（只移除紀錄，檔案會保留）"
        reply = QMessageBox.question(
            self, "確認刪除", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for e in entries:
            if delete_files:
                filepath = e.get('filepath', '')
                if filepath and os.path.isfile(filepath):
                    try:
                        os.remove(filepath)
                    except Exception as exc:
                        logger.warning("刪除檔案失敗: %s - %s", filepath, exc)
            self.download_manager.remove_history(e.get('id'))

        # 從表格移除選取的列
        ids = {e.get('id') for e in entries}
        row = 0
        while row < self.history_table.rowCount():
            item = self.history_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) in ids:
                self.history_table.removeRow(row)
            else:
                row += 1

# 主程序入口
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec()) 