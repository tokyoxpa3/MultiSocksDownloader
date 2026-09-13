"""extractor_maker CLI：Playwright 攔截 → digest → LLM 生成 → 落檔 → 驗證。

用法（從 repo 根目錄）：
    python -m tools.extractor_maker "https://example.com/video/123"

設定 LLM（三選一）：
    - 編輯 tools/extractor_maker/config.json（可先複製 config.example.json）
    - 設環境變數 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
    - 用 --base-url / --api-key / --model 參數
"""

import argparse
import json
import logging
import os
import sys

from . import capture, config, digest, generators, llm, prompts, validate

logger = logging.getLogger('extractor_maker')


def _repo_root():
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))


def _default_out_dir():
    return os.path.join(_repo_root(), 'yt_dlp_plugins', 'extractor')


def _default_resolver_dir():
    # resolver 是獨立腳本，絕不能放進 yt_dlp_plugins/extractor/——yt-dlp 會把
    # 該目錄下每個 .py 當成 extractor 外掛 import，頂層 sys.argv[1] 會炸掉。
    return os.path.join(_repo_root(), 'resolvers')


def _retry_with_feedback(cfg, digest_text, error, prev_result):
    """把驗證錯誤回饋給 LLM，要求它修正後重新輸出同格式 JSON。"""
    feedback = (
        '上一次生成的程式碼驗證失敗，錯誤訊息：\n'
        + error[:2000]
        + '\n請修正 code（必要時也修正 site_name / valid_url），'
          '重新輸出同格式 JSON。'
    )
    try:
        raw = llm.chat(
            cfg['base_url'], cfg['api_key'], cfg['model'],
            [
                {'role': 'system', 'content': prompts.SYSTEM_PROMPT},
                {'role': 'user', 'content': digest_text},
                {'role': 'assistant', 'content': json.dumps(prev_result, ensure_ascii=False)},
                {'role': 'user', 'content': feedback},
            ],
        )
        return llm.parse_json_block(raw)
    except Exception as exc:
        logger.warning('修正重試失敗：%s', exc)
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='從陌生網站影片 URL 自動生成 yt-dlp extractor 或 resolver')
    parser.add_argument('url', help='影片頁 URL')
    parser.add_argument('--headed', action='store_true',
                        help='用有頭瀏覽器（供需登入/人機驗證的站）')
    parser.add_argument('--user-data-dir', default=None,
                        help='沿用既有 Chromium profile（配合 --headed 先登入一次）')
    parser.add_argument('--base-url', default=None)
    parser.add_argument('--api-key', default=None)
    parser.add_argument('--model', default=None)
    parser.add_argument('--out-dir', default=None,
                        help='輸出目錄（extractor 預設 yt_dlp_plugins/extractor；'
                             'resolver 預設 resolvers/）')
    parser.add_argument('--max-retries', type=int, default=3,
                        help='LLM 生成/驗證失敗的重試次數（預設 3）')
    parser.add_argument('--no-llm', action='store_true',
                        help='不呼叫 LLM，僅輸出 digest（除錯用）')
    parser.add_argument('--no-validate', action='store_true',
                        help='生成後不跑驗證')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    # 0. 設定
    cfg = config.load_config()
    for key, val in (('base_url', args.base_url), ('api_key', args.api_key),
                     ('model', args.model)):
        if val:
            cfg[key] = val

    # 1. capture
    print('[1/4] Playwright 攔截：%s' % args.url)
    try:
        final_url, media_reqs, all_reqs = capture.capture(
            args.url, headless=not args.headed,
            user_data_dir=args.user_data_dir)
    except RuntimeError as exc:
        print('錯誤：%s' % exc)
        return 2
    if not media_reqs:
        print('     警告：未攔截到明顯媒體請求，改用全部請求（可能解不出直連）。')
    dig = digest.build_digest(media_reqs or all_reqs, final_url)
    print('     攔截到 %d 個請求，媒體/API 相關 %d 個。'
          % (len(all_reqs), len(media_reqs)))

    if args.no_llm:
        print('\n[digest]')
        print(digest.digest_to_text(dig))
        return 0

    if not cfg.get('base_url') or not cfg.get('api_key'):
        print('錯誤：未設定 LLM（需 base_url + api_key）。')
        print('     可用 LLM_BASE_URL / LLM_API_KEY 環境變數，或用 --base-url / --api-key，')
        print('     或複製 config.example.json 為 config.json 後填入。')
        return 2

    digest_text = digest.digest_to_text(dig)

    # 2. LLM 生成（可重試）
    result = None
    print('[2/4] LLM 分析（model=%s）...' % cfg['model'])
    for attempt in range(1, args.max_retries + 1):
        try:
            raw = llm.chat(cfg['base_url'], cfg['api_key'], cfg['model'],
                           prompts.build_messages(digest_text))
            result = llm.parse_json_block(raw)
            break
        except Exception as exc:
            print('      LLM 第 %d 次失敗：%s' % (attempt, exc))
    if result is None:
        print('LLM 分析失敗，中止。')
        return 3

    mode = result.get('mode')
    code = result.get('code') or ''
    if mode not in ('extractor', 'resolver') or not code:
        print('錯誤：LLM 回覆缺 mode 或 code。')
        return 3

    # 3. 落檔（site_name 固定後，重試時只更新 code，避免產生孤兒檔）
    site_name = generators.sanitize_module_name(result.get('site_name') or '')
    if args.out_dir:
        out_dir = args.out_dir
    elif mode == 'extractor':
        out_dir = _default_out_dir()
    else:
        out_dir = _default_resolver_dir()
    if mode == 'resolver':
        # 防呆：resolver 是獨立腳本，不可寫進 yt-dlp 外掛命名空間，否則
        # import 時頂層程式碼就會執行並炸掉整個解析。
        plugin_dir = os.path.abspath(_default_out_dir())
        if os.path.abspath(out_dir) == plugin_dir or \
                os.path.abspath(out_dir).startswith(plugin_dir + os.sep):
            print('錯誤：resolver 不可寫入 yt_dlp_plugins/extractor/（yt-dlp 會 import 該目錄所有 .py）。')
            return 3
    if mode == 'extractor':
        module_name = site_name
        path = generators.write_module(
            out_dir, module_name, generators.render_extractor_file(code))
    else:
        module_name = site_name + '_resolver'
        path = generators.write_module(
            out_dir, module_name, generators.render_resolver_file(code))
    print('[3/4] 落檔（mode=%s, site=%s）→ %s' % (mode, site_name, path))

    # 4. 驗證
    if args.no_validate:
        print('[4/4] 已跳過驗證。')
        return 0

    print('[4/4] 驗證生成結果...')
    if mode == 'extractor':
        for attempt in range(1, args.max_retries + 1):
            with open(path, 'r', encoding='utf-8') as f:
                source = f.read()
            ok, msg = validate.check_undefined_names(source)
            if ok:
                ok, msg = validate.validate_extractor(args.url)
            if ok:
                print('      驗證成功：%s' % msg)
                print('      生成檔：%s' % path)
                return 0
            print('      驗證失敗（第 %d 次）：%s' % (attempt, msg[:400]))
            result = _retry_with_feedback(cfg, digest_text, msg, result)
            if result is None:
                break
            code = result.get('code') or ''
            if not code:
                break
            path = generators.write_module(
                out_dir, module_name, generators.render_extractor_file(code))
            print('      已依錯誤回饋重寫：%s' % path)
        print('驗證多次失敗，已保留最後生成檔供人工檢查：%s' % path)
        return 1

    ok, msg = validate.validate_resolver(path, args.url)
    if ok:
        print('      直連 URL：%s' % msg)
    else:
        print('      驗證失敗：%s' % msg[:400])
    print('      生成檔：%s' % path)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
