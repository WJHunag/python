import os
import cv2
import orjson
import requests
import numpy as np
import time
import copy
import threading
from datetime import datetime
from bot import smoke_notify


regex = '''^(25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)\.( 
            25[0-5]|2[0-4][0-9]|[0-1]?[0-9][0-9]?)'''

"""
函式庫
"""
#========global========
def save_img(img, ip):
    folder_date = datetime.now().strftime("%Y-%m-%d")
    target_path=os.path.join(os.getcwd(),"image",ip)
    if not os.path.exists(target_path):
        os.makedirs(target_path)
    now = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    save_path = target_path + "/" + str(now) + ".jpg"
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    cv2.imwrite(save_path, img)

def call_model_api(frame0, roi, threshold, ip, notify_time, notify=True,roi_check=False):
    #start_det=time.time()
    url = "http://localhost:8501/v1/models/saved:predict"
    headers = {"content-type": "application/json"}
    im0_shape = frame0.shape
    frame = copy.deepcopy(frame0)
    yolov5 = True
    if yolov5:
        frame = letterbox(frame, [640,640], stride=32, auto=False)[0]
        frame = np.ascontiguousarray(frame)
        frame = frame.astype(np.float64)
        frame /= 255
        #images.append(frame)
        if len(frame.shape) == 3:
            frame = frame[None]  # expand for batch dimexit
        #data = json.dumps({"instances":frame.tolist()})
        data=str(orjson.dumps({"instances":frame.tolist()}), encoding = "utf-8")
    else:
        data=str(orjson.dumps({"instances":frame.tolist()}), encoding = "utf-8")
    #start_post=time.time()
    pred_response = requests.post(url, data=data, headers=headers)
    #print("pred",time.time()-start_post)
    #print(pred_response.text)
    #TODO 各模型回傳結果解包後要統一格式 為後續NMS等演算法好執行 先定回傳boxes, scores
    #===================依照模型回傳結果進行解包===================
    boxes, scores = yolov5_postprocess(pred_response, im0_shape)
    #============================================================

    #透過boxes classes scores將檢測結果畫到原影像中，不須複製原影像。
    #TODO 改為不更動原影像
    now = time.time()
    if (now-notify_time)>300 and notify:
        upload_path = visualize_result(frame0 ,boxes, scores, roi, threshold,ip,roi_check)
        if upload_path != None:
            #threading.Thread(target=smoke_notify, args=(upload_path, ip), daemon=True).start()
            notified_time = time.time()
            return notified_time
    #print(time.time()-start_det)
    return notify_time

def yolov5_postprocess(pred, im0_shape):
    boxes = []
    scores = []
    pred = orjson.loads(pred.text)["predictions"]
    pred = np.array(pred)

    pred = non_max_suppression(pred)
    pred[0][:,0], pred[0][:,1], pred[0][:,2], pred[0][:,3] = pred[0][:,0]*640, pred[0][:,1]*640, pred[0][:,2]*640, pred[0][:,3]*640

    for i,det in enumerate(pred):
        if len(det):
            det[:, :4] = scale_boxes((640, 640), det[:, :4], im0_shape).round()
            boxes.append([int(det[0][0]), int(det[0][1]), int(det[0][2]), int(det[0][3])])
            scores.append(det[0][4])
    return np.array(boxes), np.array(scores)

def visualize_result(image, boxes, scores, roi, threshold,ip,roi_check):
    #img_height, img_width, _ = image.shape
    saving = False
    
    for i in range(boxes.shape[0]):
        if scores is None or scores[i] > float(threshold):
            #print(f"第{i+1}個bbox 分數:{scores[i]*100}")
            xmin, ymin, xmax, ymax = boxes[i]
            (left, right, top, bottom) = (xmin, xmax, ymin, ymax)
            pos1 = (int(left), int(top))
            pos2 = (int(right), int(bottom))
            #cv2.rectangle(image, tuple(pos1), tuple(pos2), (0, 255, 0), 2)
            if(roi_check==True):
                for box in roi:
                        iou_score = calculate_iou(box, (left, top, right, bottom))
                        #print("iou分數",iou_score)
                        if iou_score > 0.0:
                            cv2.rectangle(image, tuple(pos1), tuple(pos2), (0, 255, 0), 2)
                            saving = True
            else:
                cv2.rectangle(image, tuple(pos1), tuple(pos2), (0, 255, 0), 2)
                saving = True

    if saving:
        #print("儲存")
        now = datetime.now().strftime("%Y-%d-%m-%H-%M-%S")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        folder_date = datetime.now().strftime("%Y-%m-%d")
        target_path=os.path.join(os.getcwd(),"result",folder_date)
        if not os.path.exists(target_path):
            os.makedirs(target_path)
        ip=str(ip)
        ip=ip.replace(".","_")
        save_path = os.path.join("result", folder_date, ip + "_" + str(now) + ".jpg")
        cv2.imwrite(save_path, image)
        return save_path
    else:
        return None

