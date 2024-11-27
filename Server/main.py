import os
import cv2
import mmcv
from glob import glob
from mmcv.transforms import Compose
from mmdet.apis import inference_detector, init_detector
from mmyolo.registry import VISUALIZERS

# 初始化檢測器
def init_detector_model(config, checkpoint, device='cuda:0'):
    model = init_detector(config, checkpoint, device=device)
    model.cfg.test_dataloader.dataset.pipeline[0].type = 'mmdet.LoadImageFromNDArray'
    return model

# 建立測試管道
def create_test_pipeline(model):
    return Compose(model.cfg.test_dataloader.dataset.pipeline)

# 初始化視覺化工具
def init_visualizer(model):
    visualizer = VISUALIZERS.build(model.cfg.visualizer)
    visualizer.dataset_meta = model.dataset_meta
    return visualizer


# 處理資料夾內的所有照片
def process_images(folder_path, model, visualizer, test_pipeline, score_thr=0.3, out_dir=''):
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    images = glob(os.path.join(folder_path, '*.jpg'))  # 假設照片格式為 JPG，可根據需要調整
    group_size = 10  # 每組的圖片數量
    group_index = 0

    # 分組處理圖片
    for i in range(0, len(images), group_size):
        group_images = images[i:i + group_size]
        max_boxes = -1
        best_image = None
        best_result = None

        for image_path in group_images:
            image = mmcv.imread(image_path)  # 使用 mmcv 讀取圖片
            result = inference_detector(model, image, test_pipeline=test_pipeline)
            num_boxes = count_valid_boxes(result, score_thr)

            if num_boxes > max_boxes:
                max_boxes = num_boxes
                best_image = image
                best_result = result

        if best_image is not None:
            save_image_with_most_boxes(best_image, best_result, visualizer, out_dir, group_index, score_thr, max_boxes)
            save_boxes_coordinates(best_image, best_result, out_dir, group_index, score_thr)
        group_index += 1

# 數算有效的方框
def count_valid_boxes(result, score_thr):
    if result.pred_instances['bboxes'].nelement() == 0:
        return 0
    valid_scores = result.pred_instances['scores'] >= score_thr
    return valid_scores.sum().item()

# 保存含最多方框的圖片
def save_image_with_most_boxes(image, result, visualizer, out_dir, group_index, score_thr, max_boxes):
    visualizer.add_datasample(name='image', image=image, data_sample=result, draw_gt=False, show=False, pred_score_thr=score_thr)
    best_image = visualizer.get_image()
    image_path = os.path.join(out_dir, f'best_image_group_{group_index:03d}.jpg')
    cv2.imwrite(image_path, best_image)
    print(f'Saved best image with {max_boxes} boxes from group {group_index} at {image_path}')

# 保存方框座標
def save_boxes_coordinates(image, result, out_dir, group_index, score_thr):
    boxes = result.pred_instances['bboxes'][result.pred_instances['scores'] >= score_thr]
    coordinates = boxes.cpu().numpy().tolist()
    coords_path = os.path.join(out_dir, f'coords_group_{group_index:03d}.txt')
    with open(coords_path, 'w') as f:
        for coord in coordinates:
            f.write(f'{coord}\n')
    print(f'Saved coordinates from group {group_index} at {coords_path}')

def main():
    # 設定文件路徑和檢查點文件路徑
    config_path = "./configs/yolov5/yolov5_s-v61_fast_1xb12-1000e_smoke.py"
    checkpoint_path = "./work_dirs/yolov5_s-v61_fast_1xb12-1000e_smoke/best_coco_msmoke_precision_epoch_970.pth"
    folder_path = './images'
    output_directory = "./output"
    device = 'cuda:0'
    score_threshold = 0.7

    # 初始化模型
    model = init_detector_model(config_path, checkpoint_path, device)
    
    # 建立測試管道
    test_pipeline = create_test_pipeline(model)
    
    # 初始化視覺化工具
    visualizer = init_visualizer(model)
    
    # 處理影片
    #process_video(video_path, model, visualizer, test_pipeline, score_thr=score_threshold, out_dir=output_directory)
    process_images(folder_path, model, visualizer, test_pipeline, score_thr=score_threshold, out_dir=output_directory)

if __name__ == '__main__':
    main()
