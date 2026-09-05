// 擴展程式啟動時初始化
chrome.runtime.onInstalled.addListener(() => {
  // 初始化存儲設置
  chrome.storage.local.get(['enabled', 'serverUrl', 'cancelOriginalDownload'], (result) => {
    if (result.enabled === undefined) {
      chrome.storage.local.set({ enabled: true });
    }
    if (result.serverUrl === undefined) {
      chrome.storage.local.set({ serverUrl: 'http://localhost:8765' });
    }
    if (result.cancelOriginalDownload === undefined) {
      chrome.storage.local.set({ cancelOriginalDownload: true });
    }
  });

});

// 建立右鍵選單（冪等建立，reload 不會重複）。
ensureContextMenus();

// 追蹤已處理的 URL，避免短時間內重複發送同一請求
const processedUrls = new Set();

// 由網址取出主機名稱（小寫、去掉 leading www.）；無法解析時回傳 null。
function extractHost(urlString) {
  if (!urlString) {
    return null;
  }
  try {
    return new URL(urlString).hostname.toLowerCase().replace(/^www\./, '');
  } catch (e) {
    return null;
  }
}

// 判斷網址或其來源頁是否命中黑名單。
function isBlacklisted(urlString, referrer) {
  const hosts = [extractHost(urlString), extractHost(referrer)].filter(Boolean);
  return hosts.some((h) => blacklist.includes(h));
}

// 切換某主機名稱的黑名單狀態，並提示結果。
function toggleBlacklist(host) {
  chrome.storage.local.get(['blacklist'], (result) => {
    const list = Array.isArray(result.blacklist) ? result.blacklist.slice() : [];
    const idx = list.indexOf(host);
    let added = false;
    if (idx >= 0) {
      list.splice(idx, 1);
    } else {
      list.push(host);
      added = true;
    }
    chrome.storage.local.set({ blacklist: list }, () => {
      blacklist = list;
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'images/icon128.png',
        title: '多代理下載器',
        message: added
          ? `已將 ${host} 加入黑名單，此網站改用瀏覽器原生下載`
          : `已將 ${host} 移出黑名單，恢復攔截`,
        priority: 2
      });
    });
  });
}

// 建立（冪等）右鍵選單：id 已存在時忽略 duplicate 錯誤，reload 不會重複建立。
function ensureContextMenus() {
  const menus = [
    {
      id: 'download-with-multisocks',
      title: '使用多代理下載器下載',
      contexts: ['link']
    },
    {
      id: 'toggle-site-blacklist',
      title: '將此網站加入下載黑名單（改用瀏覽器原生下載）',
      contexts: ['page']
    }
  ];
  for (const m of menus) {
    chrome.contextMenus.create(m, () => {
      // 已存在時會回傳 duplicate id 錯誤，忽略即可。
      void chrome.runtime.lastError;
    });
  }
}

// 快取「取消原始下載」與「啟用攔截」設定。onDeterminingFilename 必須同步呼叫
// suggest()，onCreated 也需同步判斷是否攔截，不能在此做非同步 storage 查詢，
// 否則存檔視窗會先彈出（見 onCreated / onDeterminingFilename）。
let cancelOriginalDownload = true;
let enabled = true;
let blacklist = [];

chrome.storage.local.get(['cancelOriginalDownload', 'enabled', 'blacklist'], (result) => {
  if (result.cancelOriginalDownload !== undefined) {
    cancelOriginalDownload = result.cancelOriginalDownload;
  }
  if (result.enabled !== undefined) {
    enabled = result.enabled;
  }
  if (Array.isArray(result.blacklist)) {
    blacklist = result.blacklist;
  }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local') {
    if (changes.cancelOriginalDownload) {
      cancelOriginalDownload = changes.cancelOriginalDownload.newValue;
    }
    if (changes.enabled) {
      enabled = changes.enabled.newValue;
    }
    if (changes.blacklist) {
      blacklist = Array.isArray(changes.blacklist.newValue)
        ? changes.blacklist.newValue
        : [];
    }
  }
});

