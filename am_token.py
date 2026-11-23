"""
Apple Music kullanici anahtarini (Music-User-Token) bir kez al.

Mac'te calistir:  python am_token.py
Tarayicida http://127.0.0.1:8898 acilir; "Apple Music'e bagla" ile Apple ID onayi
verilir. Anahtar APPLE_USER_TOKEN_FILE dosyasina (0600) yazilir. Sonra sunucuya:
  scp .applemusic_user_token servis-sunucu:/opt/music-sync/
Anahtar ~6 ayda bir yenilenir; sure dolunca gece senkronu Telegram'dan haber verir.
"""
import json
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import applemusic_client as amc

PORT = 8898

PAGE = """<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MusicSync · Apple Music</title>
<style>body{font:16px -apple-system,system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem}
button{font:inherit;padding:.7rem 1.2rem;border-radius:.6rem;border:0;background:#fa2d48;color:#fff;cursor:pointer}
#s{margin-top:1rem;color:#555}</style>
<script src="https://js-cdn.music.apple.com/musickit/v3/musickit.js" data-web-components async></script>
</head><body><h1>MusicSync</h1>
<p>Apple Music kitaplığına liste eklemek için bir kez onay ver.</p>
<button id="b">Apple Music'e bağla</button><p id="s"></p>
<script>
const DEV = __DEV__;
const s = document.getElementById('s');
document.addEventListener('musickitloaded', async () => {
  await MusicKit.configure({developerToken: DEV, app: {name: 'MusicSync', build: '1.0'}});
  s.textContent = 'Hazır.';
});
document.getElementById('b').onclick = async () => {
  try {
    const token = await MusicKit.getInstance().authorize();
    const r = await fetch('/token', {method: 'POST', body: token});
    s.textContent = r.ok ? 'Tamam, anahtar kaydedildi. Bu sekmeyi kapatabilirsin.' : 'Kaydedilemedi.';
  } catch (e) { s.textContent = 'Hata: ' + e; }
};
</script></body></html>"""


def main():
    dev = amc.make_developer_token()
    page = PAGE.replace("__DEV__", json.dumps(dev)).encode("utf-8")
    done = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def do_POST(self):
            if self.path != "/token":
                self.send_response(404)
                self.end_headers()
                return
            token = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode().strip()
            if not token:
                self.send_response(400)
                self.end_headers()
                return
            path = config.APPLE_USER_TOKEN_FILE
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            self.send_response(204)
            self.end_headers()
            done.set()

    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"Tarayicida ac: {url}")
    webbrowser.open(url)
    done.wait()
    srv.shutdown()
    print(f"Anahtar yazildi: {config.APPLE_USER_TOKEN_FILE}")
    print("Sunucuya: scp .applemusic_user_token servis-sunucu:/opt/music-sync/")


if __name__ == "__main__":
    main()
