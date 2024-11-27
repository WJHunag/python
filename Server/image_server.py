from flask import Flask, request, jsonify
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
UPLOAD_FOLDER = './input'  # 确保这个目录存在
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if file:
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        
        # 確保上傳目錄存在
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        
        file.save(save_path)
        return jsonify({'message': 'File uploaded successfully', 'path': save_path}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
