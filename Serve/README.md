# SMOKE A40 Server

A40 是煙霧偵測系統的中央端。它負責：

- 接收工作站上傳的影像。
- 監控 input 資料夾並執行一階偵測。
- 把裁切結果與標註完整圖送回對應工作站。
- 把預覽圖推送到中央 UI。
- 管理 distributed_config 並派發到工作站。

## 偵測後端

第一階段重構後只保留兩種後端：

- `yolov6_api`：預設後端，呼叫 YOLOv6 inference API。
- `ultralytics`：本機模型後端，使用 Ultralytics 套件載入權重。

舊版大型偵測框架已不再是 A40 中央端依賴。

## 安裝

建議 Python 版本：`3.10`。

```bash
pip install -r requirements.txt
```

如果使用 GPU 本機 Ultralytics，請依照部署機器的 CUDA 版本安裝對應 PyTorch。

## 主要服務

### Routing Server

負責接收工作站影像、狀態回報、座標校正回傳。

```bash
python -m smoke_config.routing_server
```

重要端點：

- `POST /upload`：工作站上傳原始影像。
- `POST /polling_report`：工作站回報目前輪巡攝影機。
- `POST /disconnection_report`：工作站回報斷線攝影機。
- `POST /coordinate/geo_reference`：座標校正工具回傳地理參考點。

### Detection Worker

預設使用 YOLOv6 API：

```bash
python multi_image_detection.py ./input --out-dir ./output --distributed-config-dir ./smoke_config/distributed_config --backend yolov6_api --api-url http://yolov6:8000/infer
```

夜間偵測門檻預設為 `0.9`，可用參數調整：

```bash
python multi_image_detection.py ./input --out-dir ./output --distributed-config-dir ./smoke_config/distributed_config --backend yolov6_api --api-url http://yolov6:8000/infer --night-score-thr 0.5
```

使用 Ultralytics 本機模型：

```bash
python multi_image_detection.py ./input --out-dir ./output --distributed-config-dir ./smoke_config/distributed_config --backend ultralytics --ultralytics-weights ./weights/ultralytics/best.pt
```

Docker Compose 執行：

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

### Dashboard V2

```bash
python frontend/dashboard_v2/smoke_UI_server.py
```

預設 UI 服務位置由 `SMOKE_UI_HOST`、`SMOKE_UI_PORT` 控制。

## 常用環境變數

- `DETECTION_BACKEND`：`yolov6_api` 或 `ultralytics`。
- `YOLOV6_API_URL`：YOLOv6 API URL，預設 `http://yolov6:8000/infer`。
- `NIGHT_SCORE_THR`：夜間偵測門檻，預設 `0.9`。
- `ULTRALYTICS_WEIGHTS`：Ultralytics 權重路徑。
- `ULTRALYTICS_IMGSZ`：Ultralytics 推論尺寸，預設 `640`。
- `UI_SERVER_UPDATE_URL`：中央 UI 預覽圖更新端點。
- `DISTRIBUTED_CONFIG_DIR`：distributed_config 路徑。
- `WORKSTATION_DEFAULT_PORT`：工作站 receive_result 預設 port，預設 `5001`。

## 與工作站的契約

這次重構不改工作站對接格式：

- 工作站仍把影像送到 A40 `/upload`。
- A40 仍依 metadata / 檔名解析 camera IP。
- A40 仍先送裁切圖，再送標註完整圖到工作站 `/receive_result`。
- A40 仍把 `group_id`、bbox、crop origin 等資訊寫入影像 metadata。
