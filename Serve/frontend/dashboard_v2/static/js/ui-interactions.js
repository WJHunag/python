const filterLabels = {
  current_polling: "輪詢中監視器",
  waiting_polling: "尚未輪詢監視器",
  fixed: "常駐監視器",
  error: "異常監視器"
};

const sidebar = document.getElementById('sidebar');
const groupFragment = document.createDocumentFragment();
const settingsToggleBtn = document.getElementById('settings-toggle-btn');
const settingsPanelBody = document.getElementById('settings-panel-body');

// 動態建立「取消」按鈕，與完成/設定按鈕相鄰（預設隱藏）
let cancelBtn = document.getElementById('settings-cancel-btn');
(function ensureCancelButton() {
  if (cancelBtn) return;
  const header = document.querySelector('.settings-panel-header');
  if (!header || !settingsToggleBtn) return;
  cancelBtn = document.createElement('button');
  cancelBtn.id = 'settings-cancel-btn';
  cancelBtn.className = 'button gray hidden';
  cancelBtn.textContent = '取消';
  // 插入到完成/設定按鈕的前面，確保顯示在左側
  header.insertBefore(cancelBtn, settingsToggleBtn);
})();

// 初始化篩選器 UI（將在 SSE 管理器完成後調用）
function initializeFiltersUI() {
  // 由當前頁面上的攝影機卡片動態蒐集 IP
  const cameraCards = document.querySelectorAll('.camera-card');
  const ipSet = new Set();

  cameraCards.forEach(card => {
    const ip = card.getAttribute('data-ip') || 'unknown';
    if (ip && ip !== 'unknown' && ip !== '') {
      ipSet.add(ip);
    }
  });

  // 如果沒有找到有效的 IP，則等待一下再重試
  if (ipSet.size === 0) {
    console.log('🔄 等待相機 IP 數據載入...');
    setTimeout(initializeFiltersUI, 500);
    return;
  }

  console.log('🏗️ 建立篩選器 UI，發現 IP:', Array.from(ipSet));
  buildFilterUI(ipSet);
  fillTargetIpSelect();
}

// 建立篩選器 UI 的邏輯
function buildFilterUI(ipSet) {
  // 重新獲取最新的相機卡片數據
  const cameraCards = document.querySelectorAll('.camera-card');
  const groupFragment = document.createDocumentFragment();

  // 動態建立每個 IP 篩選區塊（含 IP 總開關 + 類型開關，可收合）
  ipSet.forEach(ip => {
  const ipGroup = document.createElement('div');
  ipGroup.className = 'ip-group';

  const ipId = `ip-toggle-${ip.replace(/\./g, '-')}`;
  const workstationName = window.getWorkstationDisplayName(ip);
  ipGroup.innerHTML = `<h3 class="ip-label"><input type="checkbox" class="ip-toggle" id="${ipId}" data-ip="${ip}" checked> <label for="${ipId}" title="${ip}">${workstationName}</label></h3>`;

  // 可收合容器
  const filterContainer = document.createElement('div');
  filterContainer.className = 'ip-filter-container';
  filterContainer.setAttribute('data-ip', ip);

  const types = ["current_polling", "waiting_polling", "fixed", "error"];
  types.forEach(type => {
    // 根據要求，註解掉「異常監視器」篩選器的 UI，讓它不顯示
    if (type === 'error') {
      return; // 直接跳過，不建立此項目
    }
    const id = `filter-${ip.replace(/\./g, '-')}-${type}`;
    const item = document.createElement('div');
    item.className = 'filter-item';
    const labelText = filterLabels[type] || type;
    // 計算該 IP + 類型的機台數量
    const count = Array.from(cameraCards).filter(card => {
      const cardIp = card.getAttribute('data-ip');
      const cardType = card.getAttribute('data-type') || 'waiting_polling';
      const isError = (card.getAttribute('data-error') === 'true');
      return cardIp === ip && (
        (type === 'error' && isError) || 
        (type !== 'error' && cardType === type)
      );
    }).length;
    
    item.innerHTML = `
      <input type="checkbox" id="${id}" data-ip="${ip}" data-type="${type}" checked>
      <label for="${id}">${labelText} <span class="count-badge">${count}</span></label>
    `;
    filterContainer.appendChild(item);
  });

  ipGroup.appendChild(filterContainer);
  groupFragment.appendChild(ipGroup);
  });

  sidebar.innerHTML = ''; // 清空舊的篩選器，避免重複
  sidebar.appendChild(groupFragment);
  
  // 在篩選器UI建立完成後，綁定事件監聽器並執行初始同步
  sidebar.removeEventListener('change', siderbarChangeHandler);
  sidebar.addEventListener('change', siderbarChangeHandler);
  
  // 重新綁定相機卡片點擊事件（因為相機卡片可能已重新渲染）
  bindCameraClickEvents();
  
  siderbarChangeHandler();
}

