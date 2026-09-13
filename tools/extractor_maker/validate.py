"""生成後驗證：用全新 subprocess 跑 yt-dlp / resolver 腳本。

extractor 用 `python -m yt_dlp --dump-json <url>` 驗證：subprocess 是全新
程序，會依 PYTHONPATH 重新載入 yt_dlp_plugins，因此新寫入的 plugin 能被
發現，繞開「plugin 只在 import 時載入一次」的限制。
"""

import ast
import builtins
import json
import os
import subprocess
import sys


def check_undefined_names(source):
    """用 AST 找出「被使用但從未被 import/定義」的名字。

    subprocess 驗證只跑得到 happy path，像 `ExtractorError` 忘了 import 這種錯
    只在特定分支才炸，會讓驗證給出假信心。此檢查在落檔後、跑 yt-dlp 前先掃
    一遍，可攔下這類執行期 NameError。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return False, '語法錯誤：%s' % exc

    bound = set()
    loaded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
            else:
                bound.add(node.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)

    builtin_names = set(dir(builtins))
    special = {'__name__', '__file__', '__doc__', '__package__', '__spec__',
               '__builtins__', '__loader__', '__cached__'}
    undefined = sorted(loaded - bound - builtin_names - special)
    if undefined:
        return False, '使用了未 import/未定義的名稱：' + ', '.join(undefined)
    return True, 'ok'


def repo_root():
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))


def validate_extractor(url, timeout=90):
    """用 subprocess 跑 yt-dlp --dump-json 驗證新 plugin。回傳 (ok, msg)。"""
    root = repo_root()
    env = dict(os.environ)
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    cmd = [sys.executable, '-m', 'yt_dlp', '--dump-json',
           '--no-warnings', '--no-playlist', url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=env, cwd=root, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, '驗證逾時（%ds）' % timeout
    if proc.returncode != 0:
        return False, proc.stderr.strip()[-3000:]
    out = proc.stdout.strip()
    if not out:
        return False, 'yt-dlp 無輸出'
    last = out.splitlines()[-1]
    try:
        info = json.loads(last)
    except json.JSONDecodeError:
        return False, 'yt-dlp 輸出非 JSON：' + last[:500]
    if info.get('url') or info.get('formats'):
        summary = {'id': info.get('id'), 'title': info.get('title'),
                   'ext': info.get('ext')}
        return True, json.dumps(summary, ensure_ascii=False)
    return False, 'yt-dlp 解析成功但缺 url/formats：' + last[:500]


def validate_resolver(script_path, url, timeout=120):
    """執行生成的 resolver 腳本，檢查最後一行是否為直連 URL。回傳 (ok, msg)。"""
    try:
        proc = subprocess.run([sys.executable, script_path, url],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, 'resolver 執行逾時（%ds）' % timeout
    if proc.returncode != 0:
        return False, proc.stderr.strip()[-2000:]
    out = proc.stdout.strip()
    last = out.splitlines()[-1] if out else ''
    if last.startswith('http'):
        return True, last
    return False, 'resolver 未輸出直連 URL：' + (out[-500:] or '(無輸出)')
