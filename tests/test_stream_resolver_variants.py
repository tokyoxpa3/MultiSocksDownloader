"""stream_resolver 來源/集數變體解析的單元測試。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_resolver import _extract_variants, FormatListResult


class TestExtractVariants(unittest.TestCase):
    def test_parses_source_episode_ids(self):
        fmts = [
            {'format_id': 's1e01', 'url': 'https://x/a.m3u8'},
            {'format_id': 's1e02', 'url': 'https://x/b.m3u8'},
            {'format_id': 's2e1', 'url': 'https://x/c.m3u8'},
        ]
        v = _extract_variants(fmts)
        self.assertEqual(len(v), 3)
        self.assertEqual(v[0]['source'], '1')
        self.assertEqual(v[0]['episode'], '01')
        self.assertEqual(v[0]['format_id'], 's1e01')
        self.assertEqual(v[2]['source'], '2')
        self.assertEqual(v[2]['episode'], '1')

    def test_ignores_non_variant_ids(self):
        fmts = [{'format_id': '137'}, {'format_id': '720p'}, {'format_id': None}]
        self.assertEqual(_extract_variants(fmts), [])

    def test_format_list_result_defaults_variants_empty(self):
        self.assertEqual(FormatListResult(ok=True).variants, [])

    def test_source_episode_with_suffix(self):
        v = _extract_variants([{'format_id': 's12e003'}])
        self.assertEqual(v[0]['source'], '12')
        self.assertEqual(v[0]['episode'], '003')


if __name__ == '__main__':
    unittest.main()
