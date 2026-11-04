/**
 * 應用程式初始化
 * 負責啟動 SSE 管理器和綁定全域事件
 */

// 全域 SSE 管理器實例
let sseManager = null;

/**
 * 初始化應用程式
 */
function initializeApp() {
  console.log('🚀 初始化煙霧監控系統...');
  
  // 創建 SSE 管理器
  sseManager = new SSEManager();
  
  // 啟動 SSE 連線
  sseManager.init();
  
  // 綁定刷新按鈕事件
  bindRefreshButton();
  
  // 綁定工作站分組更新邏輯
  bindGroupUpdateLogic();
  
  // 註冊清理邏輯
  registerCleanupHandlers();
  
  console.log('✅ 應用程式初始化完成');
  
  // 篩選器 UI 將由 SSE 管理器在適當時機初始化
}

/**
 * 綁定刷新按鈕事件
 */
function bindRefreshButton() {
  console.log('🔧 開始綁定刷新按鈕事件...');
  const refreshBtn = document.getElementById('refresh-btn');
  console.log('🔍 找到刷新按鈕:', refreshBtn ? '是' : '否', refreshBtn);
  
  if (refreshBtn) {
    console.log('✅ 成功綁定刷新按鈕點擊事件');
    refreshBtn.addEventListener('click', async () => {
      console.log('🔄 手動刷新數據...');
      
      if (sseManager) {
        try {
          console.log('📤 強制發送 ui/config 請求（忽略 ETag）');

          // 強制刷新：不帶任何 ETag，確保獲取最新數據
          const hasUpdate = await sseManager.fetchConfiguration(null);

          console.log('📋 強制更新結果:', hasUpdate ? '有更新' : '無更新');

          if (hasUpdate) {
            console.log('🔄 重新渲染 UI...');
            sseManager.renderUserInterface();
            sseManager.saveETagToStorage();
            console.log('📋 新 ETag:', sseManager.currentETag);
            showNotification('✅ 數據已強制更新', 'success');
          } else {
            showNotification('ℹ️ 強制刷新完成', 'info');
          }
        } catch (error) {
          console.error('❌ 刷新失敗:', error);
          showNotification('❌ 刷新失敗，請稍後重試', 'error');
        }
      }
    });
  }
}

/**
 * 綁定分組更新邏輯
 */
function bindGroupUpdateLogic() {
  // 修改現有的設定模式邏輯，整合 API 調用
  window.applyTypeToSelected = async function(newType) {
    console.log('🔧 applyTypeToSelected 被調用，參數:', newType);

    // 收集選中的相機和目標 IP
    const selectedCameras = collectSelectedCameras();
    const targetIP = getTargetWorkstationIP();

    console.log('📋 收集到的數據:', {
      selectedCameras: selectedCameras.length,
      targetIP,
      newType
    });

    // 驗證必要條件
    if (!validateGroupAssignment(selectedCameras, targetIP, newType)) {
      console.warn('❌ 驗證失敗，停止執行');
      return;
    }

    console.log('🔍 條件檢查:', {
      'selectedCameras.length': selectedCameras.length,
      'targetIP': targetIP
    });

    if (selectedCameras.length > 0 && targetIP) {
      console.log('✅ 所有條件都滿足，開始處理API請求');

      // 顯示處理中狀態
      showProcessingFeedback(selectedCameras.length, targetIP, newType);

      try {
        // 根據類型分類相機
        const groups = categorizeSelectedCameras(selectedCameras, newType);

        // 發送更新請求
        const result = await APIService.updateWorkstationGroups(
          targetIP,
          groups.polling,
          groups.fixed
        );


        if (result.success) {
          console.log('✅ API 調用成功！', result);
          // 只有API成功時才執行UI更新
          if (typeof updateCameraCardsUI === 'function') {
            updateCameraCardsUI(newType);
          }
          showSuccessFeedback(selectedCameras.length, targetIP, newType);
        } else {
          console.error('❌ API 調用失敗:', result);
          showErrorFeedback(result.error || 'API 請求失敗，請稍後重試');
        }
      } catch (error) {
        console.error('❌ 分組更新錯誤:', error);
        showErrorFeedback('網路錯誤或服務器異常');
      }
    }
  };
}

/**
 * 收集選中的相機
 */
function collectSelectedCameras() {
  const cameras = [];
  const cameraDetails = [];
  
  document.querySelectorAll('.camera-card.selected').forEach(card => {
    const channel = parseInt(card.getAttribute('data-channel'));
    if (!isNaN(channel)) {
      cameras.push(channel);
      
      // 收集相機詳細資訊（用於日誌和反饋）
      const locationElement = card.querySelector('.camera-location');
      const location = locationElement ? locationElement.textContent.trim() : `相機 ${channel}`;
      const currentType = card.getAttribute('data-type') || 'unknown';
      const currentIP = card.getAttribute('data-ip') || 'unknown';
      
      cameraDetails.push({
        channel,
        location,
        currentType,
        currentIP
      });
    }
  });
  
  // 在控制台輸出選中的相機資訊
  if (cameras.length > 0) {
    console.log('📹 已選中的相機:', cameraDetails);
  }
  
  return cameras;
}

