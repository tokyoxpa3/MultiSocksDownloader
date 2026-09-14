"""多國語系支援模組。

比照 NetRedirector 的 locale 機制：以「原始字串（正體中文）」為 key 的 JSON
對照表，查不到就回退顯示原文，因此缺少翻譯只會讓該字串維持中文，不會讓介面
空白或崩潰。

與參考專案不同的地方在於，本專案的使用者字串大多直接寫在 Qt 建構子與 setter
裡，逐處包 t() 不切實際，因此改在 Qt 文字 setter 與建構子的邊界統一攔截：
`install_qt_translation(namespace)` 會修補 Qt 的文字 setter，並把 namespace
（呼叫端模組的 globals）中帶文字的建構子名稱換成會自動翻譯的子類別。字串含
`{}` 者視為樣板（例如 `"已加入下載: {}"`），以規則運算式比對後代換。
"""

from __future__ import annotations

import json
import os
import re
import sys
import weakref

DEFAULT_LANG = "zh_TW"
SUPPORTED_LANGS = (
    "zh_TW", "zh_CN", "en_US", "ja_JP", "ko_KR", "es_ES", "pt_BR",
    "fr_FR", "de_DE", "ru_RU", "it_IT", "vi_VN", "th_TH", "id_ID",
    "tr_TR", "pl_PL", "nl_NL",
)
LANG_NAMES = {
    "zh_TW": "繁體中文", "zh_CN": "简体中文", "en_US": "English",
    "ja_JP": "日本語", "ko_KR": "한국어", "es_ES": "Español",
    "pt_BR": "Português (BR)", "fr_FR": "Français", "de_DE": "Deutsch",
    "ru_RU": "Русский", "it_IT": "Italiano", "vi_VN": "Tiếng Việt",
    "th_TH": "ไทย", "id_ID": "Bahasa Indonesia", "tr_TR": "Türkçe",
    "pl_PL": "Polski", "nl_NL": "Nederlands",
}

def _match_lang(raw):
    """把 Qt／系統回報的語系字串對應到支援清單中的代碼，對不到回傳 None。"""
    if not raw:
        return None
    code = raw.replace("-", "_")
    if code in SUPPORTED_LANGS:
        return code
    parts = [p for p in code.split("_") if p]
    if not parts:
        return None
    lang = parts[0].lower()
    script = next((p.lower() for p in parts[1:] if len(p) == 4), "")
    region = next((p.upper() for p in parts[1:] if len(p) == 2), "")
    if lang == "zh":
        # 繁體（Hant／台港澳）走 zh_TW，其餘視為簡體
        if script == "hant" or region in ("TW", "HK", "MO"):
            return "zh_TW"
        return "zh_CN"
    for supported in SUPPORTED_LANGS:
        if supported.split("_")[0] == lang:
            return supported
    return None


def detect_system_lang(fallback=DEFAULT_LANG):
    """偵測系統語系並回傳支援清單中的代碼。

    優先使用 Qt 的 QLocale（需已安裝 PySide6），再退回 Python 的 locale 模組。
    系統語系不在支援清單內時回傳 en_US，避免非中文使用者看到中文原文；
    完全偵測不到時才回傳 fallback。
    """
    candidates = []
    try:
        from PySide6.QtCore import QLocale
        system_locale = QLocale.system()
        candidates.append(system_locale.name())
        candidates.extend(system_locale.uiLanguages())
    except Exception:
        pass
    try:
        import locale as _locale
        name = _locale.getlocale()[0]
        if name:
            candidates.append(name)
    except Exception:
        pass
    for raw in candidates:
        code = _match_lang(raw)
        if code:
            return code
    return "en_US" if candidates else fallback


_CJK = re.compile(r"[\u3400-\u9fff]")
_CACHE_MAX = 8192


