/**
 * SSE 事件管理器
 * 負責處理 Server-Sent Events 連線、事件監聽和 UI 更新
 */
class SSEManager {
  constructor() {
    this.currentETag = this.loadETagFromStorage();
    this.config = null;
    this.eventSource = null;
    this.isInitialized = false;
    this.pollingInterval = null;
    this.broadcastChannel = null;
    
    // 事件處理器綁定
    this.eventHandlers = {
      'snapshot': this.handleSnapshot.bind(this),
      'connected': this.handleConnected.bind(this),
      'config.updated': this.handleConfigUpdate.bind(this),
      'status.updated': this.handleStatusUpdate.bind(this),
      'image.updated': this.handleImageUpdate.bind(this)
    };
  }

  // ==================== 主要流程 ====================

  /**
   * 初始化 SSE 連線
   */
  init() {
    console.log('🔗 啟動 SSE 管理器...');
    
    // 從全域變數中讀取初始 ETag
    if (typeof window.bootETag === 'string' && window.bootETag) {
      this.currentETag = window.bootETag;
      console.log('📋 載入初始 ETag:', this.currentETag);
    }
    
    // 檢查是否有來自設定頁面的更新標記
    this.checkForUpdateFlag();
    
    // 設置頁面可見性變化監聽器
    this.setupVisibilityListener();
    
    this.setupSSEConnection();
    this.setupCrossPageCommunication();
  }

