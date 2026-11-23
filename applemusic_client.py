"""
Apple Music API istemcisi.

Iki anahtar gerekir:
  - Gelistirici anahtari (developer token): Apple Developer hesabindaki MusicKit
    anahtariyla (.p8) burada uretilir, 6 aya kadar gecerli. Katalog aramasi icin yeter.
  - Kullanici anahtari (Music-User-Token): Osman'in Apple ID onayiyla bir kez
    tarayicida alinir (am_token.py), ~6 ay gecerli. Kitapliga yazmak icin gerekir.

Apple'in genel API'si kitaplik listesine EKLEME yapabilir; listeden SILME ve
SIRALAMA yapamaz. Bu yuzden Apple Music kolu yalniz ekler.
"""
import time
import requests
import config

API = "https://api.music.apple.com/v1"
ISRC_BATCH = 25      # filter[isrc] istek basina en fazla 25 kod
ADD_BATCH = 50       # listeye tek istekte eklenen sarki sayisi


class AuthError(Exception):
    """Anahtar gecersiz ya da suresi dolmus (401/403)."""


def make_developer_token(ttl_sec=15_552_000):
    import jwt  # PyJWT[crypto]
    with open(config.APPLE_KEY_PATH, "r", encoding="utf-8") as f:
        key = f.read()
    now = int(time.time())
    return jwt.encode(
        {"iss": config.APPLE_TEAM_ID, "iat": now, "exp": now + ttl_sec},
        key, algorithm="ES256", headers={"kid": config.APPLE_KEY_ID},
    )


