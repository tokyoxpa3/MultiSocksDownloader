// 固定的本機應用程式伺服器地址（不可修改）
const SERVER_URL = 'http://localhost:8765';

// 擴展程式啟動時初始化
chrome.runtime.onInstalled.addListener(() => {
  // 初始化存儲設置
  chrome.storage.local.get(['enabled', 'cancelOriginalDownload'], (result) => {
    if (result.enabled === undefined) {
      chrome.storage.local.set({ enabled: true });
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

// 建立右鍵選單。先清空再重建，避免舊版遺留的選單 id 造成重複項目。
// 「下載檔案」與「解析影片連結」是兩個獨立動作：
//   - 下載檔案：把連結當一般檔案直接下載（resolve=false）。
//   - 解析影片連結：先把頁面網址交給主程式用 yt-dlp 解析出單檔直連網址（resolve=true）。
function ensureContextMenus() {
  chrome.contextMenus.removeAll(() => {
    const menus = [
      {
        id: 'download-file-direct',
        title: '下載檔案（多代理）',
        contexts: ['link']
      },
      {
        id: 'resolve-video-page',
        title: '解析影片連結並下載（多代理）',
        contexts: ['page', 'video']
      },
      {
        id: 'toggle-site-blacklist',
        title: '將此網站加入下載黑名單（改用瀏覽器原生下載）',
        contexts: ['page']
      }
    ];
    for (const m of menus) {
      chrome.contextMenus.create(m, () => {
        void chrome.runtime.lastError;
      });
    }
  });
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
  if (info.menuItemId === 'download-file-direct') {
    // 「下載檔案」：直接下載右鍵的連結，不解析串流。
    if (info.linkUrl) {
      sendDownloadRequest(info.linkUrl, null, null, '', false);
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
  } else if (info.menuItemId === 'resolve-video-page') {
    // 「解析影片連結」：送頁面網址給主程式解析串流（非影片 src，因為那常是 blob）。
    const pageUrl = info.pageUrl || (tab && tab.url);
    if (pageUrl) {
      sendDownloadRequest(pageUrl, null, null, '', true);
    } else {
      console.error("未獲取到頁面URL");
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'images/icon128.png',
        title: '多代理下載器',
        message: '錯誤：未獲取到頁面網址',
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

// 判斷下載是否應由本機應用攔截（而非瀏覽器原生下載）。
// blob:/data: 是瀏覽器記憶體資料，外部下載器讀不到；停用、黑名單也要放行原生。
function shouldIntercept(downloadItem) {
  let scheme = '';
  try {
    scheme = new URL(downloadItem.url).protocol;
  } catch (e) {
    scheme = '';
  }
  if (scheme === 'blob:' || scheme === 'data:') {
    console.log("blob/data 本地資料，改用瀏覽器原生下載:", downloadItem.url);
    return false;
  }
  if (!enabled) {
    console.log("下載攔截已禁用，跳過:", downloadItem.url);
    return false;
  }
  if (isBlacklisted(downloadItem.url, downloadItem.referrer)) {
    console.log("站點在黑名單內，改用瀏覽器原生下載:", downloadItem.url);
    return false;
  }
  return true;
}

// 監聽下載開始事件：只負責把原始 URL 轉送給本機應用。
// 取消原始下載、阻止另存新檔視窗改由 onDeterminingFilename 同步處理，因為
// onCreated 階段的 cancel 在 service worker 冷啟動（閒置數分鐘被回收後）時，
// 會因 worker 重新載入的延遲而慢於 Chrome 的「確定檔名」階段，導致停頓後
// 第一次下載仍彈出另存視窗。
chrome.downloads.onCreated.addListener(function(downloadItem) {
  console.log("監測到下載開始:", downloadItem);

  if (!shouldIntercept(downloadItem)) {
    return;
  }

  // 直接送原始 URL 給本機應用。重導向、Content-Disposition 檔名、Range 支援偵測
  // 與 Cookie 重放，統一交由應用端的 DownloadTask 用 requests 處理；擴充端不對目標
  // 網址發 fetch/HEAD，以免破壞重導向鏈或拿到不正確的最終連結。
  sendDownloadRequest(downloadItem.url, null, downloadItem.filename, downloadItem.referrer);
});

// 在 Chrome「確定檔名」階段同步攔截。Chrome 會等 suggest() 回應才決定是否彈出
// 另存視窗，因此這是唯一能可靠擋掉視窗的時機（onCreated 的 cancel 在 worker 冷
// 啟動時會慢半拍）。
//
// 注意：suggest() 的參數型別只有 {filename, conflictAction}，沒有 cancel 欄位。
// 先前的 suggest({cancel:true}) 會被 Chrome 忽略、等於空建議，又走回預設流程。
// 正確做法是先 cancel 原始下載，再給一個明確檔名（uniquify）以免另存視窗跳出。
chrome.downloads.onDeterminingFilename.addListener(function(downloadItem, suggest) {
  if (!shouldIntercept(downloadItem)) {
    // 放行：交還瀏覽器原生流程。
    suggest();
    return;
  }

  if (cancelOriginalDownload) {
    console.log("取消原始下載（阻止另存新檔視窗）:", downloadItem.id, downloadItem.url);
    chrome.downloads.cancel(downloadItem.id);
    suggest({ filename: downloadItem.filename || 'download', conflictAction: 'uniquify' });
  } else {
    // 使用者選擇保留瀏覽器原生下載（只轉送、不取消），交還原生流程。
    suggest();
  }
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
// resolveStream=true 表示請主程式先用 yt-dlp 解析出單檔直連網址（解析影片連結）；
// false 表示把網址當一般檔案直接下載。
function sendDownloadRequest(url, downloadId = null, filename = null, referrer = '', resolveStream = false) {
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
  chrome.storage.local.get(['cancelOriginalDownload'], async (result) => {
    const serverUrl = SERVER_URL;

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
          headers: headers, // 瀏覽器 Cookie / Referer / UA，供本機應用重抓檔案
          resolve: resolveStream // true=解析影片連結（yt-dlp），false=當一般檔案直接下載
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

      // 請求已回覆（本機應用已收到），立即解除去重標記，不能保留 60 秒：
      // 使用者可能在「選擇儲存位置」對話框按取消、並未真正建立任務，若仍標記
      // 已處理，短時間內再點同一連結會被誤判為重複而無反應。
      processedUrls.delete(normalizedUrl);
      console.log(`已從處理列表中移除URL: ${normalizedUrl}`);
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
