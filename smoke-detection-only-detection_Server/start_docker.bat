@echo off
REM 啟動容器
docker start mmyolo-deploy-2

REM 在前景執行 Python 腳本並顯示日誌
docker exec -it mmyolo-deploy /bin/bash -c "python mmyolo/multi_image_detection.py ./mmyolo/input ./mmyolo/configs/yolov5/yolov5_s-v61_fast_1xb12-1000e_smoke.py ./mmyolo/weights/smoke.pth --out-dir ./mmyolo/output"

REM 停止容器
docker stop mmyolo-deploy-2

echo Container stopped.
pause
