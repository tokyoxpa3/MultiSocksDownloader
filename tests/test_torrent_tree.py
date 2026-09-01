import os
import sys
import unittest

# 必須在匯入 ui.py（其會建立 Qt 元件）之前設為 offscreen，才能在無顯示環境執行
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6 import QtCore, QtWidgets
from ui import TorrentDialog


class TorrentFileTreeCheckStateTest(unittest.TestCase):
    """迴歸測試：取消勾選單一檔案時，不應把整棵檔案樹連鎖成「-」。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _make_dialog(self):
        # 跳過 __init__（它需要 libtorrent 種子來源與 download_manager），
        # 只取用勾選狀態處理所需的屬性與已連接 itemChanged 的檔案樹。
        dlg = TorrentDialog.__new__(TorrentDialog)
        dlg._is_torrent = True
        dlg._updating_tree = False
        dlg.selected_size_label = QtWidgets.QLabel()
        dlg.file_tree = QtWidgets.QTreeWidget()
        dlg.file_tree.itemChanged.connect(dlg._on_file_item_changed)
        return dlg

    @staticmethod
    def _dir_item(name):
        d = QtWidgets.QTreeWidgetItem([name, ''])
        d.setFlags(d.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                   | QtCore.Qt.ItemFlag.ItemIsAutoTristate)
        d.setCheckState(0, QtCore.Qt.CheckState.Checked)
        return d

    def _build_tree(self, dlg):
        """建立 根目錄 → 子目錄 → 兩個檔案 的樹，並回傳節點以供斷言。"""
        dlg._updating_tree = True  # 建樹期間抑制 itemChanged 處理，避免半成品狀態干擾
        try:
            root = dlg.file_tree.invisibleRootItem()
            root_dir = self._dir_item('root_dir')
            root.addChild(root_dir)
            sub_dir = self._dir_item('sub_dir')
            root_dir.addChild(sub_dir)

            files = []
            for i in range(2):
                f = QtWidgets.QTreeWidgetItem([f'file{i}.bin', '1.0 KB'])
                f.setFlags(f.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                f.setCheckState(0, QtCore.Qt.CheckState.Checked)
                f.setData(0, QtCore.Qt.ItemDataRole.UserRole, i)
                f.setData(1, QtCore.Qt.ItemDataRole.UserRole + 2, 1024)
                sub_dir.addChild(f)
                files.append(f)
            dlg._file_items = files
        finally:
            dlg._updating_tree = False
        return root_dir, sub_dir, files

    def test_uncheck_one_file_keeps_siblings_checked(self):
        """取消勾選一個檔案：兄弟檔案仍應勾選，父/祖目錄僅為「部分勾選」。"""
        dlg = self._make_dialog()
        root_dir, sub_dir, files = self._build_tree(dlg)

        files[0].setCheckState(0, QtCore.Qt.CheckState.Unchecked)

        self.assertEqual(files[0].checkState(0), QtCore.Qt.CheckState.Unchecked)
        # 兄弟檔案必須仍是「勾選」，不能連鎖成「-」
        self.assertEqual(files[1].checkState(0), QtCore.Qt.CheckState.Checked)
        self.assertEqual(sub_dir.checkState(0), QtCore.Qt.CheckState.PartiallyChecked)
        self.assertEqual(root_dir.checkState(0), QtCore.Qt.CheckState.PartiallyChecked)

    def test_apply_to_children_ignores_partially_checked(self):
        """「部分勾選」是目錄彙總狀態，_apply_to_children 不應將其向下套用到子項目。"""
        dlg = self._make_dialog()
        _, sub_dir, files = self._build_tree(dlg)

        dlg._apply_to_children(sub_dir, QtCore.Qt.CheckState.PartiallyChecked)

        for f in files:
            self.assertEqual(f.checkState(0), QtCore.Qt.CheckState.Checked)


if __name__ == '__main__':
    unittest.main()