// 更新篩選項目機台數量的函數
function updateFilterCounts() {
  const cameraCards = document.querySelectorAll('.camera-card');
  document.querySelectorAll('.filter-item').forEach(item => {
    const checkbox = item.querySelector('input[type="checkbox"]');
    if (!checkbox) return;
    
    const ip = checkbox.getAttribute('data-ip');
    const type = checkbox.getAttribute('data-type');
    const countBadge = item.querySelector('.count-badge');
    
    if (!countBadge) return;
    
    // 計算該 IP + 類型的機台數量
    const count = Array.from(cameraCards).filter(card => {
      const cardIp = card.getAttribute('data-ip');
      const cardType = card.getAttribute('data-type') || 'waiting_polling';
      const isError = (card.getAttribute('data-error') === 'true');
      return cardIp === ip && (
        (type === 'error' && isError) || 
        (type !== 'error' && cardType === type)
      );
    }).length;
    
    countBadge.textContent = `${count}`;
  });
}

// 填充側欄「工作站 IP」下拉選單
function fillTargetIpSelect() {
  const select = document.getElementById('target-ip-select');
  if (!select) return;
  
  // 動態獲取當前頁面上的IP
  const cameraCards = document.querySelectorAll('.camera-card');
  const ipSet = new Set();
  cameraCards.forEach(card => {
    const ip = card.getAttribute('data-ip') || 'unknown';
    if (ip && ip !== 'unknown' && ip !== '') {
      ipSet.add(ip);
    }
  });
  
  const ips = Array.from(ipSet).sort();
  select.innerHTML = '';
  const opt0 = document.createElement('option');
  opt0.value = '';
  opt0.textContent = '未指定';
  select.appendChild(opt0);
  ips.forEach(ip => {
    const opt = document.createElement('option');
    opt.value = ip;
    opt.textContent = window.getWorkstationDisplayName(ip);
    select.appendChild(opt);
  });
}

// 變動事件（同時考量 IP 總開關與類型開關，並控制收合 + 異常疊加）
siderbarChangeHandler = () => {
  const cameraCards = document.querySelectorAll('.camera-card');
  const enabledIPs = new Set();
  document.querySelectorAll('.ip-toggle:checked').forEach(cb => {
    enabledIPs.add(cb.getAttribute('data-ip'));
  });

  // 展開/收合每個 IP 的類型容器
  document.querySelectorAll('.ip-filter-container').forEach(container => {
    const ip = container.getAttribute('data-ip');
    container.style.display = enabledIPs.has(ip) ? '' : 'none';
  });

  const activePairs = new Set();
  const errorEnabledIPs = new Set();
  document.querySelectorAll('.filter-item input[type="checkbox"]:checked').forEach(cb => {
    const ip = cb.getAttribute('data-ip');
    const type = cb.getAttribute('data-type');
    if (type === 'error') {
      errorEnabledIPs.add(ip);
    } else {
      activePairs.add(`${ip}_${type}`);
    }
  });

  cameraCards.forEach(card => {
    const ip = card.getAttribute('data-ip');
    const type = card.getAttribute('data-type') || 'waiting_polling';
    const isError = (card.getAttribute('data-error') === 'true');
    const pair = `${ip}_${type}`;

    if (!enabledIPs.has(ip)) {
      card.style.display = 'none';
    } else if (isError && errorEnabledIPs.has(ip)) {
      card.style.display = '';
    } else {
      card.style.display = activePairs.has(pair) ? '' : 'none';
    }
  });
};

// 事件監聽器將在 buildFilterUI() 完成後綁定

// ==========================
// 設定模式：選取相機 + 批次設定
// ==========================
// 設定模式切換由側欄標題右側按鈕控制

let isSelectMode = false;

function setSelectMode(enabled, options = {}) {
  const cameraCards = document.querySelectorAll('.camera-card');
  const applyOnClose = options.applyOnClose !== false; // 預設關閉時會套用
  isSelectMode = enabled;
  if (enabled) {
    if (settingsToggleBtn) settingsToggleBtn.textContent = '完成';
    if (settingsPanelBody) settingsPanelBody.classList.remove('hidden');
    if (cancelBtn) cancelBtn.classList.remove('hidden');
    cameraCards.forEach(card => {
      card.classList.add('selectable');
      card.classList.remove('selected');
    });
  } else {
    // 離開設定模式：只要有選中相機就發送API
    if (applyOnClose) {
      const selectedCameras = document.querySelectorAll('.camera-card.selected');
      const checked = document.querySelector('input[name="target-mode"]:checked');
      const mode = checked ? checked.value : 'current_polling'; // 預設為輪詢模式

      console.log('🔍 完成按鈕調試:', {
        selectedCameras: selectedCameras.length,
        mode
      });

      if (selectedCameras.length > 0) {
        console.log('🚀 有選中相機，準備調用 applyTypeToSelected:', mode);
        applyTypeToSelected(mode);
      } else {
        console.warn('⚠️ 沒有選中相機，不會發送API');
      }
    }

    // 清空設定面板的選擇狀態
    resetSettingsPanel();

    if (settingsToggleBtn) settingsToggleBtn.textContent = '設定';
    if (settingsPanelBody) settingsPanelBody.classList.add('hidden');
    if (cancelBtn) cancelBtn.classList.add('hidden');
    cameraCards.forEach(card => {
      card.classList.remove('selectable');
      card.classList.remove('selected');
    });
  }
}

