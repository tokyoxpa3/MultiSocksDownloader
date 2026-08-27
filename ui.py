import sys
import os
import time
import threading
from collections import deque
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QProgressBar, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QMenu, QTabWidget,
    QCheckBox, QSizePolicy, QScrollArea, QFrame, QSystemTrayIcon,
    QDialog, QPlainTextEdit
)
from PySide6.QtCore import Qt, QTimer, Signal, QThread, QSize, QEvent, QPointF, QRectF
from PySide6.QtGui import QFont, QColor, QPainter, QPainterPath, QPen
from urllib.parse import urlparse, unquote, parse_qs

from downloader import DownloadManager, format_size
from app_icon import load_app_icon

# 格式化時間顯示
def format_time(seconds):
    if seconds < 60:
        return f"{seconds:.0f}秒"
    elif seconds < 3600:
        return f"{seconds/60:.0f}分{seconds%60:.0f}秒"
    else:
        return f"{seconds/3600:.0f}時{(seconds%3600)/60:.0f}分"

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
        print(f"開始測試代理 {self.proxy_id}")
        try:
            # 檢查是否被取消
            if self.is_canceled:
                print(f"代理 {self.proxy_id} 測試已被取消")
                return

            # 調用下載管理器的測試方法
            result = self.download_manager.test_socks_proxy(self.proxy_id)
            success, message = result
            print(f"測試結果: success={success}, message={message}")

            # 檢查是否被取消
            if self.is_canceled:
                print(f"代理 {self.proxy_id} 測試已被取消")
                return

            # 測試完成後發送信號
            self.test_finished.emit(self.proxy_id)
        except Exception as e:
            print(f"測試代理時出錯: {e}")
            # 即使出錯也發送信號，確保UI更新
            if not self.is_canceled:
                self.test_finished.emit(self.proxy_id)

    def cancel(self):
        """取消測試"""
        self.is_canceled = True
        print(f"代理 {self.proxy_id} 測試被標記為取消")

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
        gap = 2.0
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
            if frac > 0:
                if frac >= 0.999:
                    color = QColor(46, 204, 113)   # 完成 綠
                elif active:
                    color = QColor(52, 152, 219)   # 下載中 藍
                else:
                    color = QColor(126, 191, 126)  # 已下載閒置 淡綠
                # 完成則整格填滿，進行中由左往右部分填充
                fill_w = cell_w if frac >= 0.999 else cell_w * frac
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


# 新增下載對話框：貼 URL → 自動帶出檔名（可自訂）→ 確認加入佇列
class AddDownloadDialog(QDialog):
    def __init__(self, parent=None, default_save_dir=""):
        super().__init__(parent)
        self.setWindowTitle("新增下載")
        self.setMinimumWidth(520)

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

        layout.addWidget(QLabel("儲存位置："))
        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(default_save_dir or "")
        browse_btn = QPushButton("瀏覽...")
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(self.dir_edit)
        dir_row.addWidget(browse_btn)
        layout.addLayout(dir_row)

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
        self.url_edit.textChanged.connect(self._on_url_changed)

    def _on_url_changed(self):
        urls = self.urls()
        if not urls:
            return
        name = extract_filename_from_url(urls[0])
        if name:
            self.name_edit.setText(name)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "選擇儲存位置", self.dir_edit.text())
        if d:
            self.dir_edit.setText(d)

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

    def save_dir(self):
        return self.dir_edit.text().strip()


