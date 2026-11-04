# 攝影機參考座標距離門檻

每支攝影機可在 `distributed_config/camera_configs/camera_channel_XXX.json`
的攝影機最外層設定 `reference_distance_threshold_px`，單位是原圖像素。

例如在既有攝影機設定加入：

```json
"reference_distance_threshold_px": 200
```

此數值須為大於 0 的有限數值。未設定或設定為 `null` 時，工作站依當次
圖片寬度自動計算 1/3，例如 1920px → 640px、1280px → 約 426.67px。
無效的手動設定會在工作站記錄警告並改用此預設值。

網頁「攝影機設定」的「參考座標距離門檻」欄位可以修改這個設定：
開啟編輯、輸入像素值、保存；清空欄位再保存可恢復自動值。
API 使用既有 `POST /config/send_setting`，輸入欄位放在
`function_switches.reference_distance_threshold_px`，保存時轉為攝影機最外層欄位，
透過既有 distributed_config 派發流程傳到工作站。沒有傳入欄位的部分更新保留既有值。

此門檻與 `fixed_area_radius_px`（命中半徑）分開。
命中固定點照常提供座標；未命中且到固定點圓心的距離不超過門檻時，
才提供最近固定點的參考座標及名稱／十字標記。煙霧編號不受影響。
測試群詳細訊息顯示門檻來源；公開群精簡格式共用相同判定。

修改 JSON 後需透過既有設定派發或重新載入流程使執行中的工作站取得新值。
API 已保存或派發已排隊，並不代表所有現場工作站已套用。
