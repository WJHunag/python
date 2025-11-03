from flask import Flask, request, jsonify
import os
import glob

app = Flask(__name__)

# 清理資料夾功能
def clear_input_folder(input_folder, frames_by_camera):
    """清空 input 資料夾和 frames_by_camera"""
    try:
        image_files = glob.glob(os.path.join(input_folder, '*.jpg')) + \
                      glob.glob(os.path.join(input_folder, '*.jpeg')) + \
                      glob.glob(os.path.join(input_folder, '*.png'))
        for file in image_files:
            os.remove(file)
        for camera_ip in frames_by_camera:
            frames_by_camera[camera_ip] = []
        print(f"Cleared input folder and frames.")
        return {"status": "success", "message": "Input folder and frames cleared"}
    except Exception as e:
        print(f"Error clearing folder: {e}")
        return {"status": "error", "message": str(e)}

# 處理工作站啟動的通知
@app.route('/workstation_restarted', methods=['POST'])
def workstation_restarted():
    """處理工作站啟動通知"""
    # 根據需求設置資料夾路徑和 frames_by_camera 狀態
    input_folder = './input'
    frames_by_camera = {}  # 假設這是從主程式中共享的資料

    result = clear_input_folder(input_folder, frames_by_camera)
    return jsonify(result)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5005)