// 處理右鍵選單點擊事件
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'download-with-multisocks') {
    if (info.linkUrl) {
      sendDownloadRequest(info.linkUrl);
    } else {
      console.error("未獲取到連結URL");
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'images/icon128.png',
        title: '多代理下載器',
        message: '錯誤：未獲取到連結',
        priority: 2
      });
    }
  } else if (info.menuItemId === 'toggle-site-blacklist') {
    const host = extractHost(info.pageUrl);
    if (!host) {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'images/icon128.png',
        title: '多代理下載器',
        message: '無法取得此網站的主機名稱',
        priority: 2
      });
      return;
    }
    toggleBlacklist(host);
  }
});

// 監聽下載開始事件
chrome.downloads.onCreated.addListener(function(downloadItem) {
  console.log("監測到下載開始:", downloadItem);

  // 同步檢查攔截是否被禁用。
  if (!enabled) {
    console.log("下載攔截已禁用，跳過:", downloadItem.url);
    return;
  }

  // 站點在黑名單內：跳過攔截，讓瀏覽器用原生下載（帶自己的 cookie / JS 會話）。
  if (isBlacklisted(downloadItem.url, downloadItem.referrer)) {
    console.log("站點在黑名單內，改用瀏覽器原生下載:", downloadItem.url);
    return;
  }

  if (cancelOriginalDownload) {
    console.log("立即取消原始下載（阻止另存新檔視窗）:", downloadItem.id, downloadItem.url);

    // 在 onCreated 階段就取消，Chrome 不會進入「確定檔名 → 彈出另存新檔視窗」的
    // 階段。這是阻止視窗跳出的關鍵：onDeterminingFilename 的 suggest({cancel:true})
    // 雖能取消下載，但 MV3 下若 worker 回應過慢，Chrome 可能已先彈出視窗
    // （先前 log 顯示下載已 USER_CANCELED 但視窗仍跳出）。
    chrome.downloads.cancel(downloadItem.id);
  }

  // 直接送原始 URL 給本機應用。重導向、Content-Disposition 檔名、Range 支援偵測
  // 與 Cookie 重放，統一交由應用端的 DownloadTask 用 requests 處理；擴充端不對目標
  // 網址發 fetch/HEAD，以免破壞重導向鏈或拿到不正確的最終連結。
  sendDownloadRequest(downloadItem.url, null, downloadItem.filename, downloadItem.referrer);
});

// 監聽下載狀態變化
chrome.downloads.onChanged.addListener(function(downloadDelta) {
  if (downloadDelta.state) {
    console.log(`下載 ID ${downloadDelta.id} 狀態變更為: ${downloadDelta.state.current}`);

    // 如果下載完成，可以在這裡執行額外操作
    if (downloadDelta.state.current === 'complete') {
      console.log(`下載 ID ${downloadDelta.id} 已完成`);
    }

    // 如果下載失敗，可以在這裡處理錯誤
    if (downloadDelta.state.current === 'interrupted') {
      console.log(`下載 ID ${downloadDelta.id} 已中斷，原因: ${downloadDelta.error?.current || '未知'}`);
    }
  }
});

// 轉送時使用的瀏覽器 UA，覆蓋下載器預設的 bot UA，避免被以 UA 特徵攔下。
const BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

// 讀取指定網址在瀏覽器 cookie jar 中的 cookie，序列化成 Cookie 表頭字串。
// 回傳 Promise<string>；無 cookie 或讀取失敗時回傳空字串。
function getCookieHeader(url) {
  return new Promise((resolve) => {
    if (!chrome.cookies) {
      resolve('');
      return;
    }
    try {
      chrome.cookies.getAll({ url: url }, (cookies) => {
        if (chrome.runtime.lastError || !cookies || cookies.length === 0) {
          resolve('');
          return;
        }
        resolve(cookies.map((c) => `${c.name}=${c.value}`).join('; '));
      });
    } catch (e) {
      resolve('');
    }
  });
}