if (settingsToggleBtn) {
  settingsToggleBtn.addEventListener('click', () => setSelectMode(!isSelectMode));
}

if (cancelBtn) {
  cancelBtn.addEventListener('click', () => {
    // 不做任何更動，直接關閉設定模式
    setSelectMode(false, { applyOnClose: false });
  });
}

// 綁定相機卡片點擊事件
function bindCameraClickEvents() {
  console.log('🔧 開始綁定相機點擊事件...');
  const cameraCards = document.querySelectorAll('.camera-card');
  console.log('📹 找到', cameraCards.length, '個相機卡片');
  
  if (cameraCards.length === 0) {
    console.warn('⚠️ 沒有找到相機卡片，可能DOM還未載入');
    return;
  }
  
  cameraCards.forEach(card => {
    // 移除舊的事件監聽器（如果存在）
    card.removeEventListener('click', handleCameraClick);
    // 添加新的事件監聽器
    card.addEventListener('click', handleCameraClick);
  });
}

// 提取點擊處理函數
function handleCameraClick(e) {
  const card = e.currentTarget;
  console.log('🖱️ 相機卡片被點擊！', {
    isSelectMode,
    channel: card.getAttribute('data-channel')
  });

  if (isSelectMode) {
    console.log('⚙️ 設定模式：切換選取狀態');
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();

    card.classList.toggle('selected');
    console.log('📋 選中狀態:', card.classList.contains('selected'));
    return false;
  }

  // 正常模式：跳轉到相機設定頁面
  const channel = card.getAttribute('data-channel');
  const type = card.getAttribute('data-type');
  const ip = card.getAttribute('data-ip');
  const functionSwitches = card.getAttribute('data-fn');

  if (channel) {
    // 準備完整的相機數據
    const cameraData = {
      NVR_channel: parseInt(channel),
      type: type || 'waiting_polling',
      ip: ip || 'unknown',
      function_switches: functionSwitches ? JSON.parse(functionSwitches) : {},
      location: card.querySelector('.camera-location')?.textContent || '',
      image_url: card.querySelector('.camera-image')?.src || null,
      timestamp: Date.now()
    };

    // 存儲到 sessionStorage
    sessionStorage.setItem('selectedCamera', JSON.stringify(cameraData));

    const url = `/static/camera_settings.html`;
    console.log('🔗 正常模式：跳轉到相機設定頁面:', url);
    console.log('📦 已存儲相機數據到 sessionStorage:', cameraData);
    window.location.href = url;
  }
}

// 為每個相機卡片設定按鈕事件
function setupCardButtonEvents() {
  const cameraCards = document.querySelectorAll('.camera-card');
  cameraCards.forEach(card => {
    card.querySelectorAll('.button').forEach(btn => {
      btn.addEventListener('click', (e) => {
        if (!isSelectMode) return;
        e.preventDefault();
        e.stopPropagation();
      });
    });
  });
}

// DOM載入完成後綁定點擊事件
document.addEventListener('DOMContentLoaded', () => {
  bindCameraClickEvents();
});

// UI 更新函數供 app-init.js 調用
function updateCameraCardsUI(newType) {
  const select = document.getElementById('target-ip-select');
  const chosenIp = select ? select.value : '';
  const typeTextMap = {
    current_polling: '輪詢中',
    waiting_polling: '未輪詢',
    fixed: '常駐'
  };
  document.querySelectorAll('.camera-card.selected').forEach(card => {
    // 更新屬性
    card.setAttribute('data-type', newType);
    if (chosenIp) card.setAttribute('data-ip', chosenIp);

    // 同步更新狀態顯示（點在 location 右側的狀態）
    const statusEl = card.querySelector('.camera-status');
    if (statusEl) {
      statusEl.classList.remove('current_polling', 'waiting_polling', 'fixed');
      statusEl.classList.add(newType);
      const indicator = statusEl.querySelector('.status-indicator');
      if (indicator) indicator.textContent = typeTextMap[newType] || newType;
    }
  });
  if (typeof siderbarChangeHandler === 'function') {
    siderbarChangeHandler();
  }
  // 更新篩選項目的機台數量
  updateFilterCounts();
}

/**
 * 重置設定面板的選擇狀態
 */
function resetSettingsPanel() {
  // 清空工作站 IP 選擇
  const targetIpSelect = document.getElementById('target-ip-select');
  if (targetIpSelect) {
    targetIpSelect.value = '';
  }

  // 清空輪詢/常駐單選按鈕
  const modeRadios = document.querySelectorAll('input[name="target-mode"]');
  modeRadios.forEach(radio => {
    radio.checked = false;
  });

  console.log('🧹 已清空設定面板選擇狀態');
}

// 取消即時套用按鈕，改由按「完成」時一次套用
