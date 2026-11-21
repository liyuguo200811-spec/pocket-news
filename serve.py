# -*- coding: utf-8 -*-
"""本地预览服务器。

页面里的链接是不带 .html 的（为了躲开 Cloudflare 的 308 重定向），
直接双击 index.html 用 file:// 打开会点不动，所以用这个脚本起一个本地服务：
    python serve.py      然后打开 http://127.0.0.1:8000

它会把 /a01 自动映射到 a01.html，行为和线上一致。
"""

import http.server
import os
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "site"
PORT = 8000


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def translate_path(self, path):
        p = super().translate_path(path)
        if not os.path.exists(p) and os.path.exists(p + ".html"):
            return p + ".html"
        return p

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print("本地预览： http://127.0.0.1:%d" % PORT)
        print("按 Ctrl+C 停止")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
