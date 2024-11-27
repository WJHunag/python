from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel
import mmengine
import numpy as np
import torch
import base64
from PIL import Image
import io
from mmdeploy.utils import get_input_shape, load_config
from mmdeploy.apis.utils import build_task_processor
from mmdet.visualization import DetLocalVisualizer
import cv2
import time

visualizer = DetLocalVisualizer()

# FastAPI
app = FastAPI()

# 初始化
model_cfg = 'configs/mmpretrain/mobilenet_v2/mobilenet-v2_8xb32_in1k.py'    
deploy_cfg = 'configs/mmpretrain/classification_onnxruntime_dynamic.py'
img = './test.png'
backend_files = ['onnx/end2end.onnx']
device = 'cuda:0'

# 加載配置並建構任務處理器
deploy_cfg, model_cfg = load_config(deploy_cfg, model_cfg)
task_processor = build_task_processor(model_cfg, deploy_cfg, device)
model = task_processor.build_backend_model(backend_files, task_processor.update_data_preprocessor)

def serve_for_infer(img, model):
    """推理函数，接收圖片並返回推理结果"""
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)  
    input_shape = get_input_shape(deploy_cfg)
    model_inputs, _ = task_processor.create_input(img, input_shape)

    with torch.no_grad():
        result = model.test_step(model_inputs)


    return result

@app.post("/infer")
async def infer_image(file: UploadFile = File(...)):
    """接收上傳圖片並傳回推理結果"""
    try:

        # 讀取圖片
        contents = await file.read()
        img = np.array(Image.open(io.BytesIO(contents)))

        result = serve_for_infer(img, model)

        str_value = str(result[0].pred_label.item())
        # 返回推理結果
        return {"result": str_value}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == '__main__':

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)