# 主窗口
class MainWindow(QMainWindow):
    def __init__(self, download_manager=None):
        super().__init__()

        # 使用傳入的 download_manager 或創建新的
        self.download_manager = download_manager if download_manager is not None else DownloadManager()
        self.task_table = None  # 初始化為 None

        # 存儲正在運行的代理測試線程，避免被過早釋放
        self.proxy_testers = {}

        # 已通知完成的任務 ID，避免重複通知
        self.notified_completed = set()

        # 系統匣圖示與相關狀態
        self._tray_icon = None
        self._force_quit = False
        self._tray_hint_shown = False

        self.setup_ui()  # 首先設置 UI，確保 task_table 被初始化

        # 更新保存目錄顯示
        self.dir_input.setText(self.download_manager.save_dir)

        # 載入已保存的全域限速設定（blockSignals 避免啟動時觸發多餘儲存/提示）
        self.speed_limit_spinbox.blockSignals(True)
        self.speed_limit_spinbox.setValue(self.download_manager.speed_limit // 1024)
        self.speed_limit_spinbox.blockSignals(False)

        self.monitor_thread = MonitorThread(self.download_manager)
        self.monitor_thread.tasks_updated.connect(self.on_tasks_updated)
        self.monitor_thread.start()

        # 剪貼簿自動偵測計時器（每秒檢查一次）
        self._last_clipboard_url = None
        self.clipboard_timer = QTimer(self)
        self.clipboard_timer.timeout.connect(self._check_clipboard)
        self.clipboard_timer.start(1000)

        # 恢復未完成的任務
        count = self.download_manager.scan_unfinished_tasks()
        if count > 0:
            # 不再顯示確認對話框，直接恢復
            print(f"已自動恢復 {count} 個未完成的下載任務")
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
        self.stats_label = QLabel("合計速度: 0 B/s | 進行中任務: 0")
        self.stats_label.setStyleSheet("font-weight: bold;")
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
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)

        # HTTP 伺服器（供 Chrome 擴充功能連線）
        server_group = QGroupBox("HTTP 伺服器（供 Chrome 擴充功能連線）")
        server_vlayout = QHBoxLayout(server_group)
        server_label = QLabel("位址:")
        self.server_url_label = QLabel("http://localhost:8765")
        self.server_url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.server_status_label = QLabel("啟動中...")
        copy_url_btn = QPushButton("複製")
        copy_url_btn.clicked.connect(self.copy_server_url)
        server_vlayout.addWidget(server_label)
        server_vlayout.addWidget(self.server_url_label)
        server_vlayout.addWidget(self.server_status_label)
        server_vlayout.addWidget(copy_url_btn)
        server_vlayout.addStretch()
        settings_layout.addWidget(server_group)

        # 儲存設定
        save_group = QGroupBox("儲存設定")
        save_form = QFormLayout(save_group)

        self.dir_input = QLineEdit()
        self.dir_input.setReadOnly(True)
        dir_button = QPushButton("瀏覽...")
        dir_button.clicked.connect(self.select_save_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_input)
        dir_row.addWidget(dir_button)
        save_form.addRow("預設儲存目錄:", dir_row)

        self.speed_limit_spinbox = QSpinBox()
        self.speed_limit_spinbox.setRange(0, 1024 * 1024)
        self.speed_limit_spinbox.setValue(0)
        self.speed_limit_spinbox.setSpecialValueText("不限速")
        self.speed_limit_spinbox.valueChanged.connect(self.on_speed_limit_changed)
        save_form.addRow("全域限速 (KB/s):", self.speed_limit_spinbox)

        settings_layout.addWidget(save_group)

        # 下載預設值
        dl_group = QGroupBox("下載預設值")
        dl_form = QFormLayout(dl_group)

        self.chunks_spinbox = QSpinBox()
        self.chunks_spinbox.setRange(0, 2000)
        self.chunks_spinbox.setValue(0)
        self.chunks_spinbox.setSpecialValueText("自動")
        dl_form.addRow("分片數:", self.chunks_spinbox)

        self.threads_per_proxy_spinbox = QSpinBox()
        self.threads_per_proxy_spinbox.setRange(1, 32)
        self.threads_per_proxy_spinbox.setValue(6)
        dl_form.addRow("每代理線程數:", self.threads_per_proxy_spinbox)

        settings_layout.addWidget(dl_group)

        # 其他
        misc_group = QGroupBox("其他")
        misc_layout = QVBoxLayout(misc_group)

        self.header_button = QPushButton("自訂 HTTP 表頭")
        self.header_button.clicked.connect(self.open_header_dialog)
        misc_layout.addWidget(self.header_button)

        self.clipboard_checkbox = QCheckBox("剪貼簿自動偵測（複製連結即自動下載）")
        self.clipboard_checkbox.setToolTip("開啟後，複製網址/連結會自動加入下載")
        misc_layout.addWidget(self.clipboard_checkbox)

        self.tray_checkbox = QCheckBox("最小化視窗時縮到系統匣")
        self.tray_checkbox.setToolTip("開啟後，點最小化鈕會縮到系統匣繼續下載")
        self.tray_checkbox.setChecked(True)
        misc_layout.addWidget(self.tray_checkbox)

        settings_layout.addWidget(misc_group)
        settings_layout.addStretch()

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
        return url.startswith(('http://', 'https://', 'ftp://', 'magnet:'))

    def add_download(self):
        """開啟「新增下載」對話框，確認後加入任務佇列。"""
        dialog = AddDownloadDialog(self, self.download_manager.save_dir)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._add_urls(
            dialog.urls(),
            filename=dialog.filename(),
            save_dir=dialog.save_dir(),
        )

    def _add_urls(self, urls, filename=None, silent=False, save_dir=None):
        """新增一批 URL 下載任務。回傳是否至少新增了一個任務。"""
        if not urls:
            return False

        chunks_per_part = self.chunks_spinbox.value()
        threads_per_proxy = self.threads_per_proxy_spinbox.value()
        save_dir = save_dir or self.dir_input.text().strip()

        # 批次輸入多個 URL 時，檔案名稱不應套用到所有任務
        if len(urls) > 1:
            filename = None

        # 分離磁力連結（BT 下載暫不支援）
        magnet_urls = [u for u in urls if u.startswith('magnet:')]
        http_urls = [u for u in urls if not u.startswith('magnet:')]
        invalid = [u for u in urls if not self._is_valid_url(u)]

        if invalid:
            if not silent:
                QMessageBox.warning(self, "錯誤", f"無效的URL格式\nURL: {invalid[0]}")
            return False

        if magnet_urls and not silent:
            QMessageBox.information(self, "磁力連結",
                                    "偵測到磁力連結，目前僅支援 HTTP/HTTPS/FTP 直連下載。")

        if not http_urls:
            return False

        added = 0
        for url in http_urls:
            try:
                # add_task 只做簿記、不碰網路，可在 UI 執行緒安全執行
                task_id = self.download_manager.add_task(
                    url,
                    filename,
                    save_dir=save_dir,
                    use_proxy=True,
                    chunks_per_part=chunks_per_part,
                    threads_per_proxy=threads_per_proxy
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
                print(f"下載任務添加失敗: {e}")
                if not silent:
                    QMessageBox.critical(self, "錯誤", f"添加下載任務失敗:\n{e}\n\nURL: {url}")
        return added > 0

    # --- 拖放支援 ---
    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        md = event.mimeData()
        urls = []

        if md.hasUrls():
            for u in md.urls():
                local = u.toLocalFile()
                if local and os.path.isfile(local):
                    # 讀取拖入的文字檔內容（每行一個 URL）
                    try:
                        with open(local, 'r', encoding='utf-8', errors='ignore') as f:
                            for line in f.read().splitlines():
                                line = line.strip()
                                if line:
                                    urls.append(line)
                    except Exception:
                        pass
                else:
                    urls.append(u.toString())

        if md.hasText():
            for line in md.text().splitlines():
                line = line.strip()
                if line:
                    urls.append(line)

        valid = [u for u in urls if self._is_valid_url(u)]
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
        if first.startswith('magnet:'):
            self.statusBar().showMessage("偵測到磁力連結，但 BT 下載尚未支援", 4000)
            return
        if self._add_urls([first], silent=True):
            self.statusBar().showMessage(f"已自動加入下載: {first[:60]}", 4000)

    def _start_task_in_background(self, task_id, url):
        """在背景執行緒啟動下載任務，避免 start_task 的網路探測阻塞 UI。"""
        try:
            if not self.download_manager.start_task(task_id):
                print(f"任務 {task_id} 啟動失敗: {url}")
                # 失敗時 start_task 已把 task.status 設為 'error'，
                # MonitorThread 會自動把狀態同步到表格。
        except Exception as e:
            print(f"任務 {task_id} 執行失敗: {e}")

    def add_task_to_table(self, task_id, task):
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
        self.task_table.setItem(row, 4, QTableWidgetItem("0 B/s"))
        self.task_table.setItem(row, 5, QTableWidgetItem("計算中..."))

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
            print(f"檢測到新任務 (可能來自 HTTP 伺服器): {task.filename}，添加到 UI 表格")
            self.add_task_to_table(task_id, task)

        # 查找對應的行（可能是剛添加的）
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                # 更新大小
                if progress['total_size'] > 0:
                    size_text = f"{format_size(progress['downloaded_size'])}/{format_size(progress['total_size'])}"
                else:
                    size_text = format_size(progress['downloaded_size'])
                self.task_table.setItem(row, 1, QTableWidgetItem(size_text))

                # 更新進度條
                progress_bar = self.task_table.cellWidget(row, 2)
                progress_bar.setValue(int(progress['percentage']))

                # 更新狀態
                status = progress['status']
                self.task_table.setItem(row, 3, QTableWidgetItem(self.get_status_text(status)))

                # 更新速度
                if status in ['paused', 'error', 'completed', 'canceled']:
                    # 暫停、錯誤或完成狀態下顯示 0 速度
                    speed_text = "0 B/s"
                else:
                    speed_text = f"{format_size(progress['speed'])}/s"
                self.task_table.setItem(row, 4, QTableWidgetItem(speed_text))

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
                elif progress['speed'] > 0 and progress['total_size'] > 0:
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
                elif status == 'error':
                    status_item.setForeground(QColor(Qt.GlobalColor.red))
                    # 錯誤任務在狀態格顯示錯誤訊息 tooltip
                    err_msg = progress.get('error_message') or '未知錯誤'
                    status_item.setToolTip(err_msg)
                elif status == 'paused':
                    status_item.setForeground(QColor(Qt.GlobalColor.blue))

                # 偵測任務轉變為完成：記錄歷史、通知，並從下載管理列表移除
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
            print(f"系統列通知失敗: {e}")
        # 退回：警告主視窗 + 狀態列訊息
        QApplication.instance().alert(self)
        self.statusBar().showMessage(msg, 5000)

    def on_tasks_updated(self, tasks):
        """監控線程單次批次更新：逐列刷新後，全域統計只算一次。"""
        total_speed = 0.0
        active_count = 0
        for task_data in tasks:
            self.update_task_progress(task_data)
            prog = task_data['progress']
            if prog['status'] == 'downloading':
                total_speed += prog.get('speed') or 0
                active_count += 1
        self._update_global_stats(total_speed, active_count)

    def _update_global_stats(self, total_speed=None, active_count=None):
        """更新全域統計：合計下載速度與進行中任務數（可傳入已算好的值避免重複計算）"""
        if total_speed is None or active_count is None:
            total_speed = 0.0
            active_count = 0
            for task in self.download_manager.get_all_tasks():
                prog = task['progress']
                if prog['status'] == 'downloading':
                    total_speed += prog.get('speed') or 0
                    active_count += 1
        self.stats_label.setText(
            f"合計速度: {format_size(total_speed)}/s | 進行中任務: {active_count}"
        )
        self.speed_chart.add_sample(total_speed)

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
        if task.status == 'downloading':
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
        if task.status == 'downloading':
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
        """刪除所有選取的任務，先列出檔名並確認。"""
        ids = self._selected_task_ids()
        if not ids:
            return
        filenames = []
        valid_ids = []
        for tid in ids:
            task = self.download_manager.task_ids.get(tid)
            if task:
                filenames.append(task.filename)
                valid_ids.append(tid)
        if not valid_ids:
            return
        self._confirm_delete_tasks(valid_ids, filenames)

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
        for row in range(self.task_table.rowCount()):
            item = self.task_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                self.task_table.removeRow(row)
                break
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
        """暫停所有下載中的任務"""
        paused = 0
        for task_id, task in self.download_manager.task_ids.items():
            if task.status == 'downloading':
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

    def quit_app(self):
        """真正結束程式（觸發 closeEvent 執行清理）。"""
        self._force_quit = True
        self.close()

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

    def changeEvent(self, event):
        """最小化視窗時依設定縮到系統匣。"""
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            if (getattr(self, 'tray_checkbox', None) and self.tray_checkbox.isChecked()
                    and QSystemTrayIcon.isSystemTrayAvailable()):
                QTimer.singleShot(0, self._minimize_to_tray)
        super().changeEvent(event)

    def _ask_close_action(self):
        """關閉時詢問要縮到系統匣或完全關閉，回傳 'tray' / 'quit' / 'cancel'。"""
        box = QMessageBox(self)
        box.setWindowTitle("關閉程式")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("要縮到系統匣繼續下載，還是完全關閉程式？")
        box.setInformativeText("縮到系統匣後，下載會在背景繼續進行。")
        tray_btn = box.addButton("縮到系統匣", QMessageBox.ButtonRole.AcceptRole)
        quit_btn = box.addButton("完全關閉", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(tray_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked == tray_btn:
            return "tray"
        if clicked == quit_btn:
            return "quit"
        return "cancel"

    def closeEvent(self, event):
        # 點右上角關閉時，彈窗詢問要縮到系統匣或完全關閉
        if not self._force_quit and QSystemTrayIcon.isSystemTrayAvailable():
            action = self._ask_close_action()
            if action == "tray":
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
            print(f"嘗試取消代理 {proxy_id} 的測試...")
            tester.cancel()

        # 然後等待它們完成
        for proxy_id, tester in list(self.proxy_testers.items()):
            print(f"等待代理 {proxy_id} 的測試線程完成...")
            if not tester.wait(2000):  # 最多等待2秒
                print(f"代理 {proxy_id} 的測試線程無法在2秒內完成，將被強制終止")
                try:
                    # 斷開連接信號以避免在對象被銷毀後調用
                    tester.test_finished.disconnect()
                except Exception as e:
                    print(f"斷開信號連接時出錯: {e}")

        # 暫停所有仍在下載的任務，確保進度保存
        for task_id, task in self.download_manager.task_ids.items():
            if task.status == 'downloading':
                print(f"關閉應用程式時自動暫停下載任務: {task.filename}")
                self.download_manager.pause_task(task_id)

        # 保存配置文件
        self.download_manager.save_config()

        event.accept()

    def display_restored_tasks(self):
        """將恢復的未完成任務顯示到任務列表中"""
        print("添加恢復的任務到列表中...")
        tasks = self.download_manager.get_all_tasks()
        for task_info in tasks:
            task_id = task_info['id']
            task = self.download_manager.task_ids.get(task_id)
            if task:
                print(f"添加恢復的任務到列表: {task.filename}")
                self.add_task_to_table(task_id, task)
                # 如果任務狀態是暫停的，保持暫停狀態
                # 如果是下載中或初始化狀態的，則自動開始下載
                if task.status in ['downloading', 'initialized']:
                    print(f"自動開始恢復的任務: {task.filename}")
                    self.download_manager.start_task(task_id)

    def update_server_status(self, url=None, is_running=False):
        """更新 HTTP 伺服器狀態顯示"""
        if url:
            self.server_url_label.setText(url)

        if is_running:
            self.server_status_label.setText("運行中")
            self.server_status_label.setStyleSheet("color: green;")
        else:
            self.server_status_label.setText("已停止")
            self.server_status_label.setStyleSheet("color: red;")

    def copy_server_url(self):
        """複製伺服器 URL 到剪貼簿"""
        url = self.server_url_label.text()
        if url:
            clipboard = QApplication.clipboard()
            clipboard.setText(url)
            # 顯示短暫的提示
            self.statusBar().showMessage("已複製伺服器 URL 到剪貼簿", 2000)

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
            print("窗口激活，刷新任務列表")
            tasks = self.download_manager.get_all_tasks()
            for task in tasks:
                self.update_task_progress(task)
        elif event.type() == QEvent.Type.User:
            # 刷新任務列表
            print("處理自定義事件：刷新任務列表")
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
                        print(f"添加新任務到表格: ID={task_id}, 檔案名={task.filename}")
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
            print(f"代理 {proxy_id} 測試已在進行中，忽略請求")
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
        print(f"代理 {proxy_id} 測試完成，刷新UI顯示")
        # 直接從下載管理器獲取最新狀態
        self.refresh_proxy_status(proxy_id)

        # 從字典中移除測試線程的引用，允許線程正常結束
        if proxy_id in self.proxy_testers:
            # 確保線程完全結束
            self.proxy_testers[proxy_id].wait()
            # 移除線程引用
            self.proxy_testers.pop(proxy_id, None)
            print(f"代理 {proxy_id} 的測試線程已安全結束")

    def refresh_proxy_status(self, proxy_id):
        """從下載管理器刷新代理狀態"""
        # 獲取最新狀態
        if proxy_id in self.download_manager.socks_proxies:
            status = self.download_manager.socks_proxies[proxy_id]['status']
            print(f"從下載管理器獲取到代理 {proxy_id} 的最新狀態: {status}")

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
                        print(f"測試按鈕已恢復")
                    break
        else:
            print(f"代理 {proxy_id} 不存在於下載管理器中")

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
                        print(f"刪除檔案失敗: {filepath} - {exc}")
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