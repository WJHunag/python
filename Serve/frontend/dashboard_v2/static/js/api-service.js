/**
 * API 服務層
 * 統一管理所有API調用
 */
const APIService = {
  /**
   * 更新工作站分組
   */
  async updateWorkstationGroups(workstationIP, pollingCameras, fixedCameras) {
    try {
      const payload = {
        workstation_ip: workstationIP,
        polling_cameras: pollingCameras || [],
        fixed_cameras: fixedCameras || []
      };

      console.log('📤 發送API請求:', payload);

      const response = await fetch('/config/send_groups', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      });

      const result = await response.json();

      if (response.ok) {
        console.log('✅ POST /config/send_groups API請求成功:', JSON.stringify(result, null, 2));
        return { success: true, data: result };
      } else {
        console.error('❌ POST /config/send_groups API請求失敗:', response.status, result);
        return {
          success: false,
          error: result.message || `HTTP ${response.status}`,
          status: response.status
        };
      }
    } catch (error) {
      console.error('❌ API網路錯誤:', error);
      return {
        success: false,
        error: '網路錯誤或服務器無法連接',
        networkError: true
      };
    }
  },

  /**
   * 獲取UI配置
   */
  async getUIConfig() {
    try {
      const response = await fetch('/ui/config');
      if (response.ok) {
        return await response.json();
      } else {
        throw new Error(`HTTP ${response.status}`);
      }
    } catch (error) {
      console.error('❌ 獲取配置失敗:', error);
      throw error;
    }
  },

  /**
   * 發送單個相機設定
   */
  async updateCameraSetting(cameraData) {
    try {
      console.log('📤 發送相機設定:', cameraData);

      const response = await fetch('/config/send_setting', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(cameraData)
      });

      const result = await response.json();

      if (response.ok) {
        console.log('✅ POST /config/send_setting 相機設定成功:', JSON.stringify(result, null, 2));
        return { success: true, data: result };
      } else {
        console.error('❌ POST /config/send_setting 相機設定失敗:', response.status, result);
        return {
          success: false,
          error: result.message || `HTTP ${response.status}`,
          status: response.status
        };
      }
    } catch (error) {
      console.error('❌ 相機設定網路錯誤:', error);
      return {
        success: false,
        error: '網路錯誤或服務器無法連接',
        networkError: true
      };
    }
  }
};