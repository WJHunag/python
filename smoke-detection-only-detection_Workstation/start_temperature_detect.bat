CALL activate smoke
python -m temperature_detection.temperature_detection --threshold 73 --roi_position left_top --location video_test --source rtsp://192.168.0.1:8554/live/mystream 
PAUSE