/**
 * 獲取目標工作站 IP
 */
function getTargetWorkstationIP() {
  const select = document.getElementById('target-ip-select');
  return select ? select.value : null;
}

/**
 * 根據類型分類選中的相機
 */
function categorizeSelectedCameras(cameras, type) {
  const groups = { polling: [], fixed: [] };
  
  cameras.forEach(channel => {
    if (type === 'current_polling' || type === 'waiting_polling') {
      groups.polling.push(channel);
    } else if (type === 'fixed') {
      groups.fixed.push(channel);
    }
  });
  
  return groups;
}

/**
 * 驗證批次分組設定
 */
function validateGroupAssignment(selectedCameras, targetIP, newType) {
  console.log('🔍 開始驗證分組設定:', {
    selectedCameras: selectedCameras.length,
    targetIP,
    newType
  });

  if (selectedCameras.length === 0) {
    console.warn('❌ 驗證失敗: 沒有選中相機');
    showErrorFeedback('請至少選擇一個相機');
    return false;
  }

  if (!targetIP || targetIP.trim() === '') {
    console.warn('❌ 驗證失敗: 沒有選擇目標工作站 IP');
    showErrorFeedback('請選擇目標工作站 IP');
    return false;
  }

  if (!newType || !['current_polling', 'waiting_polling', 'fixed'].includes(newType)) {
    console.warn('❌ 驗證失敗: 無效的運作模式');
    showErrorFeedback('請選擇有效的運作模式');
    return false;
  }

  console.log('✅ 驗證通過');
  return true;
}

/**
 * 顯示處理中反饋
 */
function showProcessingFeedback(cameraCount, targetIP, newType) {
  const modeText = newType === 'fixed' ? '常駐' : '輪詢';
  const message = `正在將 ${cameraCount} 個相機設定為 ${targetIP} 的${modeText}模式...`;
  console.log(`[PROCESSING] ${message}`);
  
  // 可以在這裡添加載入動畫或進度條
  updateSettingsButtonState(true, '設定中...');
}

/**
 * 顯示成功反饋
 */
function showSuccessFeedback(cameraCount, targetIP, newType) {
  const modeText = newType === 'fixed' ? '常駐' : '輪詢';
  const message = `✅ 已成功將 ${cameraCount} 個相機設定為 ${targetIP} 的${modeText}模式`;
  console.log(`[SUCCESS] ${message}`);
  
  updateSettingsButtonState(false, '設定');
  
  // 可以添加成功動畫或 Toast 通知
  showTemporaryMessage(message, 'success');
}

/**
 * 顯示錯誤反饋
 */
function showErrorFeedback(errorMessage) {
  const message = `❌ 分組設定失敗: ${errorMessage}`;
  console.error(`[ERROR] ${message}`);
  
  updateSettingsButtonState(false, '設定');
  
  // 顯示錯誤提示
  alert(message);
}

/**
 * 更新設定按鈕狀態
 */
function updateSettingsButtonState(isProcessing, text) {
  const settingsBtn = document.getElementById('settings-toggle-btn');
  if (settingsBtn) {
    settingsBtn.disabled = isProcessing;
    settingsBtn.textContent = text;
  }
}

/**
 * 顯示臨時訊息
 */
function showTemporaryMessage(message, type = 'info') {
  console.log(`[${type.toUpperCase()}] ${message}`);
  
  // 簡單實現：控制台輸出
  // 未來可以實現更優雅的 Toast 通知系統
}

/**
 * 顯示通知訊息（舊版本兼容）
 */
function showNotification(message, type = 'info') {
  showTemporaryMessage(message, type);
}

/**
 * 註冊清理處理器
 */
function registerCleanupHandlers() {
  // 頁面卸載時清理資源
  window.addEventListener('beforeunload', () => {
    if (sseManager) {
      sseManager.destroy();
      sseManager = null;
    }
  });
  
  // 頁面隱藏時可以選擇性暫停 SSE（移動設備省電）
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      console.log('📱 頁面隱藏，SSE 保持運行');
      // 可以在這裡添加暫停邏輯
    } else {
      console.log('📱 頁面顯示，SSE 恢復正常');
      // 可以在這裡添加恢復邏輯
    }
  });
}

/**
 * 提供全域存取 SSE 管理器的方法
 */
window.getSSeManager = function() {
  return sseManager;
};

// DOM 載入完成後初始化
document.addEventListener('DOMContentLoaded', initializeApp);