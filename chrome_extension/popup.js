// 當彈出視窗載入時
document.addEventListener('DOMContentLoaded', () => {
  const enableToggle = document.getElementById('enableToggle');
  const cancelOriginalToggle = document.getElementById('cancelOriginalToggle');
  const serverUrlInput = document.getElementById('serverUrl');
  const connectionStatus = document.getElementById('connectionStatus');
  const testConnectionBtn = document.getElementById('testConnection');
  const saveSettingsBtn = document.getElementById('saveSettings');
  const tasksList = document.getElementById('tasksList');

  // 載入儲存的設置
  chrome.storage.local.get(['enabled', 'serverUrl', 'cancelOriginalDownload'], (result) => {
    enableToggle.checked = result.enabled !== undefined ? result.enabled : true;
    cancelOriginalToggle.checked = result.cancelOriginalDownload !== undefined ? result.cancelOriginalDownload : true;
    serverUrlInput.value = result.serverUrl || 'http://localhost:8765';

    // 初始檢查連接狀態
    checkConnection(serverUrlInput.value);

    // 開始輪詢下載任務
    startTasksPolling();

    // 載入並渲染黑名單
    loadBlacklist();
  });

  // 黑名單變更時即時重繪（例如在頁面右鍵加入／移出）。
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && changes.blacklist) {
      renderBlacklist(Array.isArray(changes.blacklist.newValue) ? changes.blacklist.newValue : []);
    }
  });

  // 切換啟用/禁用狀態
  enableToggle.addEventListener('change', () => {
    chrome.storage.local.set({ enabled: enableToggle.checked });
    updateStatus(`下載攔截已${enableToggle.checked ? '啟用' : '禁用'}`);
  });

  // 切換取消原始下載狀態
  cancelOriginalToggle.addEventListener('change', () => {
    chrome.storage.local.set({ cancelOriginalDownload: cancelOriginalToggle.checked });
    updateStatus(`取消 Chrome 原始下載已${cancelOriginalToggle.checked ? '啟用' : '禁用'}`);
  });

  // 測試連接按鈕
  testConnectionBtn.addEventListener('click', () => {
    const serverUrl = serverUrlInput.value.trim();
    if (!serverUrl) {
      updateStatus('請輸入有效的伺服器地址', 'error');
      return;
    }

    checkConnection(serverUrl);
  });

  // 儲存設置按鈕
  saveSettingsBtn.addEventListener('click', () => {
    const serverUrl = serverUrlInput.value.trim();
    if (!serverUrl) {
      updateStatus('請輸入有效的伺服器地址', 'error');
      return;
    }

    chrome.storage.local.set({ 
      serverUrl: serverUrl,
      enabled: enableToggle.checked,
      cancelOriginalDownload: cancelOriginalToggle.checked
    }, () => {
      updateStatus('設置已儲存');
    });
  });

  // 檢查與應用程式的連接
  function checkConnection(serverUrl) {
    updateStatus('正在檢查連接...', 'checking');
    checkHttpConnection(serverUrl);
  }

  // 通過 HTTP 檢查連接
  function checkHttpConnection(serverUrl) {
    fetch(`${serverUrl}/ping`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json'
      }
    })
    .then(response => {
      if (!response.ok) {
        throw new Error('HTTP 請求失敗');
      }
      return response.json();
    })
    .then(data => {
      updateStatus('已通過 HTTP 連接到應用程式', 'success');
    })
    .catch(error => {
      updateStatus('無法連接到多代理下載器應用程式', 'error');
      console.error('連接檢查失敗:', error);
    });
  }

  // 更新狀態顯示
  function updateStatus(message, type = 'info') {
    connectionStatus.textContent = message;

    // 根據狀態類型設置樣式
    connectionStatus.style.backgroundColor = {
      'info': '#f0f0f0',
      'success': '#d4edda',
      'error': '#f8d7da',
      'checking': '#fff3cd'
    }[type] || '#f0f0f0';

    connectionStatus.style.color = {
      'info': '#000',
      'success': '#155724',
      'error': '#721c24',
      'checking': '#856404'
    }[type] || '#000';
  }

  // 將任務狀態轉成可讀文字
  function statusToText(status) {
    const map = {
      'pending': '等待中',
      'downloading': '下載中',
      'completed': '已完成',
      'failed': '失敗',
      'paused': '已暫停',
      'cancelled': '已取消',
      'error': '錯誤'
    };
    return map[status] || status || '未知';
  }

  // 輪詢 GET /tasks 並渲染進行中的下載
  function startTasksPolling() {
    const poll = () => {
      const serverUrl = serverUrlInput.value.trim();
      if (!serverUrl) {
        tasksList.textContent = '沒有進行中的任務';
        return;
      }

      fetch(`${serverUrl}/tasks`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          'Cache-Control': 'no-cache'
        }
      })
      .then(response => {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        return response.json();
      })
      .then(data => {
        renderTasks(data.tasks || []);
      })
      .catch(error => {
        console.error('查詢任務失敗:', error);
        tasksList.textContent = '無法取得任務狀態';
      });
    };

    poll();
    return setInterval(poll, 2000);
  }

  function renderTasks(tasks) {
    if (!tasks || tasks.length === 0) {
      tasksList.textContent = '沒有進行中的任務';
      return;
    }

    tasksList.innerHTML = '';
    tasks.forEach(task => {
      const item = document.createElement('div');
      item.className = 'task-item';

      const title = document.createElement('div');
      title.className = 'task-title';
      title.textContent = task.filename || '(未命名檔案)';

      const meta = document.createElement('div');
      meta.className = 'task-meta';
      const status = statusToText(task.status);
      const pct = (typeof task.percentage === 'number') ? task.percentage.toFixed(1) + '%' : '--%';
      const speed = task.speed ? `${formatSpeed(task.speed)}` : '--';
      meta.textContent = `狀態：${status}　進度：${pct}　速度：${speed}`;

      item.appendChild(title);
      item.appendChild(meta);
      tasksList.appendChild(item);
    });
  }

  // 格式化速度顯示
  function formatSpeed(speed) {
    const s = Number(speed);
    if (!isFinite(s) || s <= 0) return '--';
    if (s >= 1024 * 1024) return (s / (1024 * 1024)).toFixed(1) + ' MB/s';
    if (s >= 1024) return (s / 1024).toFixed(1) + ' KB/s';
    return s.toFixed(1) + ' B/s';
  }

  // 載入並渲染黑名單
  function loadBlacklist() {
    chrome.storage.local.get(['blacklist'], (result) => {
      renderBlacklist(Array.isArray(result.blacklist) ? result.blacklist : []);
    });
  }

  // 渲染黑名單：每個站點一個「移除」按鈕
  function renderBlacklist(list) {
    const el = document.getElementById('blacklistList');
    if (!el) return;
    el.innerHTML = '';
    if (list.length === 0) {
      el.textContent = '（空）';
      return;
    }
    list.forEach((host) => {
      const row = document.createElement('div');
      row.className = 'blacklist-item';

      const name = document.createElement('span');
      name.className = 'blacklist-host';
      name.textContent = host;

      const btn = document.createElement('button');
      btn.className = 'blacklist-remove';
      btn.textContent = '移除';
      btn.addEventListener('click', () => {
        chrome.storage.local.get(['blacklist'], (r) => {
          const updated = (Array.isArray(r.blacklist) ? r.blacklist : [])
            .filter((h) => h !== host);
          chrome.storage.local.set({ blacklist: updated }, () => {
            renderBlacklist(updated);
          });
        });
      });

      row.appendChild(name);
      row.appendChild(btn);
      el.appendChild(row);
    });
  }
}); 