def read_user_token():
    try:
        with open(config.APPLE_USER_TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


class Client:
    def __init__(self, developer_token, user_token=None, storefront=None, session=None):
        self.dev = developer_token
        self.user = user_token
        self.http = session or requests.Session()
        self.storefront = storefront or config.APPLE_STOREFRONT

    # --- HTTP ---
    def _req(self, method, path, params=None, json=None, user=False, retries=4):
        headers = {"Authorization": f"Bearer {self.dev}"}
        if user:
            if not self.user:
                raise AuthError("Kullanici anahtari yok (am_token.py ile al).")
            headers["Music-User-Token"] = self.user
        if path.startswith("http"):
            url = path
        elif path.startswith("/v1/"):   # Apple'in "next" baglantilari /v1/... ile gelir
            url = API[:-len("/v1")] + path
        else:
            url = API + path
        for attempt in range(retries + 1):
            r = self.http.request(method, url, params=params, json=json,
                                  headers=headers, timeout=30)
            # Apple ayni istege ara ara bos govdeli 401 donduruyor (anahtar gecerliyken);
            # gercek yetki hatasi her denemede tekrarlar, o yuzden 401'i de yeniden dene.
            if r.status_code in (401, 429) or r.status_code >= 500:
                if attempt < retries:
                    time.sleep(int(r.headers.get("Retry-After") or 2 ** (attempt + 1)))
                    continue
            if r.status_code in (401, 403):
                raise AuthError(f"{r.status_code} {method} {path} {(params or {})} {r.text[:150]}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()

    def _paged(self, path, params=None, user=False):
        out = []
        data = self._req("GET", path, params=params, user=user)
        while data:
            out.extend(data.get("data") or [])
            nxt = data.get("next")
            data = self._req("GET", nxt, user=user) if nxt else None
        return out

    # --- Katalog (yalniz gelistirici anahtari) ---
    def detect_storefront(self):
        data = self._req("GET", "/me/storefront", user=True)
        self.storefront = data["data"][0]["id"]
        return self.storefront

    def songs_by_isrc(self, isrcs):
        """{ISRC: [aday, ...]} — adaylar matcher bicimindedir."""
        found = {}
        isrcs = [i for i in dict.fromkeys(isrcs) if i]
        for i in range(0, len(isrcs), ISRC_BATCH):
            chunk = isrcs[i:i + ISRC_BATCH]
            data = self._req("GET", f"/catalog/{self.storefront}/songs",
                             params={"filter[isrc]": ",".join(chunk)})
            for s in (data or {}).get("data") or []:
                c = _candidate(s)
                if c["isrc"]:
                    found.setdefault(c["isrc"].upper(), []).append(c)
        return found

    def search_songs(self, name, artists, limit=None):
        term = f"{name} {artists.split(',')[0] if artists else ''}".strip()
        data = self._req("GET", f"/catalog/{self.storefront}/search",
                         params={"term": term, "types": "songs",
                                 "limit": limit or config.SEARCH_RESULT_LIMIT})
        songs = (((data or {}).get("results") or {}).get("songs") or {}).get("data") or []
        return [_candidate(s) for s in songs]

    # --- Dinleme gecmisi (kullanici anahtari, salt okunur) ---
    def recent_played(self, pages=2):
        """Son calinan sarkilar, en yeni basta. Apple zaman damgasi vermez; sayfa 30."""
        out = []
        for i in range(pages):
            d = self._req("GET", "/me/recent/played/tracks", user=True,
                          params={"limit": 30, "offset": 30 * i})
            items = (d or {}).get("data") or []
            for t in items:
                a = t.get("attributes") or {}
                out.append({"id": str(t.get("id")), "type": t.get("type"),
                            "title": a.get("name") or "", "artists": a.get("artistName") or "",
                            "album": a.get("albumName") or "",
                            "duration_ms": a.get("durationInMillis")})
            if len(items) < 30:
                break
        return out

    # --- Kitaplik (kullanici anahtari) ---
    def library_playlists(self):
        return [
            {"id": p["id"], "name": (p.get("attributes") or {}).get("name", ""),
             "can_edit": (p.get("attributes") or {}).get("canEdit", False)}
            for p in self._paged("/me/library/playlists", {"limit": 100}, user=True)
        ]

    def playlist_catalog_ids(self, playlist_id):
        ids = set()
        for t in self._paged(f"/me/library/playlists/{playlist_id}/tracks",
                             {"limit": 100}, user=True):
            # Kitaplik ogesinin id'si "i.xxx"; katalog karsiligi playParams.catalogId
            cid = ((t.get("attributes") or {}).get("playParams") or {}).get("catalogId")
            if cid:
                ids.add(str(cid))
        return ids

    def playlist_items(self, playlist_id):
        """Kitaplik listesinin ogeleri, listedeki sirayla:
        [{lib_id, catalog_id|None, title, artist, duration_sec}]"""
        out = []
        for t in self._paged(f"/me/library/playlists/{playlist_id}/tracks",
                             {"limit": 100}, user=True):
            a = t.get("attributes") or {}
            out.append({"lib_id": str(t.get("id")),
                        "catalog_id": (a.get("playParams") or {}).get("catalogId"),
                        "title": a.get("name") or "", "artist": a.get("artistName") or "",
                        "duration_sec": round((a.get("durationInMillis") or 0) / 1000)})
        return out

    def isrc_of(self, catalog_ids):
        """{catalog_id: ISRC}"""
        out = {}
        ids = [i for i in dict.fromkeys(catalog_ids) if i]
        for i in range(0, len(ids), 25):
            d = self._req("GET", f"/catalog/{self.storefront}/songs",
                          params={"ids": ",".join(ids[i:i + 25])})
            for s in (d or {}).get("data") or []:
                isrc = (s.get("attributes") or {}).get("isrc")
                if isrc:
                    out[str(s["id"])] = isrc.upper()
        return out

    def create_playlist(self, name, description="Spotify'dan senkronize edildi"):
        data = self._req("POST", "/me/library/playlists", user=True, json={
            "attributes": {"name": name, "description": description},
        })
        return data["data"][0]["id"]

    def add_to_library(self, catalog_ids):
        """Sarkilari once kitapliga ekle. Kitaplikta olmayan sarki listeye gec
        (arkadan) eklendigi icin sira bozuluyor; once kitaplik, sonra liste."""
        for i in range(0, len(catalog_ids), ADD_BATCH):
            self._req("POST", "/me/library", user=True,
                      params={"ids[songs]": ",".join(catalog_ids[i:i + ADD_BATCH])})

    def missing_from_library(self, catalog_ids):
        """Kitapliga henuz dusmemis katalog id'leri (relate=library ile)."""
        missing = []
        for i in range(0, len(catalog_ids), 25):
            chunk = catalog_ids[i:i + 25]
            d = self._req("GET", f"/catalog/{self.storefront}/songs", user=True,
                          params={"ids": ",".join(chunk), "relate": "library"})
            for s in (d or {}).get("data") or []:
                lib = ((s.get("relationships") or {}).get("library") or {}).get("data")
                if not lib:
                    missing.append(str(s.get("id")))
        return missing

    def wait_in_library(self, catalog_ids, timeout=300, step=5):
        """Hepsi kitapliga dusene kadar bekle. Kalanlari dondurur (bos = hazir)."""
        left = list(dict.fromkeys(catalog_ids))
        deadline = time.time() + timeout
        while left and time.time() < deadline:
            left = self.missing_from_library(left)
            if left:
                time.sleep(step)
        return left

    def add_tracks(self, playlist_id, catalog_ids):
        """Listenin sonuna ekler; eklenen sayisini dondurur."""
        added = 0
        for i in range(0, len(catalog_ids), ADD_BATCH):
            chunk = catalog_ids[i:i + ADD_BATCH]
            self._req("POST", f"/me/library/playlists/{playlist_id}/tracks", user=True,
                      json={"data": [{"id": c, "type": "songs"} for c in chunk]})
            added += len(chunk)
        return added


def _candidate(song):
    a = song.get("attributes") or {}
    return {
        "am_id": str(song.get("id")),
        "title": a.get("name") or "",
        "artists": a.get("artistName") or "",
        "duration_sec": round((a.get("durationInMillis") or 0) / 1000),
        "isrc": (a.get("isrc") or "").upper(),
        "is_music": True,
    }


def get_client(need_user=True):
    user = read_user_token() if need_user else None
    if need_user and not user:
        raise AuthError("Kullanici anahtari yok (am_token.py ile al).")
    return Client(make_developer_token(), user)