def rescale_bboxes(out_bbox, w,h):
    img_w, img_h = float(w),float(h)
    b = [out_bbox[0]*img_w,out_bbox[1]*img_w, out_bbox[2]*img_h, out_bbox[3]*img_h]
    return b

def calculate_iou(gt_rect, pt_rect):
    xmin1, ymin1, xmax1, ymax1 = gt_rect
    xmin2, ymin2, xmax2, ymax2 = pt_rect

    xmin1, ymin1, xmax1, ymax1 = int(xmin1), int(ymin1), int(xmax1), int(ymax1)
    xmin2, ymin2, xmax2, ymax2 = float(xmin2), float(ymin2), float(xmax2), float(ymax2)
    s1 = (xmax1 - xmin1) * (ymax1 - ymin1)
    s2 = (xmax2 - xmin2) * (ymax2 - ymin2)

    sum_area = s1 + s2

    left = max(xmin2, xmin1)
    right = min(xmax2, xmax1)
    top = max(ymin2, ymin1)
    bottom = min(ymax2, ymax1)

    if s2 >= s1:
        return 0

    if left >= right or top >= bottom:
        return 0

    intersection = (right - left) * (bottom - top)
    return intersection / (sum_area - intersection ) * 1.0

#========YOLOv5========

def letterbox(im, new_shape=(640, 640), color=(114, 114, 114), auto=True, scaleFill=False, scaleup=True, stride=32):
    # Resize and pad image while meeting stride-multiple constraints
    shape = im.shape[:2]  # current shape [height, width]
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)

    # Scale ratio (new / old)
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    if not scaleup:  # only scale down, do not scale up (for better val mAP)
        r = min(r, 1.0)

    # Compute padding
    ratio = r, r  # width, height ratios
    new_unpad = int(round(shape[1] * r)), int(round(shape[0] * r))
    dw, dh = new_shape[1] - new_unpad[0], new_shape[0] - new_unpad[1]  # wh padding
    if auto:  # minimum rectangle
        dw, dh = np.mod(dw, stride), np.mod(dh, stride)  # wh padding
    elif scaleFill:  # stretch
        dw, dh = 0.0, 0.0
        new_unpad = (new_shape[1], new_shape[0])
        ratio = new_shape[1] / shape[1], new_shape[0] / shape[0]  # width, height ratios

    dw /= 2  # divide padding into 2 sides
    dh /= 2

    if shape[::-1] != new_unpad:  # resize
        im = cv2.resize(im, new_unpad, interpolation=cv2.INTER_LINEAR)
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    im = cv2.copyMakeBorder(im, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)  # add border
    return im, ratio, (dw, dh)

def non_max_suppression(
        prediction,
        conf_thres=0.25,
        iou_thres=0.45,
        max_det=1000,
):
    """Non-Maximum Suppression (NMS) on inference results to reject overlapping detections
    Returns:
         list of detections, on (n,6) tensor per image [xyxy, conf, cls]
    """

    # Checks
    assert 0 <= conf_thres <= 1, f'Invalid Confidence threshold {conf_thres}, valid values are between 0.0 and 1.0'
    assert 0 <= iou_thres <= 1, f'Invalid IoU {iou_thres}, valid values are between 0.0 and 1.0'
    if isinstance(prediction, (list, tuple)):  # YOLOv5 model in validation model, output = (inference_out, loss_out)
        prediction = prediction[0]  # select only inference output

    #device = prediction.device
    #mps = 'mps' in device.type  # Apple MPS
    #if mps:  # MPS not fully supported yet, convert tensors to CPU before NMS
    #    prediction = prediction.cpu()
    bs = prediction.shape[0]  # batch size
    nc = prediction.shape[2] - 5  # number of classes
    xc = prediction[..., 4] > conf_thres  # candidates

    # Settings
    # min_wh = 2  # (pixels) minimum box width and height
    max_wh = 7680  # (pixels) maximum box width and height
    max_nms = 30000  # maximum number of boxes into torchvision.ops.nms()

    mi = 5 + nc  # mask start index
    output = [np.zeros((0, 6))] * bs
    for xi, x in enumerate(prediction):  # image index, image inference
        # Apply constraints
        # x[((x[..., 2:4] < min_wh) | (x[..., 2:4] > max_wh)).any(1), 4] = 0  # width-height
        x = x[xc[xi]]  # confidence

        # If none remain process next image
        if not x.shape[0]:
            continue

        # Compute conf
        x[:, 5:] *= x[:, 4:5]  # conf = obj_conf * cls_conf

        # Box/Mask
        box = xywh2xyxy(x[:, :4])  # center_x, center_y, width, height) to (x1, y1, x2, y2)
        mask = x[:, mi:]  # zero columns if no masks

        # Detections matrix nx6 (xyxy, conf, cls)
        # best class only
        conf = x[:, 5:mi].max(1, keepdims=True)
        j = np.zeros((conf.shape[0],1)).astype(np.float64)
        x = np.concatenate((box, conf, j, mask), 1)[conf.reshape(-1)>conf_thres]

        # Check shape
        n = x.shape[0]  # number of boxes
        if not n:  # no boxes
            continue
        x = x[np.argsort(x[:, 4])[::-1][:max_nms]]  # sort by confidence and remove excess boxes

        # Batched NMS
        c = x[:, 5:6] * max_wh  # classes
        boxes, scores = x[:, :4] + c, x[:, 4]  # boxes (offset by class), scores

        pick_boxes, pick_scores = nms(boxes, scores, iou_thres)  # NMS
        cls = np.zeros((pick_scores.shape[0],1))
        i = np.concatenate((pick_boxes, pick_scores, cls), 1)
        output[xi] = i

    return output

