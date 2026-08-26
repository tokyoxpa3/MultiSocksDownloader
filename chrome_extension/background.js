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

  // 創建右鍵選單
  chrome.contextMenus.create({
    id: 'download-with-multisocks',
    title: '使用多代理下載器下載',
    contexts: ['link']
  });
});

// 追蹤已處理的下載，避免重複處理
const processedDownloads = new Set();
const pendingDownloads = new Map(); // 用於追蹤等待確定文件名的下載
const processedUrls = new Set(); // 追蹤已處理的URL，避免重複發送
const pendingRedirects = new Set(); // 追蹤正在獲取重定向的URL

// 快取「取消原始下載」與「啟用攔截」設定。onDeterminingFilename 必須同步呼叫
// suggest()，onCreated 也需同步判斷是否攔截，不能在此做非同步 storage 查詢，
// 否則存檔視窗會先彈出（見 onCreated / onDeterminingFilename）。
let cancelOriginalDownload = true;
let enabled = true;

chrome.storage.local.get(['cancelOriginalDownload', 'enabled'], (result) => {
  if (result.cancelOriginalDownload !== undefined) {
    cancelOriginalDownload = result.cancelOriginalDownload;
  }
  if (result.enabled !== undefined) {
    enabled = result.enabled;
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
  }
});

// 處理右鍵選單點擊事件
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'download-with-multisocks') {
    console.log("右鍵選單點擊，獲取到的連結:", info.linkUrl);

    // 確保連結有效
    if (info.linkUrl) {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'images/icon128.png',
        title: '多代理下載器',
        message: '正在獲取最終下載連結...',
        priority: 2
      });

      // 標記此URL正在處理中，防止重複請求
      pendingRedirects.add(info.linkUrl);

      // 先獲取重定向後的URL，再添加下載任務
      getRedirectedUrl(info.linkUrl).then(finalUrl => {
        console.log("獲取到最終下載連結:", finalUrl);

        // 只發送重定向後的最終URL
        sendDownloadRequest(finalUrl);

        // 移除待處理標記
        pendingRedirects.delete(info.linkUrl);
      }).catch(error => {
        console.error("獲取最終URL失敗:", error);

        // 出錯時移除待處理標記
        pendingRedirects.delete(info.linkUrl);

        // 顯示錯誤通知
        chrome.notifications.create({
          type: 'basic',
          iconUrl: 'images/icon128.png',
          title: '多代理下載器',
          message: '獲取最終連結失敗，請重試',
          priority: 2
        });
      });
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
  }
});

// 獲取重定向後的URL (返回Promise)
function getRedirectedUrl(initialUrl) {
  console.log("獲取重定向URL:", initialUrl);

  return new Promise((resolve, reject) => {
    // 嘗試使用fetch獲取重定向URL
    fetch(initialUrl, {
      method: 'HEAD',
      redirect: 'follow',
      cache: 'no-store'
    })
    .then(response => {
      if (response.url && response.url !== initialUrl) {
        console.log("使用fetch獲取到重定向URL:", response.url);
        resolve(response.url);
      } else {
        // 沒有檢測到重定向，嘗試使用標籤頁方法
        console.log("未檢測到重定向，使用標籤頁方法");
        fetchWithTab(initialUrl).then(resolve).catch(reject);
      }
    })
    .catch(error => {
      console.error("使用fetch獲取重定向URL失敗:", error);
      // 嘗試使用標籤頁方法作為備用
      fetchWithTab(initialUrl).then(resolve).catch(reject);
    });
  });
}

// 使用chrome標籤頁獲取最終URL (返回Promise)
function fetchWithTab(initialUrl) {
  console.log("使用標籤頁方法獲取最終URL");

  return new Promise((resolve, reject) => {
    // 創建一個隱藏的標籤頁
    chrome.tabs.create({ url: initialUrl, active: false }, (tab) => {
      console.log("已創建臨時標籤頁，ID:", tab.id);

      // 設置一個超時，避免無限等待
      const timeoutId = setTimeout(() => {
        console.log("標籤頁加載超時");
        chrome.tabs.remove(tab.id);
        reject(new Error("獲取最終URL超時"));
      }, 15000); // 15秒超時

      // 等待標籤頁加載完成
      chrome.tabs.onUpdated.addListener(function listener(tabId, changeInfo, updatedTab) {
        if (tabId === tab.id && changeInfo.status === 'complete') {
          // 移除監聽器，避免重複處理
          chrome.tabs.onUpdated.removeListener(listener);
          // 取消超時計時器
          clearTimeout(timeoutId);

          // 獲取當前標籤的URL（這是重定向後的最終URL）
          console.log("標籤頁加載完成，最終URL:", updatedTab.url);
          const finalUrl = updatedTab.url;

          // 關閉臨時標籤頁
          chrome.tabs.remove(tab.id, () => {
            console.log("已關閉臨時標籤頁");
            if (finalUrl && finalUrl !== "chrome://newtab/") {
              resolve(finalUrl);
            } else {
              reject(new Error("無法獲取有效的最終URL"));
            }
          });
        }
      });
    });
  });
}

// 監聽下載開始事件
chrome.downloads.onCreated.addListener(function(downloadItem) {
  console.log("監測到下載開始:", downloadItem);

  // 同步檢查攔截是否被禁用。
  if (!enabled) {
    console.log("下載攔截已禁用，跳過:", downloadItem.url);
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

  // 自己解析重定向，取得最終 URL 與檔名後再送給下載器。
  resolveDownloadUrl(downloadItem.url).then(info => {
    sendDownloadRequest(info.url, null, info.filename);
  }).catch(error => {
    console.error("解析最終 URL 失敗，改用原始 URL:", error);
    sendDownloadRequest(downloadItem.url, null, downloadItem.filename);
  });
});

// 透過 HEAD 解析重定向後的最終 URL，並嘗試從 Content-Disposition 取得檔名。
function resolveDownloadUrl(initialUrl) {
  console.log("解析最終下載 URL（HEAD）:", initialUrl);

  return fetch(initialUrl, {
    method: 'HEAD',
    redirect: 'follow',
    cache: 'no-store'
  }).then(response => {
    const finalUrl = response.url || initialUrl;
    const filename = extractFilename(response.headers.get('content-disposition'), finalUrl);
    console.log("解析到最終 URL:", finalUrl, "檔名:", filename);
    return { url: finalUrl, filename: filename };
  });
}

// 從 Content-Disposition 或 URL 解析檔名
function extractFilename(contentDisposition, url) {
  if (contentDisposition) {
    const starMatch = contentDisposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
    if (starMatch) {
      return decodeURIComponent(starMatch[1].replace(/^"|"$/g, '').trim());
    }
    const plainMatch = contentDisposition.match(/filename\s*=\s*"?([^";]+)"?/i);
    if (plainMatch) {
      return plainMatch[1].trim();
    }
  }
  try {
    const base = new URL(url).pathname.split('/').filter(Boolean).pop();
    if (base && base.includes('.')) return decodeURIComponent(base);
  } catch (e) {}
  return null;
}

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

// 發送下載請求到本地應用
function sendDownloadRequest(url, downloadId = null, filename = null) {
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
  chrome.storage.local.get(['cancelOriginalDownload', 'serverUrl'], (result) => {
    const serverUrl = result.serverUrl || 'http://localhost:8765';

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
          timestamp: Date.now() // 添加時間戳避免重複
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