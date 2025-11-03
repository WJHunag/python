@echo off
chcp 65001 > nul

CALL activate smoke

REM 啟動 MongoDB
echo 正在啟動 MongoDB...
start /B mongod --dbpath ./MongoDB/data --config ./MongoDB/mongod.cfg

REM 確保 MongoDB 啟動成功後，等待 5 秒
timeout /t 5 > nul


REM 啟動 Flask 應用程式
echo 正在啟動 Flask 應用程式...
start /B python -m result_handler.receive_result_server

REM 啟動 Flask 應用程式 start_get_record_server
echo 正在啟動 get_record_server...
start /B python -m result_handler.NVR_get_record

echo 啟動完成，服務正在運行...
pause
