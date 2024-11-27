from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval

# 初始化COCO ground truth api
annFile = 'D:/university_project/mmyolo/data/smoke-val/annotations/result.json' # 變更為您的標註文件路徑
cocoGt = COCO(annFile)

# 初始化COCO detections api
resFile = "D:/university_project/mmyolo/work_dirs/demo/json_demo.bbox.json" # 變更為您的偵測結果檔案路徑
cocoDt = cocoGt.loadRes(resFile)

# 初始化COCOeval api
cocoEval = COCOeval(cocoGt, cocoDt, 'bbox')

# 評估每一個類別
cocoEval.params.useCats = 1

# 評估AR
cocoEval.evaluate()
cocoEval.accumulate()
cocoEval.summarize()

# 輸出每個類別的AR
for catId in cocoGt.getCatIds():
     # 每個類別的名字
     catName = cocoGt.loadCats(catId)[0]['name']
    
     # 設定要評估的類別ID
     cocoEval.params.catIds = [catId]
    
     # 重新累積評估結果
     cocoEval.accumulate()
    
     # 提取此類別的平均召回率
     # 可以根據需要修改iouThrs, areaRng, maxDets等參數
     recall = cocoEval.eval['recall'][..., -1, 0, 2] # 提取所有IoU閾值和所有面積範圍的最大檢測數量
     recall = recall[recall > -1].mean() if recall[recall > -1].size > 0 else 'no detections'
    
     print(f'Recall for {catName}: {recall}')
