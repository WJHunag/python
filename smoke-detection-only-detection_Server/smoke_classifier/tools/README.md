# smoke_classifier Train

## 訓練

以下將會引導你如何安裝MMpretrain 和 MMdeploy 用於訓練 轉檔。

### 建立虛擬環境

```bash
conda create --name smoke_classifier python=3.8 -y
conda activate smoke_classifier
```
### 安裝必要套件

- [MMpretarin 安裝文本](https://mmpretrain.readthedocs.io/en/latest/get_started.html)
- [MMdeploy 安裝文本](https://github.com/open-mmlab/mmdeploy/blob/main/docs/en/get_started.md)

### 資料集
```bashs
smoke_classifier
 └── tools
    └── data
        └── smoke_data
            ├── train
            │   ├── smoke
            │   └── water_smoke
            └── val
                ├── smoke
                └── water_smoke
```

### 訓練
```bash
python train.py ./configs/mobilenet_v2/mobilenet-v2_8xb32_in1k.py 
```

### pth轉onnx

```bash
python deploy.py \ 
./configs/mmpretrain/classification_onnxruntime_static.py\    
./configs/mobilenet_v2/mobilenet-v2_8xb32_in1k.py  \    
path/'{test.pth}' \    
path/'{images.jpg}'  \    
--work-dir ./work_dir \  
```

## 聯絡作者

你可以透過以下方式與我聯絡

- [mail](https://youtu.be/dQw4w9WgXcQ?si=0Zn783lq8yKOQlL1)
