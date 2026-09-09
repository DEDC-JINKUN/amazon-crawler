"""验证：手动覆盖 cookie 强制美国本地化。"""
import urllib.request, urllib.parse, http.cookiejar, json, re
from http.cookiejar import Cookie

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
base_headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def mk_cookie(name, value, path="/", domain=".amazon.com"):
    return Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=True,
        path=path, path_specified=True, secure=False, expires=None, discard=False,
        comment=None, comment_url=None, rest={"HttpOnly": False}, rfc2109=False
    )

# Step 1: 先访问拿初始 cookie
req1 = urllib.request.Request("https://www.amazon.com/s?k=laptop", headers=base_headers)
resp1 = opener.open(req1, timeout=15)
resp1.read()
print("Step 1: initial visit OK")

# Step 2: 设置 ZIP
url = "https://www.amazon.com/gp/delivery/ajax/address-change.html"
data = urllib.parse.urlencode({
    "locationType": "LOCATION_INPUT", "zipCode": "30322",
    "storeContext": "generic", "deviceType": "web",
    "pageType": "Gateway", "actionSource": "glow",
}).encode()
req2 = urllib.request.Request(url, data=data, headers={
    **base_headers,
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "Referer": "https://www.amazon.com/", "X-Requested-With": "XMLHttpRequest"
})
resp2 = opener.open(req2, timeout=10)
print("Step 2: set ZIP OK")

# Step 3: 手动覆盖 cookie（强制美国）
jar.clear(".amazon.com")
jar.set_cookie(mk_cookie("lc-main", "en_US"))
jar.set_cookie(mk_cookie("sp-cdn", "L5Z9:US"))
jar.set_cookie(mk_cookie("i18n-prefs", "USD"))
jar.set_cookie(mk_cookie("ubid-main", "131-2155787-6867855"))
jar.set_cookie(mk_cookie("session-id", "139-9456251-2478045"))
jar.set_cookie(mk_cookie("session-id-time", "2082787201l"))

print("Cookies after override:")
for c in jar:
    print(f"  {c.name}={c.value[:50]}")

# Step 4: 搜索页
req3 = urllib.request.Request("https://www.amazon.com/s?k=laptop", headers=base_headers)
resp3 = opener.open(req3, timeout=15)
body3 = resp3.read().decode("utf-8", errors="replace")

dollar = chr(36)
dp = dollar + r"[\d,]+\.?\d*"
price_matches = re.findall(dp, body3[:10000])

print(f"\nStep 4: body len={len(body3)}")
print(f"  Has dollar price: {len(price_matches)} matches")
print(f"  Has CNY: {'CNY' in body3[:5000]}")
print(f"  Has USD text: {'USD' in body3[:5000]}")
print(f"  Has Deliver to US: {'Deliver to United States' in body3[:5000]}")
print(f"  Prices (first 10): {price_matches[:10]}")
