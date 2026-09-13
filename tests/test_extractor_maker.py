"""extractor_maker 命名規則與 digest 猜 id 的單元測試。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.extractor_maker import generators, digest, validate


class TestModuleNaming(unittest.TestCase):
    def test_module_to_class(self):
        self.assertEqual(generators.module_name_to_class('mysite'), 'Mysite')
        self.assertEqual(generators.module_name_to_class('my_site'), 'MySite')
        self.assertEqual(generators.module_name_to_class('my-site'), 'MySite')
        self.assertEqual(generators.module_name_to_class('my.site'), 'MySite')

    def test_extractor_class_name(self):
        self.assertEqual(generators.extractor_class_name('mysite'), 'MysiteIE')
        self.assertEqual(generators.extractor_class_name('my_site'), 'MySiteIE')

    def test_sanitize_module_name(self):
        self.assertEqual(generators.sanitize_module_name('My Site!'), 'my_site')
        self.assertEqual(generators.sanitize_module_name('123abc'), 's_123abc')
        self.assertEqual(generators.sanitize_module_name(''), 'unknown')
        self.assertEqual(generators.sanitize_module_name(None), 'unknown')

    def test_render_extractor_file_has_import(self):
        rendered = generators.render_extractor_file(
            'class DemoIE(InfoExtractor):\n    pass\n')
        self.assertTrue(
            rendered.startswith('from yt_dlp.extractor.common import InfoExtractor'))

    def test_render_extractor_file_imports_extractor_error(self):
        # LLM 常直接寫 raise ExtractorError(...) 卻忘了 import，落檔時必須補上。
        rendered = generators.render_extractor_file(
            'class DemoIE(InfoExtractor):\n'
            '    def _real_extract(self, url):\n'
            '        raise ExtractorError("boom")\n')
        self.assertIn('ExtractorError', rendered.splitlines()[0])

    def test_render_extractor_file_imports_used_utils(self):
        # 用到 yt_dlp.utils 的 helper（int_or_none 等）時自動補 import。
        rendered = generators.render_extractor_file(
            'class DemoIE(InfoExtractor):\n'
            '    def _real_extract(self, url):\n'
            '        return int_or_none(self._match_id(url))\n')
        self.assertIn('int_or_none', rendered)

    def test_check_undefined_names_catches_missing_import(self):
        # 直接用未 import 的名稱（模擬 LLM 原始碼）應被攔下。
        ok, msg = validate.check_undefined_names(
            'class DemoIE(InfoExtractor):\n'
            '    def _real_extract(self, url):\n'
            '        raise ExtractorError("boom")\n')
        self.assertFalse(ok)
        self.assertIn('ExtractorError', msg)


class TestGuessVideoId(unittest.TestCase):
    def test_path_segment(self):
        self.assertEqual(
            digest.guess_video_id('https://x.com/watch/8842'), '8842')

    def test_query_param(self):
        self.assertEqual(
            digest.guess_video_id('https://x.com/p?v=abc'), 'abc')

    def test_empty_when_no_id(self):
        self.assertEqual(
            digest.guess_video_id('https://x.com/'), '')


if __name__ == '__main__':
    unittest.main()
