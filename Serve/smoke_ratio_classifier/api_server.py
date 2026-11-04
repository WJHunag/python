# -*- coding: utf-8 -*-
import os
import time
import tempfile
from typing import List, Optional
from flask import Flask, request, jsonify

# [NEW] 引入 shutil 函式庫，用於複製檔案
import shutil 

# 直接沿用你現有的邏輯與模型載入
import classify_smoke as cs
from ultralytics import YOLO

"""
環境變數
- WEIGHTS: YOLOv8 segmentation 權重路徑（容器內路徑），例如 /weights/yolov8s-seg.pt
- CONF:    預設偵測 conf 門檻 (float)，例如 0.4
- USE_GRAY_WORLD:  是否啟用灰世界白平衡 (0/1)
- HOST/PORT: 綁定位址與埠（預設 0.0.0.0:8000）
- SAVE_INFER_PATH: (可選) 儲存分析後圖片的根目錄。若設定此變數，才會啟用儲存功能。
"""

WEIGHTS = os.getenv("WEIGHTS", "./best.pt")
DEFAULT_CONF = float(os.getenv("CONF", "0.4"))
USE_GRAY_WORLD = os.getenv("USE_GRAY_WORLD", "0") == "1"

# [NEW] 讀取儲存路徑開關
SAVE_INFER_PATH = os.getenv("SAVE_INFER_PATH", None)
if SAVE_INFER_PATH:
    print(f"[INFO] 啟用儲存功能，將儲存 steam/black_smoke 圖片至: {SAVE_INFER_PATH}")

# 套用到你的 classify_smoke 流程
cs.USE_GRAY_WORLD = USE_GRAY_WORLD

# 啟動時載入模型（避免每次請求重載）
model = YOLO(WEIGHTS)

app = Flask(__name__)

def summarize_image_label(det_labels: List[Optional[str]]) -> str:
    """
    影像彙總標籤策略（保守告警）：
    - 任一實例為 black_smoke -> 回 black_smoke
    - 否則若存在 steam -> 回 steam
    - 否則（無偵測或皆 None）-> 回 none
    """
    if any(lbl == "black_smoke" for lbl in det_labels):
        return "black_smoke"
    if any(lbl == "steam" for lbl in det_labels):
        return "steam"
    return "none"

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "weights": WEIGHTS,
        "use_gray_world": USE_GRAY_WORLD,
        "default_conf": DEFAULT_CONF,
        # [NEW] 回報健康狀態時也顯示儲存路徑
        "saving_to_path": SAVE_INFER_PATH
    })

@app.route("/infer", methods=["POST"])
def infer():
    t0 = time.time()

    if "file" not in request.files:
        return jsonify({"error": "缺少 file 參數（multipart/form-data）"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "未選擇任何檔案"}), 400

    # conf 可從 form 或 query 取得
    conf = request.form.get("conf", None)
    if conf is None:
        conf = request.args.get("conf", DEFAULT_CONF)
    try:
        conf = float(conf)
    except Exception:
        conf = DEFAULT_CONF

    # 儲存到暫存檔，因為 analyze_one 需要檔案路徑

    ext = os.path.splitext(file.filename)[1] or ".jpg"
    fd, tmp_path = tempfile.mkstemp(suffix=ext)
    os.close(fd)
    
    # [MODIFIED] 為了確保變數在 try/except/finally 之後都能被存取
    # 我們在 try 之前定義好預設值
    result = {}
    dets = []
    image_final_label = "none" # 預設為 none

    try:
        # 1. 儲存暫存檔
        file.save(tmp_path)
        # 2. 執行分析
        result = cs.analyze_one(tmp_path, model=model, conf=conf)

        # ========= [MOVED] 邏輯修正 =========
        # 將所有「後處理」與「儲存」邏輯移至 try 內部
        # 確保 tmp_path 仍然存在

        # 3. 整理輸出（只抽取需要的欄位）
        for d in result.get("detections", []):
            ratio_data = d.get("ratio_logic_data", {}) or {}
            dets.append({
                "id": d.get("id"),
                "final_label": d.get("final_label"),
                "conf": d.get("conf"),
                "box": d.get("box_xyxy"),
                "ratio": ratio_data.get("ratio")
            })

        # 4. 彙總影像標籤
        image_final_label = summarize_image_label([d.get("final_label") for d in result.get("detections", [])])

        # 5. 儲存檔案的核心邏輯
        if SAVE_INFER_PATH and image_final_label in ("steam", "black_smoke"):
            try:
                # 決定目標資料夾
                target_dir = os.path.join(SAVE_INFER_PATH, image_final_label)
                # 建立資料夾
                os.makedirs(target_dir, exist_ok=True)
                
                # 決定檔案名稱
                safe_filename = os.path.basename(file.filename)
                if not safe_filename: safe_filename = "image.jpg"
                
                unique_filename = f"{int(time.time() * 1000)}_{safe_filename}"
                target_path = os.path.join(target_dir, unique_filename)
                
                # 複製檔案 (此時 tmp_path 必定存在)
                shutil.copyfile(tmp_path, target_path)
                
            except Exception as e:
                # 即使儲存失敗，API 也不該中斷
                print(f"[WARN] 無法儲存推論檔案至 {target_path}: {e}")
        
        # ========= [MOVED] 邏輯修正結束 =========

    except Exception as e:
        # 如果分析過程出錯 (例如圖檔毀損)
        print(f"[ERROR] 推論過程中發生錯誤: {e}")
        # image_final_label 和 dets 會維持預設值 ("none", [])

    finally:
        # 6. 'finally' 區塊最後執行，
        # 無論 try 成功或失敗，都會刪除暫存檔
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # 7. 準備回傳
    resp = {
        "image_final_label": image_final_label,
        "detections": dets,
        "meta": {
            "processing_ms": int((time.time() - t0) * 1000),
            "weights": WEIGHTS,
            "conf": conf,
            "use_gray_world": USE_GRAY_WORLD
        }
    }
    return jsonify(resp)

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    # 直接用 Flask 開：方便本機測試；正式用 gunicorn 由 Dockerfile 啟動
    app.run(host=host, port=port, debug=False)