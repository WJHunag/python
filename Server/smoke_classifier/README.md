# smoke_classifier Docker deploy

## 部屬

以下將會引導你如何安裝部屬smoke_classifier docker。

### 安裝並架設Docker
建一個容器

```bash
export TAG=openmmlab/mmdeploy:ubuntu20.04-cuda11.8-mmdeploy
docker pull $TAG

sudo docker run --gpus=all –p {port}:{port} -it -v {path}/mmyolo/smoke_classifier/:/mmyolo/smoke_classifier/ --name smoke_classifier  $TAG
```
### 移動到專案內
先將路徑移動到smoke_classifier
```bash
cd /
cd {path}/mmyolo/smoke_classifier
```

### 安裝必要套件
```bash
pip install -r requirements.txt
```

### restful_api 需求檔案
```bashs
smoke_classifier
 └── configs
    └── end2end.onnx
        └── mobilenet_v2
            └── classification_onnxruntime_dynamic.py *
        └── classification_onnxruntime_dynamic.py *
 └── onnx
    └── end2end.onnx *
 └── tools
 └── test.png *
```
- [onnx 下載連結](https://drive.google.com/drive/folders/1tv8xbanfD9xwEU6yVYz84RhfJauMwLZf?usp=sharing)

#
### Run server
```bash
uvicorn restful_api:app --host 0.0.0.0 --port {port}
```


## 聯絡作者

你可以透過以下方式與我聯絡

- [mail](https://youtu.be/dQw4w9WgXcQ?si=0Zn783lq8yKOQlL1)
