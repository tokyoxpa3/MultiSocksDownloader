"""生成 InfoExtractor / resolver 程式碼與落檔，處理檔名→類別名規則。

yt-dlp 的 plugin 命名規則：檔案 `mysite.py` → 類別名 `MysiteIE`
（檔名駝峰化 + 'IE' 結尾），兩者不一致時 plugin 載入會失敗。
"""

import ast
import os
import re

_EXTRACTOR_BASE_IMPORT = (
    'from yt_dlp.extractor.common import InfoExtractor, ExtractorError')

# 這兩個由 render_extractor_file 無條件匯入，動態補匯入時不再重複。
_ALWAYS_IMPORTED = frozenset({'InfoExtractor', 'ExtractorError'})


def module_name_to_class(module_name):
    """'mysite' -> 'Mysite'；'my_site' -> 'MySite'；'my-site' -> 'MySite'。"""
    parts = re.split(r'[^a-zA-Z0-9]+', module_name)
    return ''.join(p.capitalize() for p in parts if p)


def extractor_class_name(site_name):
    """'mysite' -> 'MysiteIE'。"""
    return module_name_to_class(site_name) + 'IE'


def sanitize_module_name(site_name):
    """把任意 site_name 收斂成合法 Python 模組名（小寫、a-z0-9_）。"""
    name = re.sub(r'[^a-zA-Z0-9]+', '_', site_name or '').strip('_').lower()
    if not name:
        name = 'unknown'
    if name[0].isdigit():
        name = 's_' + name
    return name


def _used_names(code):
    """用 AST 取出程式碼引用到的裸名稱（排除屬性存取與字串內文字）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def _ytdlp_util_helpers():
    """yt-dlp.utils 內可匯入的公用 helper 名稱（函式/類別）。

    LLM 生成的程式碼常直接引用這些工具（int_or_none、traverse_obj 等）卻忘了
    import，落檔時依實際用到的名字自動補上，避免執行期才 NameError。
    """
    try:
        from yt_dlp import utils
    except ImportError:
        return frozenset()
    return frozenset(
        name for name in dir(utils)
        if not name.startswith('_')
        and (isinstance(getattr(utils, name), type)
             or callable(getattr(utils, name)))
    )


def render_extractor_file(code):
    """把 LLM 產生的類別本體（不含 import）包成完整 plugin 檔。

    保證基礎 import（InfoExtractor、ExtractorError）一定存在，並依程式碼實際
    用到的名字補上 yt_dlp.utils 的匯入——LLM 常漏寫這些 import，而驗證只跑得到
    happy path，這種錯會在特定分支才炸。
    """
    used = _used_names(code)
    helper_names = sorted(used & (_ytdlp_util_helpers() - _ALWAYS_IMPORTED))
    lines = [_EXTRACTOR_BASE_IMPORT]
    if helper_names:
        lines.append('from yt_dlp.utils import ' + ', '.join(helper_names))
    return '\n'.join(lines) + '\n\n\n' + code.rstrip() + '\n'


def render_resolver_file(code):
    """resolver 是獨立腳本，直接落檔。"""
    return code.rstrip() + '\n'


def write_module(out_dir, module_name, content):
    """把內容寫成 <out_dir>/<module_name>.py，回傳絕對路徑。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, module_name + '.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path
