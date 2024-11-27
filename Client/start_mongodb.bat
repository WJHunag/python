@echo off & setlocal
chcp 65001 >nul

REM 停止所有運行中的 MongoDB 進程
tasklist /fi "imagename eq mongod.exe" | find /i "mongod.exe" >nul
if errorlevel 1 (
    echo "沒有其他 MongoDB 運行中"
    echo "啟動煙霧程式的MongoDB，無其餘文字產生即正常執行，請勿關閉"
) else (
    taskkill /f /im mongod.exe
    echo "正在執行的MongoDB 已停止"
    echo "啟動煙霧程式的MongoDB，無其餘文字產生即正常執行，請勿關閉"
)

REM 啟動新的 MongoDB 伺服器
mongod --dbpath ./MongoDB/data --config ./MongoDB/mongod.cfg

PAUSE