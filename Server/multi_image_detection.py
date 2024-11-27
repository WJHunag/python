import argparse
import os
import time
import cv2
import datetime
import pytz
import mmcv
import json
from mmcv.transforms import Compose
from mmdet.apis import inference_detector, init_detector
from mmyolo.registry import VISUALIZERS
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
import glob
import threading
import requests
from requests.auth import HTTPDigestAuth

class ImageHandler(FileSystemEventHandler):
    def __init__(self, args, model, test_pipeline, visualizer):
        self.args = args
        self.model = model
        self.test_pipeline = test_pipeline
        self.visualizer = visualizer
        self.cooling_down = False
        self.frames_by_camera = {}
        self.processed_images = {}
        self.max_boxes_by_camera = {}
        self.best_frame_by_camera = {}
        self.best_result_by_camera = {}
        self.initialize_existing_images(args.folder)

    def initialize_existing_images(self, folder):
        image_paths = glob.glob(os.path.join(folder, '*.jpg'))
        image_paths.extend(glob.glob(os.path.join(folder, '*.jpeg')))
        image_paths.extend(glob.glob(os.path.join(folder, '*.png')))
        
        for image_path in image_paths:
            self.handle_new_image(image_path)

    def get_json_data(key, json_path="./mmyolo/smoke_camera_config.json"):
        with open(json_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            return data[key]

    def adjust_camera_zoom_on_detection(self, ip, boxes):
        if len(boxes) > 0:
            acc = self.get_json_data('Cameras_account_password')[0]
            pwd = self.get_json_data('Cameras_account_password')[1]
            time_sleep = 30 

            # 直接解包 boxes 列表
            xmin, ymin, xmax, ymax = boxes
            #print(xmin, ymin, xmax, ymax)
            xmin, ymin, xmax, ymax = int(xmin), int(ymin), int(xmax), int(ymax)
            # 將座標進行縮放
            high_t, width_t = 7, 4
            xmin, ymin, xmax, ymax = xmin * width_t, ymin * high_t, xmax * width_t, ymax * high_t

            # 攝影機 鏡頭縮放功能
            url = f"http://{ip}/cgi-bin/ptzBase.cgi?action=moveDirectly&channel=1&startPoint[0]={xmin}&startPoint[1]={ymin}&endPoint[0]={xmax}&endPoint[1]={ymax}"
            response = requests.get(url, auth=HTTPDigestAuth(acc, pwd))
            time.sleep(time_sleep)

            # 攝影機 鏡頭回預置點 
            url = f"http://{ip}/cgi-bin/ptz.cgi?action=start&channel=1&code=GotoPreset&arg1=0&arg2=1&arg3=0"
            response = requests.get(url, auth=HTTPDigestAuth(acc, pwd))

            # 設置一個計時器來在1分鐘後重置冷卻狀態
            threading.Timer(60, self.reset_cooling_down).start()

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(('.png', '.jpg', '.jpeg')):
            # 假設檔名是 IP_timestamp.jpg，先切割出檔案名稱部分
            file_name = os.path.basename(event.src_path)
            # 用 '_' 分割並取前四部分組合為 IP 地址
            parts = file_name.split('_')
            if len(parts) >= 4:
                camera_ip = "_".join(parts[:4])  # 正確組合 IP 地址
                if camera_ip not in self.frames_by_camera:
                    self.frames_by_camera[camera_ip] = []
                    self.processed_images[camera_ip] = []
                    self.max_boxes_by_camera[camera_ip] = -1
                    self.best_frame_by_camera[camera_ip] = None
                    self.best_result_by_camera[camera_ip] = None
                
                self.frames_by_camera[camera_ip].append(event.src_path)
                if len(self.frames_by_camera[camera_ip]) >= 5:
                    self.process_detected_images_group(camera_ip)

    def on_moved(self, event):
        if not event.is_directory and event.dest_path.endswith(('.png', '.jpg', '.jpeg')):
            #print(f"File moved to: {event.dest_path}")  # 监控文件移动
            self.handle_new_image(event.dest_path)

    def handle_new_image(self, image_path):
        # 假設檔名是 IP_timestamp.jpg，先切割出檔案名稱部分
        file_name = os.path.basename(image_path)
        # 用 '_' 分割並取前四部分組合為 IP 地址
        parts = file_name.split('_')
        if len(parts) >= 4:
            camera_ip = "_".join(parts[:4])  # 正確組合 IP 地址
            if camera_ip not in self.frames_by_camera:
                self.frames_by_camera[camera_ip] = []
                self.processed_images[camera_ip] = []
                self.max_boxes_by_camera[camera_ip] = -1
                self.best_frame_by_camera[camera_ip] = None
                self.best_result_by_camera[camera_ip] = None
            
            self.frames_by_camera[camera_ip].append(image_path)
            if len(self.frames_by_camera[camera_ip]) >= self.args.group_size:
                self.process_detected_images_group(camera_ip)
            self.processed_images[camera_ip].append(image_path)  # 確保圖片被標記為已處理



    def max_box_detect(self, image_path, camera_ip, base_filename):
        # 初始化圖片鎖
        upload_lock = threading.Lock()
        # 追蹤已上傳的圖片
        uploaded_images = set()

        # 读取图片
        frame = mmcv.imread(image_path)
        result = inference_detector(self.model, frame, self.test_pipeline)

        # 提取 bounding boxes
        num_boxes = 0
        extracted_bboxes = []

        if hasattr(result, 'pred_instances'):
            valid_boxes = result.pred_instances['scores'] > self.args.score_thr
            num_boxes = valid_boxes.sum().item()
            bboxes = result.pred_instances['bboxes'][valid_boxes].cpu().numpy()
            extracted_bboxes = bboxes.tolist()

            # 裁剪并保存裁剪后的图像
            for idx, bbox in enumerate(extracted_bboxes):
                x1, y1, x2, y2 = map(int, bbox)
                cropped_image = frame[y1:y2, x1:x2]

                # 使用传递的标准化文件名生成裁剪图的文件名
                cropped_filename = f"{base_filename}_cropped_{idx + 1}.jpg"
                cropped_save_path = os.path.join(self.args.out_dir, cropped_filename)

                # 保存裁剪后的图片
                cv2.imwrite(cropped_save_path, cropped_image)

                # 上傳前檢查是否已經上傳過
                with upload_lock:
                    if cropped_save_path not in uploaded_images:
                        uploaded_images.add(cropped_save_path)  # 標記為已上傳
                        total_cropped = len(extracted_bboxes)  # 获取该组的总裁剪图数
                        threading.Thread(target=self.upload_cropped_image_to_workstation, 
                                        args=(cropped_save_path, camera_ip, total_cropped)).start()
                    else:
                        print(f"圖片 {cropped_save_path} 已經上傳，跳過重複上傳")

        # 如果检测到的 bounding boxes 数量超过之前的最大数量，更新最大值并存储最佳帧
        if num_boxes > self.max_boxes_by_camera[camera_ip]:
            self.max_boxes_by_camera[camera_ip] = num_boxes
            self.best_frame_by_camera[camera_ip] = frame
            self.best_result_by_camera[camera_ip] = result

        # 标记当前处理过的图像
        self.processed_images[camera_ip].append(image_path)



    def upload_cropped_image_to_workstation(self, cropped_image_path, camera_ip, total_cropped):
        # 硬编码工作站的 IP 地址
        url = "http://127.0.0.1:5001/receive_result"

        # 打开裁剪后的图片并上传
        try:
            with open(cropped_image_path, 'rb') as image_file:
                files = {'file': image_file}
                data = {'total_cropped': total_cropped}  # 传递该组的裁剪图总数
                response = requests.post(url, files=files, data=data)

                # 打印服务器响应
                if response.status_code == 200:
                    print(f"Successfully uploaded {cropped_image_path} from {camera_ip}")
                    # 確認上傳成功後，清理該圖片
                    if os.path.exists(cropped_image_path):
                        os.remove(cropped_image_path)
                        print(f"Deleted local cropped image {cropped_image_path}")
                else:
                    print(f"Failed to upload {cropped_image_path} from {camera_ip}: {response.text}")

        except Exception as e:
            print(f"Error uploading cropped image {cropped_image_path}: {str(e)}")

    def process_detected_images_group(self, camera_ip):
        # 确保每个摄像头有一个专用文件夹
        output_dir = os.path.join(self.args.out_dir, camera_ip)
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # 检查并移除不存在的图片
        existing_images = [img for img in self.frames_by_camera[camera_ip] if os.path.exists(img)]
        if len(existing_images) < self.args.group_size:
            print(f"Insufficient images for processing, waiting for more images. {len(existing_images)}/{self.args.group_size} available.")
            self.frames_by_camera[camera_ip] = existing_images  # 更新列表为现有的图片
            return  # 等待更多图片，不进行处理

        # 提前生成标准化的文件名
        tz = pytz.timezone('Asia/Taipei')
        timestamp = datetime.datetime.now(tz).strftime("%Y%m%d_%H%M%S")  # 使用日期和秒数
        formatted_ip = camera_ip.replace('.', '_')
        base_filename = f"{formatted_ip}_{timestamp}"

        # 调用 max_box_detect 并传递标准化的文件名
        for image_path in existing_images:
            self.max_box_detect(image_path, camera_ip, base_filename)  # 传递标准化的文件名

        if self.max_boxes_by_camera[camera_ip] > 0 and self.best_frame_by_camera[camera_ip] is not None:
            self.visualizer.add_datasample(
                name='image',
                image=self.best_frame_by_camera[camera_ip],
                data_sample=self.best_result_by_camera[camera_ip],
                draw_gt=False,
                show=False,
                pred_score_thr=self.args.score_thr)
            best_frame_image = self.visualizer.get_image()

            # 生成未裁剪图片的文件路径
            frame_filename = f"{base_filename}.jpg"
            frame_path = os.path.join(output_dir, frame_filename)

            # 保存未裁剪的图片
            cv2.imwrite(frame_path, best_frame_image)

            # 将未裁剪的结果上传到工作站
            try:
                with open(frame_path, 'rb') as f:
                    response = requests.post('http://127.0.0.1:5001/receive_result', files={'file': f})
                    if response.status_code == 200:
                        print(f"Result successfully uploaded to workstation for camera {camera_ip}")
                    else:
                        print(f"Failed to upload result to workstation for camera {camera_ip}: {response.text}")
            except Exception as e:
                print(f"Error uploading result to workstation for camera {camera_ip}: {str(e)}")

            print(f"Saved most boxes frame for camera {camera_ip} at {frame_path}")
        else:
            print(f"No valid images to save for camera {camera_ip}")

        # 删除已处理的图片
        for image_path in self.processed_images[camera_ip]:
            try:
                os.remove(image_path)
            except OSError as e:
                print(f"Error deleting {image_path}: {e.strerror}")

        # 重置相关状态
        self.frames_by_camera[camera_ip] = []
        self.processed_images[camera_ip] = []
        self.max_boxes_by_camera[camera_ip] = -1
        self.best_frame_by_camera[camera_ip] = None
        self.best_result_by_camera[camera_ip] = None


def parse_args():
    parser = argparse.ArgumentParser(description='MMYOLO image folder demo')
    parser.add_argument('folder', help='監測的資料集')
    parser.add_argument('config', help='Config檔案')
    parser.add_argument('checkpoint', help='權重檔案')
    parser.add_argument('--device', default='cuda:0', help='Device used for inference')
    parser.add_argument('--score-thr', type=float, default=0.6, help='Bbox score threshold')
    parser.add_argument('--out-dir', type=str, required=True, help='輸出資料夾')
    parser.add_argument('--group-size', type=int, default=5, help='一組幾張圖片')
    args = parser.parse_args()
    return args

def main():
    args = parse_args()
    # 确保输出目录存在
    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)
    # 确保输入目录存在，如果不存在则创建
    if not os.path.exists(args.folder):
        os.makedirs(args.folder)
        #print(f"Input folder '{args.folder}' created because it did not exist.")

    model = init_detector(args.config, args.checkpoint, device=args.device)
    model.cfg.test_dataloader.dataset.pipeline[0].type = 'mmdet.LoadImageFromNDArray'
    test_pipeline = Compose(model.cfg.test_dataloader.dataset.pipeline)
    visualizer = VISUALIZERS.build(model.cfg.visualizer)
    visualizer.dataset_meta = model.dataset_meta

    event_handler = ImageHandler(args, model, test_pipeline, visualizer)
    # 使用 PollingObserver 替代 Observer
    observer = PollingObserver()
    observer.schedule(event_handler, args.folder, recursive=True)
    print(f"Monitoring {args.folder}")
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == '__main__':
    main()
