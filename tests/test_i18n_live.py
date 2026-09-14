"""i18n 即時切換與系統語系偵測測試。

以 offscreen 平台建立 Qt 元件，驗證切換語系後既有視窗的文字會就地更新，
不需重新啟動；另外驗證系統語系字串的對應規則。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import i18n  # noqa: E402

try:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton, QTabWidget, QWidget
    HAS_QT = True
except Exception:  # pragma: no cover - 環境缺 PySide6 時整組跳過
    HAS_QT = False


class TestMatchLang(unittest.TestCase):
    def test_exact_and_prefix(self):
        self.assertEqual(i18n._match_lang("en_US"), "en_US")
        self.assertEqual(i18n._match_lang("en-GB"), "en_US")
        self.assertEqual(i18n._match_lang("ja_JP"), "ja_JP")
        self.assertEqual(i18n._match_lang("pt-PT"), "pt_BR")

    def test_chinese_variants(self):
        self.assertEqual(i18n._match_lang("zh_TW"), "zh_TW")
        self.assertEqual(i18n._match_lang("zh-Hant-TW"), "zh_TW")
        self.assertEqual(i18n._match_lang("zh_HK"), "zh_TW")
        self.assertEqual(i18n._match_lang("zh_CN"), "zh_CN")
        self.assertEqual(i18n._match_lang("zh-Hans-CN"), "zh_CN")

    def test_unsupported_returns_none(self):
        self.assertIsNone(i18n._match_lang("hu_HU"))
        self.assertIsNone(i18n._match_lang(""))
        self.assertIsNone(i18n._match_lang(None))

    def test_detect_returns_supported_code(self):
        self.assertIn(i18n.detect_system_lang(), i18n.SUPPORTED_LANGS)

    @unittest.skipUnless(HAS_QT, "PySide6 not available")
    def test_unsupported_system_falls_back_to_english(self):
        from unittest import mock
        from PySide6.QtCore import QLocale

        class FakeLocale:
            def name(self):
                return "hu_HU"

            def uiLanguages(self):
                return ["hu-HU", "hu"]

        with mock.patch.object(QLocale, "system", staticmethod(lambda: FakeLocale())), \
                mock.patch("locale.getlocale", return_value=(None, None)):
            self.assertEqual(i18n.detect_system_lang(), "en_US")

    @unittest.skipUnless(HAS_QT, "PySide6 not available")
    def test_no_detectable_locale_returns_default(self):
        from unittest import mock
        from PySide6.QtCore import QLocale

        with mock.patch.object(QLocale, "system", side_effect=RuntimeError("no qt")), \
                mock.patch("locale.getlocale", return_value=(None, None)):
            self.assertEqual(i18n.detect_system_lang(), i18n.DEFAULT_LANG)


@unittest.skipUnless(HAS_QT, "PySide6 not available")
class TestLiveRetranslate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # 用獨立的 namespace 換掉建構子名稱，避免影響其他測試模組的 globals
        cls.ns = {"QLabel": QLabel, "QPushButton": QPushButton, "QAction": QAction}
        i18n.install_qt_translation(cls.ns)

    def setUp(self):
        i18n.i18n.load("zh_TW")
        i18n.retranslate()

    def tearDown(self):
        i18n.i18n.load("zh_TW")
        i18n.retranslate()

    def test_widgets_retranslate_in_place(self):
        label = self.ns["QLabel"]("下載中")
        button = self.ns["QPushButton"]("已完成")
        action = self.ns["QAction"]("顯示主視窗")
        combo = QComboBox()
        combo.addItems(["下載中", "已完成"])
        tabs = QTabWidget()
        tabs.addTab(QWidget(), "下載管理")

        self.assertEqual(label.text(), "下載中")

        i18n.i18n.load("en_US")
        i18n.retranslate()

        self.assertEqual(label.text(), "Downloading")
        self.assertEqual(button.text(), "Completed")
        self.assertEqual(action.text(), "Show main window")
        self.assertEqual(combo.itemText(0), "Downloading")
        self.assertEqual(combo.itemText(1), "Completed")
        self.assertEqual(tabs.tabText(0), "Downloads")

    def test_switch_back_to_source(self):
        label = self.ns["QLabel"]("下載中")
        i18n.i18n.load("en_US")
        i18n.retranslate()
        self.assertEqual(label.text(), "Downloading")

        i18n.i18n.load("zh_TW")
        i18n.retranslate()
        self.assertEqual(label.text(), "下載中")

    def test_non_translatable_text_is_untouched(self):
        label = self.ns["QLabel"]("report_2026-09-14.mp4")
        i18n.i18n.load("en_US")
        i18n.retranslate()
        self.assertEqual(label.text(), "report_2026-09-14.mp4")

    def test_destroyed_widget_does_not_break_retranslate(self):
        label = self.ns["QLabel"]("下載中")
        label.deleteLater()
        self.app.processEvents()
        i18n.i18n.load("en_US")
        i18n.retranslate()  # 不應拋出例外


if __name__ == "__main__":
    unittest.main()