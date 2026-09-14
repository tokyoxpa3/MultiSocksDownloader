"""i18n 語系檔一致性測試。

確保每個 locale/*.json：
- 是合法 JSON 物件且有 _info
- key 集合與 locale/_source_strings.json（來源字串清單）一致
- 每個 value 的 {} 數量與對應 key 相同、且非空
未列在表中的字串會回退顯示原文，故 zh_TW 允許空表。
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import i18n  # noqa: E402


def _read_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


class TestI18nCore(unittest.TestCase):
    def test_supported_langs(self):
        self.assertEqual(i18n.DEFAULT_LANG, "zh_TW")
        self.assertEqual(len(i18n.SUPPORTED_LANGS), 17)
        for code in ("en_US", "zh_CN", "ja_JP", "ko_KR", "de_DE", "nl_NL"):
            self.assertIn(code, i18n.SUPPORTED_LANGS)

    def test_fallback_returns_source(self):
        inst = i18n.I18n()
        inst.load("zh_TW")
        self.assertEqual(inst.t("下載中"), "下載中")
        self.assertEqual(inst.t("__not_in_table__"), "__not_in_table__")

    def test_non_string_passthrough(self):
        self.assertIsNone(i18n.i18n.t(None))
        self.assertEqual(i18n.i18n.t(123), 123)

    def test_template_fill(self):
        inst = i18n.I18n()
        inst._table = {"已加入下載: {}": "Added: {}"}
        inst._cache = {}
        inst._compile_templates()
        self.assertEqual(inst.t("已加入下載: a.mp4"), "Added: a.mp4")

    def test_lang_name(self):
        self.assertEqual(i18n.i18n.lang_name("en_US"), "English")
        self.assertEqual(i18n.i18n.lang_name("zh_TW"), "繁體中文")


class TestLocaleFiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.locale_dir = os.path.join(ROOT, "locale")
        cls.source = _read_json(os.path.join(cls.locale_dir, "_source_strings.json"))

    def test_source_list(self):
        self.assertGreater(len(self.source), 100)
        self.assertEqual(len(set(self.source)), len(self.source), "duplicate source strings")

    def test_all_locales_present_and_consistent(self):
        src = set(self.source)
        for code in i18n.SUPPORTED_LANGS:
            path = os.path.join(self.locale_dir, code + ".json")
            with self.subTest(locale=code):
                self.assertTrue(os.path.isfile(path), "%s.json missing" % code)
                data = _read_json(path)
                self.assertIn("_info", data, "%s missing _info" % code)
                body = {k: v for k, v in data.items() if not k.startswith("_")}
                if code == "zh_TW":
                    continue  # 預設語言以原文回退，允許空表
                self.assertEqual(set(body), src, "%s key set differs" % code)
                for key, val in body.items():
                    self.assertIsInstance(val, str, "%s non-str value for %r" % (code, key))
                    self.assertTrue(val.strip(), "%s empty value for %r" % (code, key))
                    self.assertEqual(
                        val.count("{}"), key.count("{}"),
                        "%s placeholder mismatch for %r" % (code, key))


if __name__ == "__main__":
    unittest.main()