def xywh2xyxy(x):
    # Convert nx4 boxes from [x, y, w, h] to [x1, y1, x2, y2] where xy1=top-left, xy2=bottom-right
    y = np.copy(x)
    y[..., 0] = x[..., 0] - x[..., 2] / 2  # top left x
    y[..., 1] = x[..., 1] - x[..., 3] / 2  # top left y
    y[..., 2] = x[..., 0] + x[..., 2] / 2  # bottom right x
    y[..., 3] = x[..., 1] + x[..., 3] / 2  # bottom right y
    return y

def box_iou(box1, box2, eps=1e-7):
    """
    Return intersection-over-union (Jaccard index) of boxes.
    Both sets of boxes are expected to be in (x1, y1, x2, y2) format.
    Arguments:
        box1 (Tensor[N, 4])
        box2 (Tensor[M, 4])
    Returns:
        iou (Tensor[N, M]): the NxM matrix containing the pairwise
            IoU values for every element in boxes1 and boxes2
    """

    # inter(N,M) = (rb(N,M,2) - lt(N,M,2)).clamp(0).prod(2)
    (a1, a2), (b1, b2) = box1.unsqueeze(1).chunk(2, 2), box2.unsqueeze(0).chunk(2, 2)
    inter = (np.min(a2, b2) - np.max(a1, b1)).clamp(0).prod(2)

    # IoU = inter / (area1 + area2 - inter)
    return inter / ((a2 - a1).prod(2) + (b2 - b1).prod(2) - inter + eps)

def scale_boxes(img1_shape, boxes, img0_shape, ratio_pad=None):
    # Rescale boxes (xyxy) from img1_shape to img0_shape
    if ratio_pad is None:  # calculate from img0_shape
        gain = min(img1_shape[0] / img0_shape[0], img1_shape[1] / img0_shape[1])  # gain  = old / new
        pad = (img1_shape[1] - img0_shape[1] * gain) / 2, (img1_shape[0] - img0_shape[0] * gain) / 2  # wh padding
    else:
        gain = ratio_pad[0][0]
        pad = ratio_pad[1]

    boxes[..., [0, 2]] -= pad[0]  # x padding
    boxes[..., [1, 3]] -= pad[1]  # y padding
    boxes[..., :4] /= gain
    clip_boxes(boxes, img0_shape)
    return boxes

def clip_boxes(boxes, shape):
    # Clip boxes (xyxy) to image shape (height, width)
    boxes[..., [0, 2]] = boxes[..., [0, 2]].clip(0, shape[1])  # x1, x2
    boxes[..., [1, 3]] = boxes[..., [1, 3]].clip(0, shape[0])  # y1, y2

def nms(bounding_boxes, confidence_score, threshold):
    if len(bounding_boxes) == 0:
        return [], []

    boxes = np.array(bounding_boxes)

    start_x = boxes[:, 0]
    start_y = boxes[:, 1]
    end_x = boxes[:, 2]
    end_y = boxes[:, 3]

    score = np.array(confidence_score)

    picked_boxes = []
    picked_score = []

    areas = (end_x - start_x + 1) * (end_y - start_y + 1)

    order = np.argsort(score)

    while order.size > 0:
        index = order[-1]

        picked_boxes.append(bounding_boxes[index])
        picked_score.append([confidence_score[index]])

        x1 = np.maximum(start_x[index], start_x[order[:-1]])
        x2 = np.minimum(end_x[index], end_x[order[:-1]])
        y1 = np.maximum(start_y[index], start_y[order[:-1]])
        y2 = np.minimum(end_y[index], end_y[order[:-1]])

        w = np.maximum(0.0, x2 - x1 + 1)
        h = np.maximum(0.0, y2 - y1 + 1)
        intersection = w * h

        ratio = intersection / (areas[index] + areas[order[:-1]] - intersection)

        left = np.where(ratio < threshold)
        order = order[left]

    return np.array(picked_boxes), np.array(picked_score)


#==========local_info抓取==========

def get_local_info(path):
    local_info = {}
    with open(path, "r", encoding="utf-8") as file:
        lines = file.readlines()
        for line in lines:
            line = line.strip()
            if line:
                ip, address = line.split(":")
                local_info[ip] = address
    return local_info





