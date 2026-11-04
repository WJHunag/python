# -*- coding: utf-8 -*-
import os
import sys
import argparse
import requests

IMG_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.webp')

def is_image(name: str) -> bool:
    return name.lower().endswith(IMG_EXTS)

def infer_one(image_path: str, url: str, conf: float = None, timeout: int = 180) -> str:
    """
    對單張圖片呼叫 /infer，回傳影像彙總標籤 image_final_label。
    若伺服器未提供 image_final_label，則以 detections 的 final_label 做保守彙整：
      任一 black_smoke > steam > none
    任何例外則回傳 'ERROR:<Type>'
    """
    files = {"file": open(image_path, "rb")}
    data = {}
    if conf is not None:
        data["conf"] = str(conf)

    try:
        resp = requests.post(url, files=files, data=data, timeout=timeout)
        resp.raise_for_status()
        js = resp.json()

        label = js.get("image_final_label")
        if label is None:
            dets = js.get("detections", []) or []
            labels = [(d.get("final_label") or "") for d in dets]
            if any(l == "black_smoke" for l in labels):
                label = "black_smoke"
            elif any(l == "steam" for l in labels):
                label = "steam"
            else:
                label = "none"
        return label
    except Exception as e:
        return f"ERROR:{e.__class__.__name__}"
    finally:
        try:
            files["file"].close()
        except Exception:
            pass

def collect_images(path: str, recursive: bool = False):
    if os.path.isdir(path):
        if recursive:
            for root, _, names in os.walk(path):
                for n in sorted(names):
                    if is_image(n):
                        yield os.path.join(root, n)
        else:
            for n in sorted(os.listdir(path)):
                if is_image(n):
                    yield os.path.join(path, n)
    else:
        if is_image(path):
            yield path

def main():
    parser = argparse.ArgumentParser(
        description="批次上傳圖片到亮度比例分類器 API，只輸出 final_label，並彙整成文字檔。"
    )
    parser.add_argument("path", type=str, help="圖片檔或資料夾路徑")
    parser.add_argument(
        "--url",
        type=str,
        default="http://127.0.0.1:8011/infer",
        help="伺服器 /infer URL（預設：http://127.0.0.1:8011/infer）",
    )
    parser.add_argument("--conf", type=float, default=None, help="偵測門檻（可選）")
    parser.add_argument("-o", "--out", type=str, default="infer_results.txt", help="輸出文字檔路徑")
    parser.add_argument("-R", "--recursive", action="store_true", help="資料夾遞迴掃描")
    parser.add_argument("--timeout", type=int, default=180, help="請求逾時（秒）")

    args = parser.parse_args()

    images = list(collect_images(args.path, recursive=args.recursive))
    if not images:
        print("找不到可處理的圖片。", file=sys.stderr)
        return 1

    results = []
    for p in images:
        label = infer_one(p, args.url, conf=args.conf, timeout=args.timeout)
        fname = os.path.basename(p)
        # 依你需求：只在終端輸出「檔名 final_label」
        print(f"{fname} {label}")
        results.append((fname, label))

    # 彙整成文字檔（每行：檔名,final_label）
    try:
        with open(args.out, "w", encoding="utf-8") as f:
            for fname, label in results:
                f.write(f"{fname},{label}\n")
    except Exception as e:
        print(f"寫入輸出檔失敗：{e}", file=sys.stderr)
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
