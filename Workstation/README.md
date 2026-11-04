# SMOKE MMYOLO REWORK README

## 系統架構圖

<div align="center">
  <img width="1000" height="300" src="images/framework.png"/>
  <p>煙霧檢測系統架構圖</p>
</div>

## 安裝


以下將會引導你如何安裝此專案到你的電腦上。

python 版本建議為：`3.8.0` 

### 取得專案

```bash
git clone https://gitlab.com/prophet.ai.inc/smoke-detection-only-detection.git
```

### 移動到專案內

```bash
cd smoke-detection-only-detection
```

#

### 創建虛擬環境與進入環境

```bash
conda create -n smoke python==3.10
conda activate smoke
```

### 安裝套件

```bash
pip install -r requirement.txt
```

#

### 下載MongoDB

[MongoDB官網下載連結](https://www.mongodb.com/try/download/community)

<div align="center">
  <img width="500" height="400" src="images/mongo_download.png"/>
  <p>點擊紅框處下載即可</p>
</div>


### MongoDB環境變數設定

>Step 1 在windows 開始的搜尋視窗搜尋 '編輯環境系統變數'，進入(圖1)視窗

>Step 2 再(圖1)中，點環境變數(N)，會跳出視窗(如圖2)

>Step 3 再(圖2)中，點選最上白框變數中的Path，接著點選編輯(E)會進入(圖3)的視窗。

>Step 4 再(圖3)中，點選新增 (N)，加入MongoDB當初的安裝位置。位置形式可以參考(圖3)中環境變數的第一列。


<div align="center">
  <img width="400" height="400" src="images/MongoDB_EnvironmentVariable_Setup1.png"/>
  <p>(圖1)</p>
  <img width="400" height="400" src="images/MongoDB_EnvironmentVariable_Setup2.png"/>
  <p>(圖2)</p>
  <img width="400" height="400" src="images/MongoDB_EnvironmentVariable_Setup3.png"/>
  <p>(圖3)</p>
</div>

## 運行專案

>Step1. 下載Mongo配置包及配置包放置處

[MongoDB配置包](https://drive.google.com/file/d/1Ukp0q9xOb4HpaqYtfaHIHpqCowsjq7fz/view?usp=sharing)

配置包放置於專案內最外層
```bashs
smoke-detection-only-detection
  ├─MongoDB
  │  ├─data
  │  └─log
  │   mongod.cfg
  │   start_mongodb.bat
  ├─main.py
  ├─utility.py
  └─page.py
```

>Step2. 執行初始化程序

##### 回到源資料夾執行start_initialization.bat

#### 鍵入指令

```bash
cd ..
start_initialization.bat
```

<div align="center">
  <img width="800" height="500" src="images/mongo_demo_1.png"/>
  <p>有看到這些訊息代表啟動成功，請別關閉它</p>
</div>

>Step3. 給定攝影機ip檔案

#### 此步驟請與相關負責人員提供檔案，此處不公開內容
#### 若取得到攝影機ip資訊檔案，須根據以下位置擺放
```bashs
smoke-detection-only-detection
  ├─MongoDB
  ├─start_hub_center.bat
  ├─start_initialization.bat
  ├─start_UI.bat
  ├─utility.py
  └─smoke_local_camera_config.json
```



>Step4. 執行攝影機設定程式

#### 直接運行bat檔

```bash
start_UI.bat
```

## 檔案說明

- start_initialization.bat - 初始化程式啟動
- start_hub_center.bat - 多支攝影機檢測啟動
- start_UI.bat - 攝影機設定程式啟動
- start_temperature_detect.bat - 熱顯檢測啟動
- start_result_show.bat - 檢視當前攝影機最新檢測圖
- utility.py - 程式主功能


## 攝影機設定運行畫面展示

<div align="center">
  <img width="800" height="500" src="images/UI_demo_1.png"/>
  <p>執行畫面</p>
  <img width="800" height="500" src="images/UI_demo_2.png"/>
  <p>讀取ip後</p>
  <img width="800" height="500" src="images/UI_demo_3.png"/>
  <p>登出畫面</p>
</div>

>Step5. 執行多支攝影機檢測

#### 直接運行bat檔

```bash
start_hub_center.bat
```

#### 或者從使用介面進行開啟

<div align="center">
  <img width="800" height="500" src="images/UI_demo_4.png"/>
  <p>按下紅色框中的按鈕也可以直接執行多支攝影機檢測程式</p>
</div>

## 運行畫面展示

<div align="center">
  <img width="800" height="500" src="images/UI_demo_5.png"/>
  <p>它會根據設定檔中的所輸入的攝影機IP來顯示</p>
</div>

## 聯絡作者

你可以透過以下方式與我聯絡

- [mail](https://youtu.be/dQw4w9WgXcQ?si=0Zn783lq8yKOQlL1)
