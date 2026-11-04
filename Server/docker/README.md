# A40 Docker 部署說明

這個資料夾用來把 `multi_image_detection.py` 包成 Docker worker。
它不需要對外開 port，主要靠掛載的 `input/` 資料夾接收圖片，再從
`output/` 暫存或上傳處理結果。

## 檔案說明

- `Dockerfile`：建立 stage1 detector image。
- `docker-compose.yml`：啟動一個 stage1 detector container。
- `.env.example`：部署參數範例。
- `entrypoint.sh`：container 啟動後實際執行 `multi_image_detection.py`。

## 部署前準備

如果要調整預設值，可以先複製環境變數範本：

```bash
cp docker/.env.example docker/.env
```

stage1 detector 需要跟既有的 YOLOv6 infer container 在同一個 Docker network。
實際伺服端目前的 network 名稱是 `smoke_detection`，建議在 `docker/.env`
設定：

```env
SMOKE_DOCKER_NETWORK=smoke_detection
YOLOV6_API_URL=http://yolov6_infer:8000/infer
```

如果 `smoke_detection` network 還不存在，先建立：

```bash
docker network create smoke_detection
```

如果既有的 `yolov6_infer` container 還沒加入這個 network：

```bash
docker network connect smoke_detection yolov6_infer
```

如果 stage1 detector container 已經啟動，也可以把它加入同一個 network：

```bash
docker network connect smoke_detection smoke_stage1_detector
docker restart smoke_stage1_detector
```

確認 network 內有哪些 container：

```bash
docker network inspect smoke_detection
```

## 啟動

在專案根目錄執行：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml up -d --build
```

如果不使用自訂 `.env`，也可以直接用 compose 預設值：

```bash
docker compose -f docker/docker-compose.yml up -d --build
```

## 常用操作

查看 log：

```bash
docker logs -f smoke_stage1_detector
```

重啟 worker：

```bash
docker restart smoke_stage1_detector
```

停止 worker：

```bash
docker compose --env-file docker/.env -f docker/docker-compose.yml down
```

## 掛載路徑

- `../input` -> `/root/workspace/input`
- `../output` -> `/root/workspace/output`
- `../smoke_config/distributed_config` -> `/root/workspace/smoke_config/distributed_config`
- `../weights` -> `/root/workspace/weights`，唯讀掛載，只有使用 Ultralytics backend 時才會用到。
