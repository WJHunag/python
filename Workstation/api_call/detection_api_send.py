import argparse
import requests
import base64
from PIL import Image
from io import BytesIO
import os
import sys

def upload_image_and_get_result(image_path, api_url, output_dir):
    os.makedirs(output_dir, exist_ok=True)

    with open(image_path, 'rb') as image_file:
        files = {'image': image_file}
        print(f"Uploading {image_path} to {api_url}...")
        response = requests.post(api_url, files=files)
    
    if response.status_code != 200:
        print(f"Error: Failed to get a valid response from the API. Status code: {response.status_code}")
        print(f"Response content: {response.text}")
        return

    response_data = response.json()
    if "result_image_base64" in response_data:
        result_image_base64 = response_data["result_image_base64"]
        img_data = base64.b64decode(result_image_base64)
        image = Image.open(BytesIO(img_data))
        
        result_path = os.path.join(output_dir, f"result_{os.path.basename(image_path)}")
        image.save(result_path)
        print(f"Result image saved to: {result_path}")
    else:
        print(f"Error in response: {response_data.get('error', 'Unknown error')}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload image to API and process result")
    parser.add_argument("image_path", help="Path to the image file")
    parser.add_argument("--api_url", default="http://127.0.0.1:5006/detect", help="API URL (default: http://127.0.0.1:5006/detect)")
    parser.add_argument("--output_dir", default="./results", help="Directory to save the result (default: ./results)")
    args = parser.parse_args()

    if not os.path.exists(args.image_path):
        print("錯誤：影像檔案不存在，請檢查路徑。")
        sys.exit(1)

    upload_image_and_get_result(args.image_path, args.api_url, args.output_dir)
