import urllib.request
import urllib.error

try:
    req = urllib.request.Request("http://127.0.0.1:5051/api/stats")
    with urllib.request.urlopen(req) as response:
        print(response.read().decode())
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.reason}")
    print(e.read().decode())
except Exception as e:
    print(f"Connection Error: {e}")