def _find_locale_dir():
    """依序尋找 locale 目錄：原始碼旁 → 執行檔旁 → 打包解壓目錄 → 工作目錄。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, "locale")]
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "locale"))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "locale"))
    candidates.append(os.path.join(os.getcwd(), "locale"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0]


def _fill(template, values):
    """把樣板中的 {} 依序換成 values，避免 str.format 對大括號的額外解讀。"""
    parts = template.split("{}")
    if len(parts) - 1 != len(values):
        return template
    out = [parts[0]]
    for value, tail in zip(values, parts[1:]):
        out.append(value)
        out.append(tail)
    return "".join(out)


class I18n:
    def __init__(self):
        self.locale_dir = _find_locale_dir()
        self.lang = DEFAULT_LANG
        self._table = {}
        self._templates = []
        self._cache = {}
        self._installed = False

    # -- 載入 -------------------------------------------------------------- #
    def load(self, lang):
        """載入語系檔；lang 不在支援清單內時退回預設語言。"""
        self.lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
        self._table = {}
        self._templates = []
        self._cache = {}
        path = os.path.join(self.locale_dir, "{}.json".format(self.lang))
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key, val in data.items():
                    if key.startswith("_") or not isinstance(val, str):
                        continue
                    self._table[key] = val
        except Exception:
            self._table = {}
        self._compile_templates()

    def available(self):
        """回傳 [(語系代碼, 顯示名稱), ...]，只列出 locale 目錄中實際存在的檔案。"""
        out = []
        for code in SUPPORTED_LANGS:
            if os.path.isfile(os.path.join(self.locale_dir, "{}.json".format(code))):
                out.append((code, self.lang_name(code)))
        return out or [(DEFAULT_LANG, self.lang_name(DEFAULT_LANG))]

    def _compile_templates(self):
        compiled = []
        for src, dst in self._table.items():
            if "{}" not in src:
                continue
            parts = src.split("{}")
            pattern = "^" + "(.*?)".join(re.escape(p) for p in parts) + "$"
            try:
                compiled.append((re.compile(pattern, re.S), dst))
            except re.error:
                continue
        # 長樣板優先，避免短樣板先吃掉較長的字串
        compiled.sort(key=lambda item: -len(item[0].pattern))
        self._templates = compiled

    # -- 翻譯 -------------------------------------------------------------- #
    def t(self, s):
        if not isinstance(s, str) or not s:
            return s
        cached = self._cache.get(s)
        if cached is not None:
            return cached
        out = self._table.get(s)
        if out is None:
            out = s
            if self._templates and _CJK.search(s):
                for pattern, dst in self._templates:
                    match = pattern.match(s)
                    if match:
                        out = _fill(dst, match.groups())
                        break
        if len(self._cache) >= _CACHE_MAX:
            self._cache.clear()
        self._cache[s] = out
        return out

    def lang_name(self, lang):
        return LANG_NAMES.get(lang, lang)


i18n = I18n()


# --------------------------------------------------------------------------- #
# 介面文字登記：切換語系時就地重譯既有視窗，不需要重啟
#
# 這裡只登記、不主動查表；翻譯仍由下面 patch 過的 setter 負責。登記的內容是
# 「呼叫當下傳入的原文」，所以重套時只要再走一次同一個 setter，就會拿到新語系
# 的譯文。新增項目類的方法（addTab / addItem / addAction）會先換算成對應的
# setter 再登記，避免重套時又新增一個項目。
# --------------------------------------------------------------------------- #
_registry = weakref.WeakSet()


def _recordable(value):
    """只有非空字串（或含非空字串的序列）才值得登記。"""
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (list, tuple)):
        return any(isinstance(v, str) and v != "" for v in value)
    return False


def _remember(obj, key, source):
    if obj is None or not _recordable(source):
        return
    entries = getattr(obj, "_i18n_entries", None)
    if entries is None:
        entries = {}
        try:
            obj._i18n_entries = entries
        except Exception:
            return
    entries[key] = source
    try:
        _registry.add(obj)
    except TypeError:
        pass  # 少數型別不支援弱引用，放棄登記（仍會在建立時翻譯）


def _record_call(obj, name, args, indexes, result, start_index):
    """把一次 setter／新增項目的呼叫換算成可重套的登記項。"""
    if name in ("addTab", "insertTab"):
        widget = args[0] if name == "addTab" else (args[1] if len(args) > 1 else None)
        try:
            tab_index = obj.indexOf(widget)
        except Exception:
            return
        if tab_index >= 0:
            _remember(obj, ("setTabText", 1, (int(tab_index),)), args[indexes[0]])
        return
    if name in ("addItem", "insertItem"):
        item_index = start_index if name == "addItem" else args[0]
        if item_index is None:
            return
        _remember(obj, ("setItemText", 1, (int(item_index),)), args[indexes[0]])
        return
    if name == "addItems":
        if start_index is None:
            return
        for offset, text in enumerate(args[0] or []):
            _remember(obj, ("setItemText", 1, (int(start_index) + offset,)), text)
        return
    if name == "addAction":
        # 文字掛在回傳的 QAction 上；重套走 setText，不能重呼 addAction（會多一個項目）
        _remember(result, ("setText", 0, ()), args[indexes[0]])
        return
    text_index = indexes[0]
    if text_index >= len(args):
        return
    others = tuple(a for j, a in enumerate(args) if j != text_index)
    if all(isinstance(a, (int, float, str, bool)) for a in others):
        _remember(obj, (name, text_index, others), args[text_index])


def _apply_entry(obj, key, source):
    name, text_index, others = key
    args = list(others)
    args.insert(text_index, source)
    getattr(obj, name)(*args)


def retranslate():
    """以目前載入的語系，重新套用所有登記過的介面文字。"""
    for obj in list(_registry):
        entries = getattr(obj, "_i18n_entries", None)
        if not entries:
            continue
        for key, source in list(entries.items()):
            try:
                _apply_entry(obj, key, source)
            except Exception:
                pass  # 物件可能已被 Qt 銷毀，略過即可


_CTOR_TEXT_SETTERS = {
    "QLabel": "setText",
    "QPushButton": "setText",
    "QCheckBox": "setText",
    "QRadioButton": "setText",
    "QToolButton": "setText",
    "QAction": "setText",
    "QTableWidgetItem": "setText",
    "QListWidgetItem": "setText",
    "QStandardItem": "setText",
    "QGroupBox": "setTitle",
}


def _record_ctor(obj, base_name, args, kwargs):
    setter = _CTOR_TEXT_SETTERS.get(base_name)
    if setter is None:
        return
    if args and isinstance(args[0], str):
        _remember(obj, (setter, 0, ()), args[0])
        return
    keyword = "text" if setter == "setText" else "title"
    if isinstance(kwargs.get(keyword), str):
        _remember(obj, (setter, 0, ()), kwargs[keyword])


# --------------------------------------------------------------------------- #
# Qt 整合：在 Qt 文字 setter / 建構子的邊界統一翻譯
# --------------------------------------------------------------------------- #
def _tr_list(value):
    if isinstance(value, (list, tuple)):
        return type(value)(i18n.t(v) if isinstance(v, str) else v for v in value)
    return value


def _tr_args(args):
    out = []
    for arg in args:
        if isinstance(arg, str):
            out.append(i18n.t(arg))
        elif isinstance(arg, (list, tuple)):
            out.append(_tr_list(arg))
        else:
            out.append(arg)
    return tuple(out)


def _tr_at(args, indexes):
    out = list(args)
    for i in indexes:
        if i < len(out):
            val = out[i]
            if isinstance(val, str):
                out[i] = i18n.t(val)
            elif isinstance(val, (list, tuple)):
                out[i] = _tr_list(val)
    return tuple(out)


def _tr_kwargs(kwargs):
    for key in ("text", "title", "label", "caption"):
        if key in kwargs and isinstance(kwargs[key], str):
            kwargs[key] = i18n.t(kwargs[key])
    return kwargs


def _patch_method(cls, name, indexes):
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_i18n_patched", False):
        return

    def wrapper(self, *args, **kwargs):
        start_index = None
        if name in ("addItem", "addItems"):
            try:
                start_index = self.count()
            except Exception:
                start_index = None
        result = orig(self, *_tr_at(args, indexes), **_tr_kwargs(kwargs))
        try:
            _record_call(self, name, args, indexes, result, start_index)
        except Exception:
            pass
        return result

    wrapper._i18n_patched = True
    try:
        setattr(cls, name, wrapper)
    except Exception:
        pass


def _patch_static(cls, name, indexes):
    orig = getattr(cls, name, None)
    if orig is None or getattr(orig, "_i18n_patched", False):
        return

    def wrapper(*args, **kwargs):
        return orig(*_tr_at(args, indexes), **_tr_kwargs(kwargs))

    wrapper._i18n_patched = True
    try:
        setattr(cls, name, staticmethod(wrapper))
    except Exception:
        pass


_TEXT_CTORS = (
    "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox",
    "QToolButton", "QAction", "QTableWidgetItem", "QTreeWidgetItem",
    "QListWidgetItem", "QStandardItem",
)


def _make_tr_subclass(base):
    """建立一個會自動翻譯文字參數的 Qt 子類別（僅用於覆寫建構子）。"""

    class _TranslatedText(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*_tr_args(args), **_tr_kwargs(kwargs))
            try:
                _record_ctor(self, base.__name__, args, kwargs)
            except Exception:
                pass

    _TranslatedText.__name__ = base.__name__
    _TranslatedText.__qualname__ = getattr(base, "__qualname__", base.__name__)
    _TranslatedText._i18n_generated = True
    return _TranslatedText


def _map_namespace(namespace, W, G):
    """把 namespace 中帶文字的建構子名稱換成會自動翻譯的子類別。"""
    for name in _TEXT_CTORS:
        current = namespace.get(name)
        if current is None or getattr(current, "_i18n_generated", False):
            continue
        base = getattr(W, name, None) or getattr(G, name, None)
        if base is None:
            continue
        try:
            namespace[name] = _make_tr_subclass(base)
        except Exception:
            pass


def install_qt_translation(namespace=None):
    """修補 Qt 文字 setter；若給 namespace（呼叫端模組的 globals），額外把其中
    帶文字的建構子名稱換成自動翻譯子類別。

    Qt 類別的修補只做一次；namespace 的替換則每次呼叫都處理，因為不同模組
    （例如 ui 與測試）各有自己的 globals。
    """
    try:
        from PySide6 import QtGui, QtWidgets
    except Exception:
        return
    W, G = QtWidgets, QtGui

    if i18n._installed:
        if namespace is not None:
            _map_namespace(namespace, W, G)
        return

    i18n._installed = True
    method_targets = (
        (W.QWidget, "setWindowTitle", (0,)),
        (W.QWidget, "setToolTip", (0,)),
        (W.QWidget, "setStatusTip", (0,)),
        (W.QWidget, "setWhatsThis", (0,)),
        (W.QWidget, "addAction", (0,)),
        (W.QLabel, "setText", (0,)),
        (W.QAbstractButton, "setText", (0,)),
        (W.QLineEdit, "setText", (0,)),
        (W.QLineEdit, "setPlaceholderText", (0,)),
        (W.QTextEdit, "setPlaceholderText", (0,)),
        (W.QPlainTextEdit, "setPlaceholderText", (0,)),
        (W.QGroupBox, "setTitle", (0,)),
        (W.QTabWidget, "setTabText", (1,)),
        (W.QTabWidget, "setTabToolTip", (1,)),
        (W.QTabWidget, "addTab", (1,)),
        (W.QTabWidget, "insertTab", (2,)),
        (W.QComboBox, "addItem", (0,)),
        (W.QComboBox, "addItems", (0,)),
        (W.QComboBox, "insertItem", (1,)),
        (W.QComboBox, "setItemText", (1,)),
        (W.QComboBox, "setPlaceholderText", (0,)),
        (W.QTableWidget, "setHorizontalHeaderLabels", (0,)),
        (W.QTableWidget, "setVerticalHeaderLabels", (0,)),
        (W.QTableWidgetItem, "setText", (0,)),
        (W.QTableWidgetItem, "setToolTip", (0,)),
        (W.QTreeWidget, "setHeaderLabels", (0,)),
        (W.QTreeWidgetItem, "setText", (1,)),
        (W.QTreeWidgetItem, "setToolTip", (1,)),
        (W.QListWidgetItem, "setText", (0,)),
        (W.QListWidgetItem, "setToolTip", (0,)),
        (W.QMenu, "addAction", (0,)),
        (W.QMenu, "setTitle", (0,)),
        (W.QSystemTrayIcon, "setToolTip", (0,)),
        (W.QProgressBar, "setFormat", (0,)),
        (W.QAbstractSpinBox, "setSpecialValueText", (0,)),
        (W.QSpinBox, "setSuffix", (0,)),
        (W.QSpinBox, "setPrefix", (0,)),
        (W.QDoubleSpinBox, "setSuffix", (0,)),
        (W.QDoubleSpinBox, "setPrefix", (0,)),
        (G.QAction, "setText", (0,)),
        (G.QAction, "setToolTip", (0,)),
    )
    for cls, name, indexes in method_targets:
        _patch_method(cls, name, indexes)

    static_targets = (
        (W.QMessageBox, "information", (1, 2)),
        (W.QMessageBox, "warning", (1, 2)),
        (W.QMessageBox, "critical", (1, 2)),
        (W.QMessageBox, "question", (1, 2)),
        (W.QMessageBox, "about", (1, 2)),
        (W.QInputDialog, "getText", (1, 2)),
        (W.QInputDialog, "getMultiLineText", (1, 2)),
        (W.QInputDialog, "getItem", (1, 2)),
        (W.QInputDialog, "getInt", (1, 2)),
        (W.QInputDialog, "getDouble", (1, 2)),
        (W.QFileDialog, "getExistingDirectory", (1,)),
        (W.QFileDialog, "getOpenFileName", (1,)),
        (W.QFileDialog, "getSaveFileName", (1,)),
        (W.QFileDialog, "getOpenFileNames", (1,)),
    )
    for cls, name, indexes in static_targets:
        _patch_static(cls, name, indexes)

    if namespace is not None:
        _map_namespace(namespace, W, G)
