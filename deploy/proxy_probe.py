"""服务器端代理出口自检：验证 ZooProxy 凭证 + 到 amazon.com 的链路。

用法（先 source .env）：
  set -a; . /opt/amazon-crawler/.env; set +a
  python deploy/proxy_probe.py
"""
import base64
import os
import re
import sys
import urllib.request

username = os.environ.get("ZOO_PROXY_USERNAME", "")
password = os.environ.get("ZOO_PROXY_PASSWORD", "")
if not username or not password:
    print("缺 ZOO_PROXY_USERNAME / ZOO_PROXY_PASSWORD")
    sys.exit(2)

proxy = "http://us-eu.zooproxy.com:5000"
token = base64.b64encode(f"{username}:{password}".encode()).decode()

# 独立会话 ID，避免与生产 worker 的 sid 撞车
sid_user = re.sub(r"-sid-[^-]+-t-\d+", "-sid-cloudprobe1-t-10", username, count=1)
tok2 = base64.b64encode(f"{sid_user}:{password}".encode()).decode()


class AuthProxy(urllib.request.HTTPSHandler):
    def http_open(self, req):
        req.add_header("Proxy-Authorization", f"Basic {tok2}")
        return super().http_open(req)

    def https_open(self, req):
        req.add_header("Proxy-Authorization", f"Basic {tok2}")
        return super().https_open(req)


opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": proxy, "https": proxy}), AuthProxy()
)
req = urllib.request.Request(
    "https://www.amazon.com/dp/B07CRHSTSL",
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"},
)
try:
    body = opener.open(req, timeout=60).read().decode("utf-8", "replace")
except Exception as exc:
    print(f"代理链路失败: {exc}")
    sys.exit(1)
captcha = "captcha" in body.lower() or "Robot Check" in body
title = re.search(r"<title>(.{0,60})", body)
print(f"captcha={captcha} bytes={len(body)} title={(title.group(1) if title else '')[:50]}")
sys.exit(0 if not captcha else 1)