// 發送下載請求到本地應用
function sendDownloadRequest(url, downloadId = null, filename = null, referrer = '') {
  // 確保URL有效
  if (!url) {
    console.error("嘗試下載無效URL");
    return;
  }

  // 規範化URL以便更好地進行去重比較
  let normalizedUrl = url;
  try {
    normalizedUrl = new URL(url).toString();
  } catch (e) {
    console.error("無效的URL格式:", url);
  }

  // 檢查URL是否已被處理過，防止重複發送
  if (processedUrls.has(normalizedUrl)) {
    console.log("此URL已處理過，跳過:", normalizedUrl);
    return;
  }

  // 標記URL為已處理
  processedUrls.add(normalizedUrl);
  console.log("發送下載請求:", normalizedUrl, "檔案名:", filename);

  // 顯示通知，表示開始下載
  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'images/icon128.png',
    title: '多代理下載器',
    message: '已開始下載處理',
    priority: 2
  });

  // 檢查是否需要取消原始下載
  chrome.storage.local.get(['cancelOriginalDownload', 'serverUrl'], async (result) => {
    const serverUrl = result.serverUrl || 'http://localhost:8765';

    // 讀取瀏覽器 cookie，連同 Referer / 真實 UA 一併轉送，讓本機應用能以
    // 「已通過驗證」的身份重抓檔案（部分檔案站綁 cookie，缺了就回驗證頁）。
    const cookieHeader = await getCookieHeader(normalizedUrl);
    const headers = { 'User-Agent': BROWSER_UA };
    if (cookieHeader) {
      headers['Cookie'] = cookieHeader;
    }
    if (referrer) {
      headers['Referer'] = referrer;
    }

    // 先檢查伺服器連接
    fetch(`${serverUrl}/ping`, {
      method: 'GET',
      headers: {
        'Cache-Control': 'no-cache' // 避免緩存
      }
    })
    .then(response => {
      if (!response.ok) {
        throw new Error(`伺服器連接失敗: ${response.status}`);
      }
      return response.json();
    })
    .then(pingData => {
      console.log('伺服器連接成功:', pingData);

      // 發送下載請求
      return fetch(`${serverUrl}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Cache-Control': 'no-cache' // 避免緩存
        },
        body: JSON.stringify({
          url: normalizedUrl, // 使用規範化URL
          downloadId: downloadId,
          filename: filename,
          timestamp: Date.now(), // 添加時間戳避免重複
          headers: headers // 瀏覽器 Cookie / Referer / UA，供本機應用重抓檔案
        })
      });
    })
    .then(response => {
      if (!response.ok) {
        throw new Error(`下載請求失敗: ${response.status}`);
      }
      return response.json();
    })
    .then(data => {
      console.log('下載請求已發送:', data);

      // 如果設置為取消原始下載且有下載 ID
      if (result.cancelOriginalDownload && downloadId !== null) {
        chrome.downloads.cancel(downloadId, function() {
          console.log(`已取消原始下載 ID: ${downloadId}`);
        });
      }

      // 下載成功，保持URL在已處理列表中一段時間後再移除
      setTimeout(() => {
        processedUrls.delete(normalizedUrl);
        console.log(`已從處理列表中移除URL: ${normalizedUrl}`);
      }, 60000); // 1分鐘後清理
    })
    .catch(error => {
      // 如果發送失敗，立即從已處理列表中移除，允許重試
      processedUrls.delete(normalizedUrl);
      console.error('發送下載請求時出錯:', error);

      // 顯示錯誤通知
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'images/icon128.png',
        title: '多代理下載器',
        message: `下載請求失敗: ${error.message}`,
        priority: 2
      });
    });
  });
}
