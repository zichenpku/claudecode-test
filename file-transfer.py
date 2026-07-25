#!/usr/bin/env python3
"""
📁 文件传输助手 - File Transfer Assistant
在 Windows 上运行，Mac/手机/任何设备的浏览器打开即可传输文件。

用法:
  python file-transfer.py                    # 启动服务 (端口 8080)
  python file-transfer.py --port 9090        # 指定端口
  python file-transfer.py --ngrok            # 启用 ngrok 远程访问
  python file-transfer.py --share C:\share   # 指定共享目录
"""

import os
import sys
import json
import io
import mimetypes
import socket
import threading
import webbrowser
import argparse
import html
import urllib.parse
import base64
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

# ============================================================
#  配置
# ============================================================
VERSION = "1.1"
PORT = 8080
SHARE_DIR = os.path.expanduser("~/file-transfer-share")  # 默认共享目录
USE_NGROK = False
MAX_FILE_SIZE = 10 * 1024 * 1024 * 1024  # 10GB 限制（实际上受系统限制）

# ============================================================
#  工具函数
# ============================================================
def ensure_dir(path):
    """确保目录存在"""
    if not os.path.exists(path):
        os.makedirs(path)
    return path

def format_size(size):
    """人性化文件大小显示"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"

def get_local_ip():
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def get_file_mtime(path):
    """获取文件修改时间"""
    try:
        ts = os.path.getmtime(path)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except:
        return "未知"

def is_binary(path):
    """简单判断是否为二进制文件"""
    try:
        with open(path, 'rb') as f:
            chunk = f.read(8192)
            return b'\0' in chunk
    except:
        return True

# ============================================================
#  HTTP 服务器
# ============================================================
class FileTransferHandler(BaseHTTPRequestHandler):
    """处理文件传输的 HTTP 请求"""

    def log_message(self, format, *args):
        """彩色日志"""
        msg = format % args
        icon = {
            'GET': '📥',
            'POST': '📤',
            'DELETE': '🗑️',
        }.get(args[0], '🔗')
        print(f"  {icon} {msg}")

    def send_json(self, data, status=200):
        """发送 JSON 响应"""
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        """发送 JSON 错误"""
        self.send_json({'error': message}, status)

    def get_file_list(self):
        """获取文件列表"""
        files = []
        try:
            for name in os.listdir(SHARE_DIR):
                path = os.path.join(SHARE_DIR, name)
                if os.path.isfile(path):
                    stat = os.stat(path)
                    files.append({
                        'name': name,
                        'size': stat.st_size,
                        'size_display': format_size(stat.st_size),
                        'modified': get_file_mtime(path),
                        'is_binary': is_binary(path),
                    })
            # 按修改时间倒序排列
            files.sort(key=lambda f: f['modified'], reverse=True)
        except Exception as e:
            print(f"  ⚠️ 读取文件列表失败: {e}")
        return {
            'files': files,
            'count': len(files),
            'share_dir': SHARE_DIR,
        }

    def get_client_address(self):
        """获取客户端地址"""
        return self.client_address[0]

    # ---- HTTP 方法 ----

    def do_OPTIONS(self):
        """CORS 预检请求"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-File-Name')
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # API 路由
        if path == '/':
            self.send_html_page()
        elif path == '/api/files':
            self.send_json(self.get_file_list())
        elif path.startswith('/download/'):
            filename = urllib.parse.unquote(path[10:])
            self.serve_file(filename)
        elif path == '/api/info':
            self.send_json({
                'version': VERSION,
                'port': PORT,
                'platform': sys.platform,
                'share_dir': SHARE_DIR,
                'client_ip': self.get_client_address(),
            })
        else:
            self.send_error(404, 'Not Found')

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == '/api/upload':
            self.handle_upload()
        elif path == '/api/upload-binary':
            self.handle_upload_binary()
        else:
            self.send_error_json('未知 API', 404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path.startswith('/api/delete/'):
            filename = urllib.parse.unquote(path[12:])
            self.handle_delete(filename)
        else:
            self.send_error_json('未知 API', 404)

    # ---- 核心功能 ----

    def send_html_page(self):
        """发送网页界面"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode('utf-8'))

    def serve_file(self, filename):
        """下载文件"""
        # 安全检查：防止路径穿越
        safe_name = os.path.basename(filename)
        filepath = os.path.join(SHARE_DIR, safe_name)

        if not os.path.exists(filepath) or not os.path.isfile(filepath):
            self.send_error_json('文件不存在', 404)
            return

        file_size = os.path.getsize(filepath)
        mime_type, _ = mimetypes.guess_type(safe_name)
        if mime_type is None:
            mime_type = 'application/octet-stream'

        # 处理断点续传/分块下载 (Range header)
        range_header = self.headers.get('Range', '')

        self.send_response(206 if range_header else 200)
        self.send_header('Content-Type', mime_type)
        self.send_header('Content-Disposition', f'attachment; filename="{urllib.parse.quote(safe_name)}"')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Access-Control-Allow-Origin', '*')

        start = 0
        end = file_size - 1

        if range_header:
            try:
                range_val = range_header.strip().replace('bytes=', '')
                parts = range_val.split('-')
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
            except:
                start = 0
                end = file_size - 1

            content_length = end - start + 1
            self.send_header('Content-Length', str(content_length))
            self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            self.end_headers()

            with open(filepath, 'rb') as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        else:
            content_length = file_size
            self.send_header('Content-Length', str(content_length))
            self.end_headers()

            with open(filepath, 'rb') as f:
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)

    def handle_upload(self):
        """处理文件上传 (multipart/form-data)"""
        content_type = self.headers.get('Content-Type', '')
        content_length = int(self.headers.get('Content-Length', 0))

        if content_length > MAX_FILE_SIZE:
            self.send_error_json('文件超过大小限制', 413)
            return

        # 解析 multipart 数据
        boundary = None
        if 'boundary=' in content_type:
            boundary = content_type.split('boundary=')[1].strip()
            if boundary.startswith('"') and boundary.endswith('"'):
                boundary = boundary[1:-1]

        if not boundary:
            self.send_error_json('无效的 Content-Type', 400)
            return

        boundary_bytes = f'--{boundary}'.encode('utf-8')
        end_boundary_bytes = f'--{boundary}--'.encode('utf-8')

        # 读取原始数据
        raw_data = self.rfile.read(content_length)

        # 按 boundary 分割
        parts = raw_data.split(boundary_bytes)
        uploaded_files = []

        for part in parts:
            if part in (b'', b'\r\n', b'--\r\n', b'--'):
                continue
            if part.startswith(b'--'):
                continue

            # 找到头部和文件数据的分隔
            header_end = part.find(b'\r\n\r\n')
            if header_end == -1:
                continue

            header_bytes = part[:header_end]
            file_data = part[header_end + 4:]  # 跳过 \r\n\r\n

            # 去掉尾部的 \r\n--
            if file_data.endswith(b'\r\n'):
                file_data = file_data[:-2]
            if file_data.endswith(b'--'):
                file_data = file_data[:-2]
            if file_data.endswith(b'\r\n'):
                file_data = file_data[:-2]

            # 解析头部，获取文件名
            header_text = header_bytes.decode('utf-8', errors='replace')
            filename = None
            for line in header_text.split('\r\n'):
                if 'filename="' in line:
                    # 处理 filename 和 filename* (RFC 5987)
                    if 'filename*=' in line:
                        # Encoding: Language: Value
                        # e.g., filename*=UTF-8''%E4%B8%AD%E6%96%87.txt
                        enc_part = line.split("filename*=")[1].strip()
                        if "'" in enc_part:
                            parts_enc = enc_part.split("'", 2)
                            if len(parts_enc) == 3:
                                filename = urllib.parse.unquote(parts_enc[2])
                    if not filename:
                        start = line.find('filename="')
                        if start != -1:
                            start += len('filename="')
                            end = line.find('"', start)
                            if end != -1:
                                filename = line[start:end]
                    break

            if not filename:
                continue

            # 安全化文件名
            safe_name = os.path.basename(filename)
            filepath = os.path.join(SHARE_DIR, safe_name)

            # 处理文件名冲突
            base, ext = os.path.splitext(safe_name)
            counter = 1
            while os.path.exists(filepath):
                safe_name = f"{base} ({counter}){ext}"
                filepath = os.path.join(SHARE_DIR, safe_name)
                counter += 1

            with open(filepath, 'wb') as f:
                f.write(file_data)

            uploaded_files.append({
                'name': safe_name,
                'size': len(file_data),
                'size_display': format_size(len(file_data)),
            })

        if uploaded_files:
            names = ', '.join(f['name'] for f in uploaded_files)
            print(f"  ✅ 上传成功: {names}")
            self.send_json({
                'success': True,
                'files': uploaded_files,
                'message': f'成功上传 {len(uploaded_files)} 个文件',
            })
        else:
            self.send_error_json('未找到文件数据', 400)

    def handle_upload_binary(self):
        """处理二进制文件上传 (直接用请求体)"""
        filename = self.headers.get('X-File-Name', '')
        content_length = int(self.headers.get('Content-Length', 0))

        if not filename:
            self.send_error_json('缺少文件名 (X-File-Name)', 400)
            return

        if content_length > MAX_FILE_SIZE:
            self.send_error_json('文件超过大小限制', 413)
            return

        safe_name = os.path.basename(urllib.parse.unquote(filename))
        filepath = os.path.join(SHARE_DIR, safe_name)

        # 处理文件名冲突
        base, ext = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(filepath):
            safe_name = f"{base} ({counter}){ext}"
            filepath = os.path.join(SHARE_DIR, safe_name)
            counter += 1

        with open(filepath, 'wb') as f:
            remaining = content_length
            while remaining > 0:
                chunk_size = min(65536, remaining)
                data = self.rfile.read(chunk_size)
                if not data:
                    break
                f.write(data)
                remaining -= len(data)

        print(f"  ✅ 上传成功: {safe_name} ({format_size(content_length)})")
        self.send_json({
            'success': True,
            'file': {
                'name': safe_name,
                'size': content_length,
                'size_display': format_size(content_length),
            },
            'message': f'上传成功: {safe_name}',
        })

    def handle_delete(self, filename):
        """删除文件"""
        safe_name = os.path.basename(filename)
        filepath = os.path.join(SHARE_DIR, safe_name)

        if not os.path.exists(filepath):
            self.send_error_json('文件不存在', 404)
            return

        try:
            os.remove(filepath)
            print(f"  🗑️ 已删除: {safe_name}")
            self.send_json({'success': True, 'message': f'已删除: {safe_name}'})
        except Exception as e:
            self.send_error_json(f'删除失败: {str(e)}', 500)


# ============================================================
#  HTML 页面 (精美的网页界面)
# ============================================================
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📁 文件传输助手</title>
<style>
  :root {
    --bg: #0f0f1a;
    --surface: #1a1a2e;
    --surface2: #252540;
    --border: #3a3a5c;
    --text: #e0e0f0;
    --text2: #8888aa;
    --accent: #6c63ff;
    --accent-hover: #7b73ff;
    --green: #4caf7d;
    --green-bg: rgba(76, 175, 125, 0.15);
    --red: #e74c5e;
    --red-bg: rgba(231, 76, 94, 0.15);
    --blue: #5b8def;
    --radius: 12px;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    justify-content: center;
    padding: 24px;
  }
  .container {
    width: 100%;
    max-width: 960px;
  }

  /* Header */
  .header {
    text-align: center;
    padding: 20px 0 16px;
  }
  .header h1 {
    font-size: 28px;
    background: linear-gradient(135deg, #6c63ff, #5b8def);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    display: inline-flex;
    align-items: center;
    gap: 10px;
  }
  .header h1 span { font-size: 30px; -webkit-text-fill-color: initial; }
  .header p {
    color: var(--text2);
    margin-top: 6px;
    font-size: 14px;
  }

  /* Connection Info */
  .conn-info {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 20px;
    margin-bottom: 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
  }
  .conn-info .urls {
    display: flex;
    gap: 16px;
    flex-wrap: wrap;
    align-items: center;
  }
  .conn-info .url-label { color: var(--text2); font-size: 13px; }
  .conn-info .url-value {
    background: var(--surface2);
    padding: 4px 12px;
    border-radius: 6px;
    font-family: 'SF Mono', 'Cascadia Code', monospace;
    font-size: 14px;
    color: var(--accent);
    cursor: pointer;
    transition: all 0.2s;
    border: 1px solid transparent;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .conn-info .url-value:hover {
    border-color: var(--accent);
    background: rgba(108,99,255,0.1);
  }
  .conn-info .url-value.copied::after {
    content: ' ✓ 已复制';
    color: var(--green);
    font-size: 12px;
  }
  .badge {
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 20px;
    font-weight: 600;
  }
  .badge.online { background: var(--green-bg); color: var(--green); }
  .badge.info { background: rgba(91,141,239,0.15); color: var(--blue); }

  /* Main Grid */
  .main-grid {
    display: grid;
    grid-template-columns: 1fr 320px;
    gap: 20px;
  }
  @media (max-width: 720px) {
    .main-grid { grid-template-columns: 1fr; }
    body { padding: 12px; }
    .container { max-width: 100%; }
  }

  /* Panel */
  .panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .panel-header {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .panel-header h2 {
    font-size: 16px;
    font-weight: 600;
  }
  .panel-header .count {
    font-size: 13px;
    color: var(--text2);
    background: var(--surface2);
    padding: 2px 10px;
    border-radius: 12px;
  }
  .panel-body {
    padding: 12px;
    max-height: 480px;
    overflow-y: auto;
  }

  /* File List */
  .file-list { list-style: none; }
  .file-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border-radius: 8px;
    transition: background 0.15s;
    gap: 8px;
  }
  .file-item:hover { background: var(--surface2); }
  .file-item + .file-item { margin-top: 2px; }
  .file-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .file-name {
    font-size: 14px;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .file-meta {
    font-size: 12px;
    color: var(--text2);
    display: flex;
    gap: 12px;
  }
  .file-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .file-actions button {
    width: 32px; height: 32px;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 15px;
    transition: all 0.15s;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .btn-download {
    background: rgba(76, 175, 125, 0.15);
    color: var(--green);
  }
  .btn-download:hover { background: rgba(76, 175, 125, 0.3); }
  .btn-delete {
    background: var(--red-bg);
    color: var(--red);
  }
  .btn-delete:hover { background: rgba(231, 76, 94, 0.3); }
  .empty-state {
    text-align: center;
    padding: 40px 20px;
    color: var(--text2);
  }
  .empty-state .icon { font-size: 40px; margin-bottom: 8px; }

  /* Upload Area */
  .upload-zone {
    border: 2px dashed var(--border);
    border-radius: 10px;
    padding: 30px 20px;
    text-align: center;
    cursor: pointer;
    transition: all 0.2s;
    margin-bottom: 12px;
  }
  .upload-zone:hover, .upload-zone.dragover {
    border-color: var(--accent);
    background: rgba(108,99,255,0.08);
  }
  .upload-zone .icon { font-size: 40px; }
  .upload-zone .text { font-size: 14px; margin-top: 8px; color: var(--text2); }
  .upload-zone .hint { font-size: 12px; margin-top: 4px; color: var(--text2); opacity: 0.6; }
  .upload-zone input { display: none; }

  /* Upload Progress */
  .upload-progress { display: none; margin-top: 8px; }
  .upload-progress.active { display: block; }
  .progress-bar {
    width: 100%;
    height: 6px;
    background: var(--surface2);
    border-radius: 3px;
    overflow: hidden;
    margin-top: 6px;
  }
  .progress-bar .fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent), var(--blue));
    width: 0%;
    border-radius: 3px;
    transition: width 0.3s;
  }
  .progress-text { font-size: 12px; color: var(--text2); margin-top: 4px; }

  /* Status / Toast */
  .toast-container {
    position: fixed;
    top: 20px;
    right: 20px;
    z-index: 9999;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .toast {
    padding: 12px 20px;
    border-radius: 10px;
    font-size: 14px;
    font-weight: 500;
    animation: slideIn 0.3s ease;
    max-width: 360px;
    box-shadow: 0 8px 30px rgba(0,0,0,0.4);
  }
  .toast.success { background: var(--green-bg); color: var(--green); border: 1px solid rgba(76,175,125,0.3); }
  .toast.error { background: var(--red-bg); color: var(--red); border: 1px solid rgba(231,78,94,0.3); }
  .toast.info { background: rgba(91,141,239,0.15); color: var(--blue); border: 1px solid rgba(91,141,239,0.3); }
  @keyframes slideIn {
    from { opacity: 0; transform: translateX(40px); }
    to { opacity: 1; transform: translateX(0); }
  }
  @keyframes slideOut {
    from { opacity: 1; transform: translateX(0); }
    to { opacity: 0; transform: translateX(40px); }
  }

  /* Server Log */
  .server-log {
    margin-top: 20px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 16px;
    font-family: 'SF Mono', 'Cascadia Code', monospace;
    font-size: 12px;
    max-height: 120px;
    overflow-y: auto;
    color: var(--text2);
  }
  .server-log .log-entry { padding: 2px 0; }
  .server-log .log-time { color: var(--text2); opacity: 0.5; }

  /* Footer */
  .footer {
    text-align: center;
    padding: 20px 0;
    font-size: 12px;
    color: var(--text2);
    opacity: 0.5;
  }

  /* Scrollbar */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text2); }
</style>
</head>
<body>

<div class="container">
  <!-- Header -->
  <div class="header">
    <h1><span>📁</span> 文件传输助手</h1>
    <p>从任意设备的浏览器传文件到这台电脑</p>
  </div>

  <!-- Connection Info -->
  <div class="conn-info" id="connInfo">
    <div class="urls">
      <span class="url-label">局域网:</span>
      <span class="url-value" id="localUrl" onclick="copyUrl(this)">加载中...</span>
      <span class="url-label" id="ngrokLabel" style="display:none;">远程:</span>
      <span class="url-value" id="ngrokUrl" style="display:none;" onclick="copyUrl(this)"></span>
    </div>
    <span class="badge online">● 运行中</span>
  </div>

  <!-- Main Grid -->
  <div class="main-grid">
    <!-- File List -->
    <div class="panel">
      <div class="panel-header">
        <h2>📂 文件列表</h2>
        <span class="count" id="fileCount">0 个文件</span>
      </div>
      <div class="panel-body" id="fileList">
        <div class="empty-state">
          <div class="icon">📁</div>
          <div>暂无文件</div>
          <div style="font-size:12px;margin-top:4px;">上传文件后这里会显示</div>
        </div>
      </div>
    </div>

    <!-- Upload Panel -->
    <div class="panel">
      <div class="panel-header">
        <h2>📤 上传文件</h2>
      </div>
      <div class="panel-body">
        <div class="upload-zone" id="uploadZone" onclick="document.getElementById('fileInput').click()">
          <div class="icon">☁️</div>
          <div class="text">点击或拖拽文件到此处</div>
          <div class="hint">支持任意文件类型 · 单文件最大 10GB</div>
          <input type="file" id="fileInput" multiple onchange="uploadFiles(this.files)">
        </div>

        <div class="upload-progress" id="uploadProgress">
          <div class="progress-text" id="progressText">正在上传...</div>
          <div class="progress-bar"><div class="fill" id="progressFill"></div></div>
        </div>

        <div style="margin-top:12px;">
          <button onclick="refreshFiles()" style="
            width:100%;padding:8px;border:1px solid var(--border);border-radius:8px;
            background:var(--surface2);color:var(--text);cursor:pointer;font-size:13px;
            transition:all 0.15s;
          " onmouseover="this.style.borderColor='var(--accent)'" onmouseout="this.style.borderColor='var(--border)'">
            🔄 刷新文件列表
          </button>
        </div>
      </div>
    </div>
  </div>

  <!-- Server Log -->
  <div class="server-log" id="serverLog">
    <div class="log-entry"><span class="log-time">[系统]</span> 服务器已启动，共享目录: <span id="shareDirDisplay"></span></div>
  </div>

  <!-- Footer -->
  <div class="footer">File Transfer Assistant v""" + VERSION + """</div>
</div>

<!-- Toast Container -->
<div class="toast-container" id="toastContainer"></div>

<script>
// ============================================================
//  文件传输助手 - 前端逻辑
// ============================================================
const BASE = '';
let uploadQueue = [];
let isUploading = false;

// --- Toast ---
function showToast(msg, type='info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'slideOut 0.3s ease forwards';
    setTimeout(() => toast.remove(), 300);
  }, 3000);
}

// --- Copy URL ---
function copyUrl(el) {
  const text = el.textContent.replace(' ✓ 已复制', '');
  navigator.clipboard.writeText(text).then(() => {
    el.classList.add('copied');
    setTimeout(() => el.classList.remove('copied'), 1500);
  }).catch(() => {
    // Fallback
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('copy');
    el.classList.add('copied');
    setTimeout(() => el.classList.remove('copied'), 1500);
  });
}

// --- File List ---
async function refreshFiles() {
  try {
    const res = await fetch('/api/files');
    const data = await res.json();
    renderFileList(data);
  } catch(e) {
    showToast('无法获取文件列表', 'error');
  }
}

function renderFileList(data) {
  const el = document.getElementById('fileList');
  const countEl = document.getElementById('fileCount');

  if (!data.files || data.files.length === 0) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="icon">📁</div>
        <div>暂无文件</div>
        <div style="font-size:12px;margin-top:4px;">上传文件后这里会显示</div>
      </div>`;
    countEl.textContent = '0 个文件';
    return;
  }

  countEl.textContent = `${data.files.length} 个文件`;

  // Get icons based on file extension
  function getFileIcon(name) {
    const ext = name.split('.').pop().toLowerCase();
    const icons = {
      'pdf': '📄', 'doc': '📝', 'docx': '📝', 'txt': '📄',
      'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️', 'svg': '🖼️', 'webp': '🖼️',
      'mp4': '🎬', 'mov': '🎬', 'avi': '🎬', 'mkv': '🎬',
      'mp3': '🎵', 'wav': '🎵', 'flac': '🎵',
      'zip': '🗜️', 'rar': '🗜️', '7z': '🗜️', 'gz': '🗜️', 'tar': '🗜️',
      'html': '🌐', 'css': '🎨', 'js': '⚡', 'py': '🐍', 'java': '☕', 'c': '⚙️', 'cpp': '⚙️',
      'json': '📋', 'xml': '📋', 'csv': '📊',
      'exe': '⚡', 'dmg': '💿', 'app': '📦',
      'xlsx': '📊', 'pptx': '📽️',
    };
    return icons[ext] || '📎';
  }

  let html = '<div class="file-list">';
  for (const file of data.files) {
    const icon = getFileIcon(file.name);
    const encodedName = encodeURIComponent(file.name);
    html += `
      <div class="file-item">
        <div class="file-info">
          <div class="file-name">${icon} ${escapeHtml(file.name)}</div>
          <div class="file-meta">
            <span>${file.size_display}</span>
            <span>${file.modified}</span>
          </div>
        </div>
        <div class="file-actions">
          <button class="btn-download" onclick="downloadFile('${encodedName}')" title="下载">⬇</button>
          <button class="btn-delete" onclick="deleteFile('${encodedName}')" title="删除">✕</button>
        </div>
      </div>`;
  }
  html += '</div>';
  el.innerHTML = html;
}

function escapeHtml(text) {
  const d = document.createElement('div');
  d.textContent = text;
  return d.innerHTML;
}

function downloadFile(encodedName) {
  window.open(`/download/${encodedName}`, '_blank');
  showToast('正在下载...', 'info');
}

async function deleteFile(encodedName) {
  if (!confirm('确定要删除这个文件吗？')) return;
  try {
    const res = await fetch(`/api/delete/${encodedName}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      showToast(data.message, 'success');
      refreshFiles();
      addLog('删除', decodeURIComponent(encodedName));
    } else {
      showToast(data.error || '删除失败', 'error');
    }
  } catch(e) {
    showToast('删除请求失败', 'error');
  }
}

// --- Upload ---
async function uploadFiles(files) {
  if (!files || files.length === 0) return;

  // Add to queue
  for (let i = 0; i < files.length; i++) {
    uploadQueue.push(files[i]);
  }

  if (!isUploading) {
    processQueue();
  }
}

async function processQueue() {
  if (uploadQueue.length === 0) {
    isUploading = false;
    document.getElementById('uploadProgress').classList.remove('active');
    refreshFiles();
    return;
  }

  isUploading = true;
  const file = uploadQueue.shift();
  const progressEl = document.getElementById('uploadProgress');
  const progressText = document.getElementById('progressText');
  const progressFill = document.getElementById('progressFill');

  progressEl.classList.add('active');
  progressText.textContent = `📤 正在上传: ${file.name}`;
  progressFill.style.width = '0%';

  try {
    // Use XMLHttpRequest for progress tracking
    const xhr = new XMLHttpRequest();

    const formData = new FormData();
    formData.append('file', file);

    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        progressFill.style.width = pct + '%';
        progressText.textContent = `📤 正在上传: ${file.name} (${pct}%)`;
      }
    });

    await new Promise((resolve, reject) => {
      xhr.addEventListener('load', () => {
        if (xhr.status === 200) {
          try {
            const data = JSON.parse(xhr.responseText);
            if (data.success) {
              resolve(data);
            } else {
              reject(new Error(data.error || '上传失败'));
            }
          } catch(e) {
            reject(new Error('解析响应失败'));
          }
        } else {
          reject(new Error(`服务器错误: ${xhr.status}`));
        }
      });
      xhr.addEventListener('error', () => reject(new Error('网络错误')));
      xhr.open('POST', '/api/upload');
      xhr.send(formData);
    });

    showToast(`✅ ${file.name} 上传成功`, 'success');
    addLog('上传', file.name);

  } catch(e) {
    showToast(`❌ ${file.name}: ${e.message}`, 'error');
    addLog('上传失败', `${file.name} - ${e.message}`);
  }

  // Continue queue
  setTimeout(processQueue, 200);
}

// --- Drag & Drop ---
const uploadZone = document.getElementById('uploadZone');
uploadZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadZone.classList.add('dragover');
});
uploadZone.addEventListener('dragleave', () => {
  uploadZone.classList.remove('dragover');
});
uploadZone.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadZone.classList.remove('dragover');
  if (e.dataTransfer.files.length > 0) {
    uploadFiles(e.dataTransfer.files);
  }
});

// --- Server Log ---
function addLog(type, msg) {
  const el = document.getElementById('serverLog');
  const time = new Date().toLocaleTimeString();
  const entry = document.createElement('div');
  entry.className = 'log-entry';
  entry.innerHTML = `<span class="log-time">[${time}]</span> ${type}: ${escapeHtml(msg)}`;
  el.appendChild(entry);
  el.scrollTop = el.scrollHeight;
}

// --- Init ---
async function init() {
  // Get server info
  try {
    const res = await fetch('/api/info');
    const info = await res.json();
    document.getElementById('shareDirDisplay').textContent = info.share_dir;

    // Show URLs
    const localUrl = `http://${info.client_ip}:${info.port}`;
    document.getElementById('localUrl').textContent = localUrl;

    // Try to detect ngrok from window location
    const hostname = window.location.hostname;
    if (hostname.includes('ngrok')) {
      document.getElementById('ngrokUrl').textContent = window.location.origin;
      document.getElementById('ngrokUrl').style.display = 'inline';
      document.getElementById('ngrokLabel').style.display = 'inline';
    }
  } catch(e) {
    document.getElementById('localUrl').textContent = '无法获取连接信息';
  }

  // Initial file list
  await refreshFiles();
  addLog('系统', '页面加载完成');
}

// Refresh every 5 seconds
setInterval(refreshFiles, 5000);

init();
</script>
</body>
</html>
"""


# ============================================================
#  NGROK 集成
# ============================================================
def start_ngrok(port):
    """尝试启动 ngrok 隧道"""
    try:
        import subprocess
        import threading
        import urllib.request
        import json
        import time

        # 检查 ngrok 是否已安装
        ngrok_path = None
        # 常见 ngrok 安装路径
        common_paths = [
            'ngrok',
            os.path.expanduser('~/ngrok'),
            os.path.expanduser('~/bin/ngrok'),
            r'C:\ngrok\ngrok.exe',
            r'C:\tools\ngrok\ngrok.exe',
        ]

        for p in common_paths:
            try:
                subprocess.run([p, 'version'], capture_output=True, check=True)
                ngrok_path = p
                break
            except:
                continue

        if not ngrok_path:
            print("\n  ⚠️ 未检测到 ngrok，跳过远程隧道")
            print("  💡 安装 ngrok: https://ngrok.com/download")
            print("     然后运行: ngrok config add-authtoken <your-token>")
            return None

        print(f"\n  🔗 正在启动 ngrok 隧道 (端口 {port})...")
        ngrok_process = subprocess.Popen(
            [ngrok_path, 'http', str(port), '--log=stdout'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # 等待 ngrok 启动
        time.sleep(2)

        # 获取 ngrok 公网 URL
        for _ in range(10):
            try:
                with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels') as resp:
                    data = json.loads(resp.read())
                    for tunnel in data.get('tunnels', []):
                        if tunnel.get('public_url', '').startswith('https://'):
                            public_url = tunnel['public_url']
                            print(f"  ✅ ngrok 隧道已建立!")
                            print(f"  🌐 远程地址: {public_url}")
                            return public_url
            except:
                time.sleep(1)

        print("  ⚠️ ngrok 启动但未能获取公网地址")
        return None

    except Exception as e:
        print(f"  ⚠️ ngrok 启动失败: {e}")
        return None


# ============================================================
#  控制台界面
# ============================================================
def print_banner(port, local_ip, ngrok_url=None):
    """打印启动横幅"""
    print("""
╔══════════════════════════════════════════════════╗
║         📁  文件传输助手 v""" + VERSION + """               ║
║      File Transfer Assistant                     ║
╚══════════════════════════════════════════════════╝
""")
    print(f"  📂 共享目录: {SHARE_DIR}")
    print(f"  🚀 服务端口: {port}")
    print()
    print(f"  🌐 局域网地址:")
    print(f"       http://{local_ip}:{port}")
    print(f"       http://127.0.0.1:{port}")
    print()

    if ngrok_url:
        print(f"  🌍 远程地址 (互联网):")
        print(f"       {ngrok_url}")
        print()

    print(f"  📱 在浏览器打开上述地址，即可传输文件")
    print(f"  ⏹️  按 Ctrl+C 停止服务器")
    print()


# ============================================================
#  主入口
# ============================================================
def main():
    global PORT, SHARE_DIR, USE_NGROK, HTML_PAGE

    # 处理命令行参数
    parser = argparse.ArgumentParser(
        description='📁 文件传输助手 - 在浏览器中传输文件',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python file-transfer.py                    # 默认端口 8080
  python file-transfer.py --port 9090        # 指定端口
  python file-transfer.py --share ./myfiles  # 指定共享目录
  python file-transfer.py --ngrok            # 启用远程访问
  python file-transfer.py --open             # 启动后自动打开浏览器
        """
    )
    parser.add_argument('--port', '-p', type=int, default=8080, help='服务端口 (默认: 8080)')
    parser.add_argument('--share', '-s', type=str, default=None, help='共享目录路径 (默认: ~/file-transfer-share)')
    parser.add_argument('--ngrok', action='store_true', help='启动 ngrok 远程隧道')
    parser.add_argument('--open', '-o', action='store_true', help='启动后自动打开浏览器')

    args = parser.parse_args()

    PORT = args.port
    USE_NGROK = args.ngrok

    if args.share:
        SHARE_DIR = os.path.abspath(args.share)
    else:
        SHARE_DIR = ensure_dir(SHARE_DIR)

    # 确保共享目录存在
    ensure_dir(SHARE_DIR)

    # 获取本地 IP
    local_ip = get_local_ip()

    # 启动 ngrok (如果启用)
    ngrok_url = None
    if USE_NGROK:
        ngrok_url = start_ngrok(PORT)

    # 启动 HTTP 服务器
    server = HTTPServer(('0.0.0.0', PORT), FileTransferHandler)

    # 打印启动信息
    print_banner(PORT, local_ip, ngrok_url)

    # 自动打开浏览器
    if args.open:
        webbrowser.open(f'http://127.0.0.1:{PORT}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  👋 正在关闭服务器...")
        server.shutdown()
        print("  ✅ 已停止")


if __name__ == '__main__':
    main()
