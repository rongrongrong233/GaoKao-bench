#!/usr/bin/env python3
# 启动审阅页面本地服务器 - 跨平台版本
# Usage: python serve_review.py

import http.server
import os
import sys
import webbrowser
import threading
import time

PORT = 8765

class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()
    
    def handle(self):
        try:
            super().handle()
        except BrokenPipeError:
            # 忽略客户端断开连接的情况
            pass

def start_server():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = http.server.HTTPServer(('', PORT), Handler)
    print(f"启动本地服务器: http://localhost:{PORT}/review.html")
    print("按 Ctrl+C 停止")
    server.serve_forever()

def open_browser():
    time.sleep(1)
    url = f"http://localhost:{PORT}/review.html"
    try:
        webbrowser.open(url)
    except:
        print(f"请手动打开浏览器访问: {url}")

if __name__ == '__main__':
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    open_browser()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n服务器已停止")