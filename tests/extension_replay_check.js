#!/usr/bin/env node
/**
 * 擴充端「瀏覽器啟動時重播歷史下載」防護的驗證腳本。
 *
 * 用 Node 的 vm 模組載入 chrome_extension/background.js，並注入假的 chrome API，
 * 直接餵各種 onCreated 事件，檢查到底有沒有把請求轉送出去。
 *
 * 執行：node tests/extension_replay_check.js
 * 離開碼 0 表示全部通過。
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const BACKGROUND_JS = path.join(__dirname, '..', 'chrome_extension', 'background.js');
const SERVER_PORT = 8765;

function makeEnv() {
  const listeners = { onCreated: [], onDeterminingFilename: [], onChanged: [] };
  const localStore = {};
  const sessionStore = {};
  const posted = [];        // 送到本機應用的 POST
  const notifications = [];
  const cancelled = [];

  function storageArea(store) {
    return {
      get(keys, cb) {
        const result = {};
        if (Array.isArray(keys)) {
          keys.forEach((k) => { if (k in store) result[k] = store[k]; });
        } else if (typeof keys === 'string') {
          if (keys in store) result[keys] = store[keys];
        } else if (keys && typeof keys === 'object') {
          Object.keys(keys).forEach((k) => {
            result[k] = (k in store) ? store[k] : keys[k];
          });
        }
        if (cb) setTimeout(() => cb(result), 0);
      },
      set(obj, cb) {
        Object.assign(store, obj);
        if (cb) setTimeout(cb, 0);
      },
    };
  }

  const chrome = {
    runtime: { onInstalled: { addListener() {} }, lastError: undefined },
    storage: {
      local: storageArea(localStore),
      session: storageArea(sessionStore),
      onChanged: { addListener(fn) { listeners.onChanged.push(fn); } },
    },
    contextMenus: {
      removeAll(cb) { if (cb) cb(); },
      create(_m, cb) { if (cb) cb(); },
      onClicked: { addListener() {} },
    },
    notifications: { create(o) { notifications.push(o); } },
    downloads: {
      onCreated: { addListener(fn) { listeners.onCreated.push(fn); } },
      onDeterminingFilename: { addListener(fn) { listeners.onDeterminingFilename.push(fn); } },
      onChanged: { addListener(fn) { listeners.onChanged.push(fn); } },
      cancel(id, cb) { cancelled.push(id); if (cb) cb(); },
    },
    cookies: { getAll(_q, cb) { cb([]); } },
  };

  const fetchCalls = [];
  const sandbox = {
    chrome,
    console: { log() {}, error() {}, warn() {} },
    setTimeout,
    clearTimeout,
    Date,
    URL,
    Set,
    Map,
    JSON,
    Promise,
    Number,
    Object,
    Array,
    String,
    isFinite,
    fetch(url, opts) {
      const method = (opts && opts.method) || 'GET';
      fetchCalls.push({ url, method, opts });
      if (method === 'POST' && url.includes(`:${SERVER_PORT}`)) {
        posted.push({ url, body: JSON.parse(opts.body) });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'ok' }),
      });
    },
  };
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BACKGROUND_JS, 'utf8'), sandbox, { filename: 'background.js' });

  return { sandbox, listeners, posted, notifications, cancelled, sessionStore };
}

const tick = (ms) => new Promise((r) => setTimeout(r, ms || 30));

function isoAgo(ms) {
  return new Date(Date.now() - ms).toISOString();
}

function makeItem(overrides) {
  return Object.assign({
    id: 1,
    url: 'https://example.com/file.bin',
    filename: 'file.bin',
    referrer: '',
    startTime: new Date().toISOString(),
  }, overrides);
}

const results = [];

function check(name, condition, detail) {
  results.push({ name, pass: !!condition, detail: detail || '' });
  const mark = condition ? 'PASS' : 'FAIL';
  console.log(`[${mark}] ${name}${detail && !condition ? ' -> ' + detail : ''}`);
}

async function main() {
  const env = makeEnv();
  const onCreated = env.listeners.onCreated[0];
  const onDetermining = env.listeners.onDeterminingFilename[0];

  check('onCreated 已註冊', typeof onCreated === 'function');
  check('onDeterminingFilename 已註冊', typeof onDetermining === 'function');

  // 1) 瀏覽器啟動時重播的歷史下載（startTime 是 2 小時前）不應被轉送
  onCreated(makeItem({ id: 101, startTime: isoAgo(2 * 60 * 60 * 1000) }));
  await tick();
  check('歷史重播不轉送', env.posted.length === 0,
        `posted=${env.posted.length}`);
  check('歷史重播不跳通知', env.notifications.length === 0,
        `notifications=${env.notifications.length}`);

  // 2) 真正的新下載（startTime 幾乎等於現在）應該被轉送
  onCreated(makeItem({ id: 102, startTime: new Date().toISOString() }));
  await tick();
  check('新下載會被轉送', env.posted.length === 1, `posted=${env.posted.length}`);
  check('轉送的 URL 正確',
        env.posted[0] && env.posted[0].body.url === 'https://example.com/file.bin',
        JSON.stringify(env.posted[0] && env.posted[0].body));

  // 3) 同一個 download id 再次觸發（worker 被回收後重播同一事件）不應重複轉送
  onCreated(makeItem({ id: 102, startTime: new Date().toISOString() }));
  await tick();
  check('同一下載 id 不重複轉送', env.posted.length === 1, `posted=${env.posted.length}`);

  // 4) 之後再下載同一個 URL：新的一次下載會有新的 id，必須允許
  onCreated(makeItem({ id: 103, startTime: new Date().toISOString() }));
  await tick();
  check('同一 URL 之後仍可重新下載', env.posted.length === 2, `posted=${env.posted.length}`);

  // 5) 邊界：剛剛（1 秒前）才開始的下載，仍在容許誤差內，應被視為新下載
  onCreated(makeItem({ id: 104, startTime: isoAgo(1000) }));
  await tick();
  check('1 秒前開始的下載仍算新下載', env.posted.length === 3, `posted=${env.posted.length}`);

  // 6) 歷史重播不應在確定檔名階段被取消（否則會靜默中斷使用者的續傳）
  let suggestArgs = null;
  onDetermining(makeItem({ id: 201, startTime: isoAgo(3 * 60 * 60 * 1000) }),
                (opts) => { suggestArgs = opts; });
  await tick(5);
  check('歷史重播不取消原始下載', env.cancelled.length === 0,
        `cancelled=${JSON.stringify(env.cancelled)}`);
  check('歷史重播在檔名階段直接放行', suggestArgs === undefined,
        JSON.stringify(suggestArgs));

  // 7) 新下載在確定檔名階段仍要被攔截（取消原始下載並指定檔名）
  let suggestArgs2 = null;
  onDetermining(makeItem({ id: 105, startTime: new Date().toISOString() }),
                (opts) => { suggestArgs2 = opts; });
  await tick(5);
  check('新下載在檔名階段被取消', env.cancelled.includes(105),
        `cancelled=${JSON.stringify(env.cancelled)}`);
  check('新下載在檔名階段指定檔名',
        !!suggestArgs2 && suggestArgs2.conflictAction === 'uniquify',
        JSON.stringify(suggestArgs2));

  // 8) 黑名單網站仍走原生下載（確認沒有把 shouldIntercept 弄壞）
  env.listeners.onChanged.forEach((fn) => fn({ blacklist: { newValue: ['example.com'] } }, 'local'));
  const before = env.posted.length;
  onCreated(makeItem({ id: 106, startTime: new Date().toISOString() }));
  await tick();
  check('黑名單網站不被轉送', env.posted.length === before, `posted=${env.posted.length}`);

  const failed = results.filter((r) => !r.pass);
  console.log('');
  console.log(`${results.length - failed.length}/${results.length} 項通過`);
  if (failed.length) {
    console.log('失敗項目：');
    failed.forEach((f) => console.log(`  - ${f.name} (${f.detail})`));
    process.exit(1);
  }
  console.log('擴充端重播防護驗證全部通過');
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