  /**
   * 設置頁面可見性監聽器
   */
  setupVisibilityListener() {
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        this.checkForUpdateFlag();
      }
    });
    
    // 也監聽 focus 事件（當用戶返回此標籤頁時）
    window.addEventListener('focus', () => {
      this.checkForUpdateFlag();
    });
  }

  /**
   * 檢查設定頁面的更新標記
   */
  checkForUpdateFlag() {
    const updateFlag = sessionStorage.getItem('dashboard_needs_update');
    console.log('📋 sessionStorage 中的更新標記:', updateFlag);
    
    if (updateFlag) {
      try {
        const updateData = JSON.parse(updateFlag);
        console.log('🔍 發現設定頁面的更新標記:', updateData);
        
        // 清除標記（避免重複處理）
        sessionStorage.removeItem('dashboard_needs_update');
        
        // 如果有新的 ETag，直接觸發更新
        if (updateData.etag) {
          console.log('📦 設定頁面已更新配置，主動同步數據');
          console.log('📋 舊 ETag:', this.currentETag);
          console.log('📋 新 ETag:', updateData.etag);
          
          // 直接調用同步更新（移除 ETag 比較，強制更新）
          this.syncUpdate(updateData.etag);
        } else {
          console.warn('⚠️ 更新標記中沒有 ETag');
        }
      } catch (error) {
        console.warn('⚠️ 解析更新標記失敗:', error);
        sessionStorage.removeItem('dashboard_needs_update');
      }
    }
  }

  /**
   * 建立 SSE 連線
   */
  setupSSEConnection() {
    const sseUrl = this.buildSSEUrl();
    console.log(`🔗 連接到: ${sseUrl}`);

    this.eventSource = new EventSource(sseUrl);
    
    // 註冊事件監聽器
    this.registerEventListeners();
    
    // 註冊連線狀態監聽器
    this.registerConnectionListeners();
  }

  /**
   * 建構 SSE URL
   */
  buildSSEUrl() {
    let url = '/ui/events';
    if (this.currentETag) {
      url += `?etag=${encodeURIComponent(this.currentETag)}`;
    }
    return url;
  }

  /**
   * 設置跨頁面通信
   */
  setupCrossPageCommunication() {
    console.log('🔧 開始設置跨頁面通信...');
    
    // 優先使用 BroadcastChannel
    if (typeof BroadcastChannel !== 'undefined') {
      try {
        this.broadcastChannel = new BroadcastChannel('dashboard_updates');
        this.broadcastChannel.addEventListener('message', (event) => {
          console.log('📨 主頁面收到 BroadcastChannel 消息:', event.data);
          this.handleCrossPageMessage(event.data);
        });
        
        // 添加錯誤處理
        this.broadcastChannel.addEventListener('error', (error) => {
          console.error('❌ BroadcastChannel 錯誤:', error);
        });
        
        console.log('✅ BroadcastChannel 跨頁面通信設置完成');
        
        // 測試接收器是否工作
        console.log('🧪 BroadcastChannel 接收器已就緒，等待消息...');
      } catch (error) {
        console.error('❌ BroadcastChannel 設置失敗:', error);
      }
    } else {
      // 降級使用 localStorage 事件
      window.addEventListener('storage', (event) => {
        console.log('📨 主頁面收到 localStorage 事件:', event);
        if (event.key === 'dashboard_update_trigger' && event.newValue) {
          try {
            const data = JSON.parse(event.newValue);
            console.log('📨 解析的 localStorage 數據:', data);
            this.handleCrossPageMessage(data);
          } catch (error) {
            console.warn('⚠️ 解析跨頁面消息失敗:', error);
          }
        }
      });
      console.log('📡 已設置 localStorage 跨頁面通信（降級模式）');
    }
  }

  /**
   * 處理跨頁面消息
   */
  handleCrossPageMessage(data) {
    console.log('📨 收到跨頁面消息:', data);
    console.log('🔍 消息類型:', data.type);
    console.log('🔍 消息來源:', data.source);
    console.log('🔍 新 ETag:', data.etag);
    console.log('🔍 當前 ETag:', this.currentETag);
    
    if (data.type === 'config_updated' && data.etag) {
      console.log('🔄 收到配置更新通知，主動獲取最新數據');
      console.log('🔄 調用 syncUpdate，ETag:', data.etag);
      
      // 主動調用API獲取最新數據
      this.syncUpdate(data.etag);
    } else {
      console.warn('⚠️ 跨頁面消息格式不正確或缺少 ETag');
    }
  }

  // ==================== 事件監聽器註冊 ====================

  /**
   * 註冊 SSE 事件監聽器
   */
  registerEventListeners() {
    Object.entries(this.eventHandlers).forEach(([eventType, handler]) => {
      this.eventSource.addEventListener(eventType, (event) => {
        const data = JSON.parse(event.data);
        console.log(`📨 收到 ${eventType} 事件:`, data);
        handler(data);
      });
    });
  }

  /**
   * 註冊連線狀態監聽器
   */
  registerConnectionListeners() {
    this.eventSource.onopen = () => {
      console.log('✅ SSE 連線建立成功');
      this.stopPollingMode();
    };

    this.eventSource.onerror = (error) => {
      console.warn('❌ SSE 連線錯誤:', error);
      this.handleConnectionError();
    };
  }

  // ==================== 事件處理器 ====================

  /**
   * 處理 snapshot 事件（初次連線推送）
   */
  async handleSnapshot(data) {
    if (!this.isInitialized) {
      console.log('🎨 首次初始化 UI...');
      await this.performInitialSetup(data.etag);
    } else {
      console.log('🔄 同步更新 UI...');
      await this.syncUpdate(data.etag);
    }
  }

  /**
   * 處理 connected 事件（數據已同步）
   */
  handleConnected(data) {
    console.log('✅ 連線已建立，數據同步完成');
    if (!this.isInitialized) {
      this.tryRestoreFromCache();
    }
  }

  /**
   * 處理配置更新事件
   */
  async handleConfigUpdate(data) {
    console.log('⚙️ 配置已更新');
    // 配置更新時需要完整同步
    await this.syncUpdate(data.etag);
  }

  /**
   * 處理狀態更新事件
   */
  async handleStatusUpdate(data) {
    console.log('📊 狀態已更新', data);

    // 如果有ETag且相同，使用增量更新
    if (data.etag && data.etag === this.currentETag && data.camera) {
      this.updateCameraStatusIncremental(data);
    } else {
      // ETag不同時進行完整同步
      await this.syncUpdate(data.etag);
    }
  }

  /**
   * 處理圖片更新事件
   */
  async handleImageUpdate(data) {
    console.log(`🖼️ 相機 ${data.camera} 圖片已更新`, data);

    // 如果有ETag且相同，使用增量更新
    if (data.etag && data.etag === this.currentETag && data.camera) {
      this.updateCameraImageIncremental(data);
    } else {
      // ETag不同時進行完整同步
      await this.syncUpdate(data.etag);
    }
  }

  // ==================== 增量更新方法 ====================

  /**
   * 增量更新相機狀態
   */
  updateCameraStatusIncremental(data) {
    console.log('🔄 增量更新相機狀態:', data);

    if (!data.camera || !data.status) {
      console.warn('⚠️ 狀態更新數據不完整:', data);
      return;
    }

    // 查找對應的相機卡片
    const cameraCard = document.querySelector(`[data-channel="${data.camera}"]`);
    if (!cameraCard) {
      console.warn(`⚠️ 找不到相機 ${data.camera} 的卡片`);
      return;
    }

    // 更新狀態屬性
    const oldType = cameraCard.getAttribute('data-type');
    cameraCard.setAttribute('data-type', data.status);

    // 更新IP（如果提供）
    if (data.ip) {
      cameraCard.setAttribute('data-ip', data.ip);
    }

    // 更新視覺狀態
    const statusEl = cameraCard.querySelector('.camera-status');
    if (statusEl) {
      // 移除舊的狀態類別
      statusEl.classList.remove('current_polling', 'waiting_polling', 'fixed');
      // 添加新的狀態類別
      statusEl.classList.add(data.status);

      // 更新狀態文字
      const indicator = statusEl.querySelector('.status-indicator');
      if (indicator) {
        const statusText = {
          'current_polling': '輪詢中',
          'waiting_polling': '未輪詢',
          'fixed': '常駐'
        };
        indicator.textContent = statusText[data.status] || data.status;
      }
    }

    console.log(`✅ 相機 ${data.camera} 狀態已更新: ${oldType} → ${data.status}`);

    // 觸發篩選器更新
    if (typeof siderbarChangeHandler === 'function') {
      siderbarChangeHandler();
    }

    // 更新篩選項目計數
    if (typeof updateFilterCounts === 'function') {
      updateFilterCounts();
    }
  }

  /**
   * 增量更新相機圖片
   */
  updateCameraImageIncremental(data) {
    console.log('🖼️ 增量更新相機圖片:', data);

    if (!data.camera) {
      console.warn('⚠️ 圖片更新數據不完整:', data);
      return;
    }

    // 查找對應的相機卡片
    const cameraCard = document.querySelector(`[data-channel="${data.camera}"]`);
    if (!cameraCard) {
      console.warn(`⚠️ 找不到相機 ${data.camera} 的卡片`);
      return;
    }

    // 更新圖片
    const imageWrapper = cameraCard.querySelector('.image-wrapper');
    if (!imageWrapper) {
      console.warn(`⚠️ 找不到相機 ${data.camera} 的圖片容器`);
      return;
    }

    if (data.image_url) {
      // 有新圖片：更新或創建 img 元素
      let img = imageWrapper.querySelector('.camera-image');
      if (img) {
        // 更新現有圖片的時間戳避免快取
        const url = new URL(data.image_url, window.location.origin);
        url.searchParams.set('t', Date.now());
        img.src = url.toString();
      } else {
        // 創建新的圖片元素（移除異常狀態）
        const noImageDiv = imageWrapper.querySelector('.camera-no-image');
        if (noImageDiv) {
          noImageDiv.remove();
        }

        img = document.createElement('img');
        img.src = data.image_url;
        img.alt = `Camera ${data.camera}`;
        img.className = 'camera-image';
        imageWrapper.appendChild(img);
      }

      console.log(`✅ 相機 ${data.camera} 圖片已更新`);
    } else {
      // 無圖片：顯示空白區域（不是異常，只是沒有圖片更新）
      const img = imageWrapper.querySelector('.camera-image');
      if (img) {
        img.remove();
      }

      if (!imageWrapper.querySelector('.camera-no-image')) {
        const noImageDiv = document.createElement('div');
        noImageDiv.className = 'camera-no-image';
        imageWrapper.appendChild(noImageDiv);
      }

      console.log(`✅ 相機 ${data.camera} 圖片清除（無新圖片）`);
    }
  }

  // ==================== UI 更新邏輯 ====================

  /**
   * 執行首次初始化
   */
  async performInitialSetup(serverETag) {
    try {
      const hasUpdate = await this.fetchConfiguration(serverETag);
      
      if (hasUpdate && this.config) {
        this.renderUserInterface();
        this.isInitialized = true;
        this.saveETagToStorage();
        console.log('✅ UI 初始化完成');
      }
    } catch (error) {
      console.error('❌ 初始化失敗:', error);
      this.startPollingMode();
    }
  }

  /**
   * 同步更新 UI
   */
  async syncUpdate(serverETag) {
    if (this.shouldUpdate(serverETag)) {
      console.log('🔄 ETag 不同，更新 UI');
      const hasUpdate = await this.fetchConfiguration(serverETag);
      
      if (hasUpdate) {
        this.renderUserInterface();
        this.saveETagToStorage();
      }
    }
  }

  /**
   * 判斷是否需要更新
   */
  shouldUpdate(serverETag) {
    return serverETag !== this.currentETag;
  }

  // ==================== 數據獲取 ====================

  /**
   * 獲取配置數據
   */
  async fetchConfiguration(serverETag) {
    const headers = this.buildFetchHeaders(serverETag);
    
    console.log('🌐 發送 /ui/config 請求', {
      url: '/ui/config',
      headers: headers,
      serverETag: serverETag,
      method: 'GET'
    });

    try {
      console.log('🔗 發送 fetch 請求到 /ui/config');
      const response = await fetch('/ui/config', { headers });

      console.log('📊 響應狀態:', response.status, response.statusText);
      console.log('📋 響應 ETag:', response.headers.get('ETag'));
      
      const result = await this.processFetchResponse(response, serverETag);
      console.log('🔄 processFetchResponse 結果:', result);
      
      return result;
    } catch (error) {
      console.error('❌ 獲取配置失敗:', error);
      throw error;
    }
  }

  /**
   * 建構請求標頭
   */
  buildFetchHeaders(serverETag) {
    const headers = {};
    // 只有在已初始化且 ETag 相同時才設置 If-None-Match（利用快取）
    if (this.isInitialized && this.currentETag && this.currentETag === serverETag) {
      headers['If-None-Match'] = this.currentETag;
    }
    return headers;
  }

  /**
   * 處理 fetch 回應
   */
  async processFetchResponse(response, serverETag) {
    
    if (response.status === 304) {
      console.log('📋 配置無變化 (304)，使用快取');
      return false;
    }
    
    if (response.ok) {
      const result = await response.json();

      console.log('📦 GET /ui/config 完整響應 JSON:', JSON.stringify(result, null, 2));
      console.log('📹 相機數量:', result.data?.cameras?.length || 0);
      
      this.config = result.data;
      this.updateETag(response, serverETag);
      console.log('📥 配置已載入，當前 ETag:', this.currentETag);
      return true;
    }
    
    console.warn('⚠️ 響應不成功，狀態:', response.status, response.statusText);
    return false;
  }

  /**
   * 更新 ETag
   */
  updateETag(response, fallbackETag) {
    const etag = response.headers.get('ETag');
    this.currentETag = etag ? etag.replace(/^W\/"|"$/g, '') : fallbackETag;
  }

  // ==================== UI 渲染 ====================

  /**
   * 渲染用戶界面
   */
  renderUserInterface() {
    if (!this.config) {
      console.warn('⚠️ 無配置數據，跳過渲染');
      return;
    }
    
    console.log('🎨 渲染 UI...');
    
    this.updateCameraDisplay();
    this.updateWorkstationSelector();
    this.updatePollingTimeDisplay();
    this.refreshFilters();
    
    // 重新綁定相機卡片點擊事件（針對動態創建的卡片）
    if (typeof bindCameraClickEvents === 'function') {
      bindCameraClickEvents();
    }
  }

  /**
   * 更新相機顯示 - 支援動態載入
   */
  updateCameraDisplay() {
    const cameras = this.config.cameras || [];
    const statusMap = this.buildCameraStatusMap();
    const cameraGrid = document.querySelector('.camera-grid');
    
    if (!cameraGrid) {
      console.error('❌ 找不到相機網格容器');
      return;
    }
    
    console.log(`🔄 開始更新相機顯示，配置中有 ${cameras.length} 個相機`);
    
    // 如果網格是空的，動態創建所有相機卡片
    if (cameraGrid.children.length === 0 && cameras.length > 0) {
      console.log('🆕 相機網格為空，動態創建相機卡片');
      this.renderCameraCards(cameras, statusMap, cameraGrid);
    } else {
      // 更新現有相機卡片
      const cameraCards = cameraGrid.querySelectorAll('.camera-card');
      console.log(`🔄 更新 ${cameraCards.length} 個現有相機卡片`);
      
      cameraCards.forEach(card => {
        const channel = parseInt(card.getAttribute('data-channel'));
        const camera = cameras.find(cam => cam.NVR_channel === channel);
        
        if (camera) {
          this.updateCameraCard(card, camera, statusMap);
        } else {
          card.style.display = 'none';
        }
      });
    }
    
    console.log(`📹 相機顯示更新完成`);
  }

  /**
   * 動態渲染相機卡片到網格容器
   */
  renderCameraCards(cameras, statusMap, container) {
    const cardsHtml = cameras.map(camera => {
      const channel = camera.NVR_channel;
      const status = statusMap.get(channel) || { type: 'waiting_polling', ip: 'unknown' };
      const functionSwitches = camera.function_switches || {};

      const imgSrc = camera.picture || camera.base64;
      
      // 如果相機斷線（isError），顯示異常，否則顯示圖片或空白區域
      const imageSection = status.isError
        ? `<div class="camera-no-image"><span class="error-text" style="color: white;">異常</span></div>`
        : (camera.base64 && camera.base64.trim()
          ? `<img src="${camera.base64}" alt="Camera ${channel}" class="camera-image">`
          : `<div class="camera-no-image"></div>`);
      
      // 功能按鈕
      let buttons = '';
      if (functionSwitches.adjust_camera_zoom_on_detection) {
        buttons += '<button class="button blue">追焦</button>';
      }
      if (functionSwitches.enable_camera_ptz) {
        buttons += '<button class="button green">巡弋</button>';
      }
      if (functionSwitches.stage1_detection) {
        buttons += '<button class="button orange">一階</button>';
      }
      if (functionSwitches.stage2_detection) {
        buttons += '<button class="button brown">二階</button>';
      }
      
      const statusText = {
        'current_polling': '輪詢中',
        'waiting_polling': '未輪詢',
        'fixed': '常駐'
      }[status.type] || status.type;
      const workstationName = window.getWorkstationDisplayName(status.ip);

      return `
        <div class="camera-card" data-type="${status.type}" data-ip="${status.ip}" data-channel="${channel}" data-fn='${JSON.stringify(functionSwitches)}'>
          <div class="image-wrapper">
            ${imageSection}
            <div class="button-group over-image">${buttons}</div>
          </div>
          <div class="camera-info">
            <div class="camera-info-row">
              ${camera.location ? `<div class="camera-location">${camera.location}</div>` : ''}
              <div class="camera-status ${status.type}">
                <span class="status-dot"></span>
                <span class="status-indicator">${statusText}</span>
              </div>
            </div>
            <div class="camera-channel">
              <span>連接頻道：${channel}</span>
              <span class="workstation-badge" title="${status.ip}">${workstationName}</span>
            </div>
          </div>
        </div>
      `;
    }).join('');
    
    container.innerHTML = cardsHtml;
    console.log(`✅ 已動態創建 ${cameras.length} 個相機卡片`);
  }

  /**
   * 更新現有相機卡片
   */
  updateCameraCard(card, camera, statusMap) {
    const channel = camera.NVR_channel;
    const status = statusMap.get(channel) || { type: 'waiting_polling', ip: 'unknown' };
    const functionSwitches = camera.function_switches || {};
    
    // 更新 data 屬性
    card.setAttribute('data-type', status.type);
    card.setAttribute('data-ip', status.ip);
    card.setAttribute('data-fn', JSON.stringify(functionSwitches));

    const imgSrc = camera.picture || camera.base64;
    
    // 更新圖片
    const imageWrapper = card.querySelector('.image-wrapper');
    const existingImg = imageWrapper.querySelector('.camera-image');
    const existingNoImage = imageWrapper.querySelector('.camera-no-image');
    
    if (status.isError) {
      // 斷線：顯示異常 (保持不變)
      if (!existingNoImage || !existingNoImage.innerHTML.includes('異常')) {
        if (existingImg) existingImg.remove();
        if (existingNoImage) existingNoImage.remove();
        const noImageDiv = document.createElement('div');
        noImageDiv.className = 'camera-no-image';
        noImageDiv.innerHTML = '<span class="error-text" style="color: white;">異常</span>';
        imageWrapper.insertBefore(noImageDiv, imageWrapper.querySelector('.button-group'));
      }
    } else if (imgSrc && imgSrc.trim()) { // [MOD] 使用 imgSrc 判斷
      // 有圖片：顯示圖片
      if (existingImg) {
        existingImg.src = imgSrc; // [MOD] 使用 imgSrc
      } else {
        // 替換無圖片區塊
        if (existingNoImage) existingNoImage.remove();
        const img = document.createElement('img');
        img.src = imgSrc; // [MOD] 使用 imgSrc
        img.alt = `Camera ${channel}`;
        img.className = 'camera-image';
        imageWrapper.insertBefore(img, imageWrapper.querySelector('.button-group'));
      }
    } else {
      // 無圖片但不是斷線：顯示空白區域 (保持不變)
      if (!existingNoImage || existingNoImage.innerHTML.includes('異常')) {
        if (existingImg) existingImg.remove();
        if (existingNoImage) existingNoImage.remove();
        const noImageDiv = document.createElement('div');
        noImageDiv.className = 'camera-no-image';
        imageWrapper.insertBefore(noImageDiv, imageWrapper.querySelector('.button-group'));
      }
    }
    
    // 更新功能按鈕
    const buttonGroup = card.querySelector('.button-group');
    buttonGroup.innerHTML = `
      ${functionSwitches.adjust_camera_zoom_on_detection ? '<button class="button blue">追焦</button>' : ''}
      ${functionSwitches.enable_camera_ptz ? '<button class="button green">巡弋</button>' : ''}
      ${functionSwitches.stage1_detection ? '<button class="button orange">一階</button>' : ''}
      ${functionSwitches.stage2_detection ? '<button class="button brown">二階</button>' : ''}
    `;
    
    // 更新位置信息
    const locationDiv = card.querySelector('.camera-location');
    if (camera.location) {
      if (locationDiv) {
        locationDiv.textContent = camera.location;
      } else {
        // 創建位置元素
        const newLocationDiv = document.createElement('div');
        newLocationDiv.className = 'camera-location';
        newLocationDiv.textContent = camera.location;
        const infoRow = card.querySelector('.camera-info-row');
        infoRow.insertBefore(newLocationDiv, infoRow.firstChild);
      }
    } else {
      if (locationDiv) locationDiv.remove();
    }
    
    // 更新狀態
    const statusDiv = card.querySelector('.camera-status');
    const statusTextMap = {
      'current_polling': '輪詢中',
      'waiting_polling': '未輪詢',
      'fixed': '常駐'
    };
    
    statusDiv.className = `camera-status ${status.type}`;
    const statusIndicator = statusDiv.querySelector('.status-indicator');
    statusIndicator.textContent = statusTextMap[status.type] || status.type;
    
    // 確保卡片可見
    card.style.display = '';
  }

  // (已刪除舊的 createCameraCard 和相關函數，現在使用 updateCameraCard 方式)

  /**
   * 建立相機狀態映射
   */
  buildCameraStatusMap() {
    const statusMap = new Map();
    const workstations = this.config.workstation_ip_mapping || {};
    
    Object.entries(workstations).forEach(([ip, data]) => {
      this.mapCameraStatus(statusMap, ip, data);
    });
    
    return statusMap;
  }

  /**
   * 映射相機狀態
   */
  mapCameraStatus(statusMap, ip, workstationData) {
    // 輪詢中
    (workstationData.current_polling_cameras || []).forEach(channel => {
      statusMap.set(channel, { type: 'current_polling', ip });
    });
    
    // 等待輪詢
    (workstationData.polling_cameras || []).forEach(channel => {
      if (!statusMap.has(channel)) {
        statusMap.set(channel, { type: 'waiting_polling', ip });
      }
    });
    
    // 固定顯示
    (workstationData.fixed_cameras || []).forEach(channel => {
      statusMap.set(channel, { type: 'fixed', ip });
    });
    
    // 斷線狀態（疊加）
    (workstationData.disconnect_cameras || []).forEach(channel => {
      const existing = statusMap.get(channel) || { type: 'waiting_polling', ip };
      existing.isError = true;
      statusMap.set(channel, existing);
    });
  }

  /**
   * 更新單個相機卡片
   */
  updateSingleCameraCard(card, cameras, statusMap) {
    const channel = parseInt(card.getAttribute('data-channel'));
    const camera = cameras.find(cam => cam.NVR_channel === channel);
    const status = statusMap.get(channel) || { type: 'waiting_polling', ip: 'unknown' };
    
    if (!camera) return;
    
    // 更新卡片屬性
    this.updateCardAttributes(card, status);
    
    // 更新預覽圖
    this.updateCardImage(card, camera);
    
    // 更新狀態顯示
    this.updateCardStatus(card, status);
  }

  /**
   * 更新卡片屬性
   */
  updateCardAttributes(card, status) {
    card.setAttribute('data-type', status.type);
    card.setAttribute('data-ip', status.ip);
    card.setAttribute('data-error', status.isError ? 'true' : 'false');
  }

  /**
   * 更新卡片圖片
   */
  updateCardImage(card, camera) {
    const img = card.querySelector('.camera-image');
    const noImageDiv = card.querySelector('.camera-no-image');
    
    if (camera.base64) {
      if (img) {
        img.src = camera.base64;
        img.style.display = '';
      }
      if (noImageDiv) noImageDiv.style.display = 'none';
    } else {
      if (img) img.style.display = 'none';
      if (noImageDiv) noImageDiv.style.display = '';
    }
  }

  /**
   * 更新卡片狀態
   */
  updateCardStatus(card, status) {
    const statusEl = card.querySelector('.camera-status');
    if (!statusEl) return;
    
    const statusText = {
      'current_polling': '輪詢中',
      'waiting_polling': '未輪詢',
      'fixed': '常駐'
    };
    
    statusEl.className = 'camera-status ' + status.type;
    
    const indicator = statusEl.querySelector('.status-indicator');
    if (indicator) {
      indicator.textContent = statusText[status.type] || status.type;
    }
  }

  /**
   * 更新工作站選擇器
   */
  updateWorkstationSelector() {
    const select = document.getElementById('target-ip-select');
    if (!select) return;
    
    const currentValue = select.value;
    const workstationIPs = Object.keys(this.config.workstation_ip_mapping || {}).sort();
    
    // 重建選項
    select.innerHTML = '';
    
    const defaultOption = document.createElement('option');
    defaultOption.value = '';
    defaultOption.textContent = '請選擇工作站';
    select.appendChild(defaultOption);
    
    workstationIPs.forEach(ip => {
      const option = document.createElement('option');
      option.value = ip;
      option.textContent = window.getWorkstationDisplayName(ip);
      select.appendChild(option);
    });
    
    // 恢復選中值
    select.value = currentValue;
  }

  /**
   * 更新頁面標題和輪詢時間顯示
   */
  updatePollingTimeDisplay() {
    // 更新相機數量標題
    const titleElement = document.querySelector('.header h2');
    if (titleElement) {
      const cameraCount = this.config.cameras ? this.config.cameras.length : 0;
      titleElement.textContent = `總共管理 ${cameraCount} 個監視器`;
    }
    
    // 更新輪詢時間
    const pollingTimeElement = document.querySelector('.polling-time');
    if (pollingTimeElement) {
      const pollingTime = this.config.polling_time !== undefined ? this.config.polling_time : 'N/A';
      pollingTimeElement.textContent = `輪詢間隔時間: ${pollingTime} 分鐘`;
    }
  }

  /**
   * 刷新篩選器
   */
  refreshFilters() {
    // 檢查篩選器 UI 是否已初始化，如果沒有則初始化
    const sidebar = document.getElementById('sidebar');
    const hasFilters = sidebar && sidebar.querySelector('.ip-group');
    
    if (!hasFilters && typeof initializeFiltersUI === 'function') {
      console.log('🔄 初始化篩選器 UI...');
      initializeFiltersUI();
    } else {
      // 呼叫現有的篩選器更新函數
      if (typeof updateFilterCounts === 'function') {
        updateFilterCounts();
      }
      
      if (typeof siderbarChangeHandler === 'function') {
        siderbarChangeHandler();
      }
    }
  }

  // ==================== 錯誤處理與降級 ====================

  /**
   * 處理連線錯誤
   */
  handleConnectionError() {
    console.warn('🔄 SSE 連線中斷，啟動降級模式');
    setTimeout(() => {
      this.startPollingMode();
    }, 1000);
  }

  /**
   * 啟動輪詢模式
   */
  startPollingMode() {
    if (this.pollingInterval) return;
    
    console.log('📡 啟動輪詢模式');
    this.pollingInterval = setInterval(async () => {
      await this.performPollingUpdate();
    }, 5000);
  }

  /**
   * 執行輪詢更新
   */
  async performPollingUpdate() {
    try {
      const hasUpdate = await this.fetchConfiguration();
      
      if (hasUpdate) {
        if (!this.isInitialized) {
          await this.performInitialSetup(this.currentETag);
        } else {
          this.renderUserInterface();
          this.saveETagToStorage();
        }
      }
    } catch (error) {
      console.error('❌ 輪詢失敗:', error);
    }
  }

  /**
   * 停止輪詢模式
   */
  stopPollingMode() {
    if (this.pollingInterval) {
      clearInterval(this.pollingInterval);
      this.pollingInterval = null;
      console.log('⏹️ 輪詢模式已停止');
    }
  }

  // ==================== 數據持久化 ====================

  /**
   * 從儲存載入 ETag
   */
  loadETagFromStorage() {
    return localStorage.getItem('smoke_ui_etag') || null;
  }

  /**
   * 儲存 ETag 到本地
   */
  saveETagToStorage() {
    if (this.currentETag) {
      localStorage.setItem('smoke_ui_etag', this.currentETag);
    }
  }

  /**
   * 嘗試從快取恢復
   */
  tryRestoreFromCache() {
    console.log('🔄 嘗試從快取恢復狀態...');
    // 未來可以擴展快取恢復邏輯
  }

  // ==================== API 操作 ====================

  /**
   * 更新工作站分組
   */
  async updateWorkstationGroups(workstationIP, pollingCameras, fixedCameras) {
    try {
      const response = await fetch('/config/send_groups', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          workstation_ip: workstationIP,
          polling_cameras: pollingCameras,
          fixed_cameras: fixedCameras
        })
      });

      return await this.handleGroupUpdateResponse(response);
    } catch (error) {
      console.error('❌ 分組更新請求失敗:', error);
      return false;
    }
  }

  /**
   * 處理分組更新回應
   */
  async handleGroupUpdateResponse(response) {
    if (response.ok) {
      const result = await response.json();
      console.log('✅ 分組更新成功:', result);
      
      // 直接使用回應中的配置，無需等待 SSE
      if (result.data?.ui_config) {
        this.config = result.data.ui_config;
        this.updateETag(response, result.data.etag);
        this.renderUserInterface();
        this.saveETagToStorage();
      }
      
      return true;
    } else {
      const error = await response.json();
      console.error('❌ 分組更新失敗:', error.message);
      return false;
    }
  }

  /**
   * 更新相機設定
   */
  async updateCameraSetting(channelId, functionSwitches) {
    try {
      const response = await fetch('/config/send_setting', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          NVR_channel: channelId,
          function_switches: functionSwitches
        })
      });

      return await this.handleSettingUpdateResponse(response);
    } catch (error) {
      console.error('❌ 相機設定更新失敗:', error);
      return false;
    }
  }

  /**
   * 處理設定更新回應
   */
  async handleSettingUpdateResponse(response) {
    if (response.ok) {
      const result = await response.json();
      console.log('✅ 相機設定更新成功:', result);
      
      // 直接使用回應中的配置，無需等待 SSE
      if (result.data?.ui_config) {
        this.config = result.data.ui_config;
        this.updateETag(response, result.data.etag);
        this.renderUserInterface();
        this.saveETagToStorage();
      }
      
      return true;
    } else {
      const error = await response.json();
      console.error('❌ 相機設定更新失敗:', error.message);
      return false;
    }
  }

  // ==================== 生命週期管理 ====================

  /**
   * 清理資源
   */
  destroy() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
      console.log('🔌 SSE 連線已關閉');
    }
    
    this.stopPollingMode();
    
    // 清理跨頁面通信
    if (this.broadcastChannel) {
      this.broadcastChannel.close();
      this.broadcastChannel = null;
      console.log('📡 BroadcastChannel 已關閉');
    }
  }
}
