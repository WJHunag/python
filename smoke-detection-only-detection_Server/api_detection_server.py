from flask import Flask, request, jsonify
import os
import cv2
import mmcv
import base64
from werkzeug.utils import secure_filename
from mmcv.transforms import Compose
from mmdet.apis import inference_detector, init_detector
import argparse
from io import BytesIO
from PIL import Image

# 定義命令列參數解析
def parse_args():
    parser = argparse.ArgumentParser(description='API for single image detection')
    parser.add_argument('config', help='Config檔案的路徑')
    parser.add_argument('checkpoint', help='權重檔案的路徑')
    parser.add_argument('--device', default='cuda:0', help='設備 (如 cuda:0 或 cpu)')
    parser.add_argument('--score-thr', type=float, default=0.6, help='檢測置信度閾值')
    return parser.parse_args()

# 解析參數
args = parse_args()

# 初始化模型
model = init_detector(args.config, args.checkpoint, device=args.device)
model.cfg.test_dataloader.dataset.pipeline[0].type = 'mmdet.LoadImageFromNDArray'
test_pipeline = Compose(model.cfg.test_dataloader.dataset.pipeline)

# 建立 Flask 應用程式
app = Flask(__name__)

@app.route('/detect', methods=['POST'])
def detect_image():
    """
    API 端點，用於檢測單張圖片中的物件。
    """
    if 'image' not in request.files:
        return jsonify({"error": "未提供圖片"}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "未選擇圖片"}), 400
    
    # 讀取上傳的圖片
    filename = secure_filename(file.filename)
    file_path = os.path.join("/tmp", filename)
    file.save(file_path)
    frame = mmcv.imread(file_path)
    
    # 執行檢測
    result = inference_detector(model, frame, test_pipeline)

    # 處理檢測結果
    if hasattr(result, 'pred_instances'):
        valid_boxes = result.pred_instances['scores'] > args.score_thr
        bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy().tolist()
        scores = result.pred_instances['scores'][valid_boxes].cpu().numpy().tolist()
        
        # 繪製結果到圖片
        for bbox in result.pred_instances['bboxes'][valid_boxes].cpu().numpy():
            x1, y1, x2, y2 = map(int, bbox)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)  # 綠色框

        # 將圖片轉換為 Base64
        _, buffer = cv2.imencode('.jpg', frame)
        result_image_base64 = base64.b64encode(buffer).decode('utf-8')
        
        response = {
            "bboxes": bboxes,
            "scores": scores,
            "result_image_base64": result_image_base64
        }
    else:
        response = {"error": "沒有檢測到結果"}
    
    # 清理暫存圖片
    os.remove(file_path)
    return jsonify(response), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5006, debug=False)
