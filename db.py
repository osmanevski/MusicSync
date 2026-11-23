"""
SQLite veritabani. Iki sey takip eder:
  1. synced_tracks: basariyla eklenmis sarkilar (tekrar eklemeyi onler, kotayi/zamani korur)
  2. pending_review: supheli eslesmeler (sen onaylayana kadar bekler)
  3. playlist_snapshots: Spotify playlist snapshot_id'leri (degismemisse atla)
"""
import sqlite3
from contextlib import contextmanager
import config


@contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS synced_tracks (
            spotify_track_id TEXT,
            yt_playlist_id   TEXT,
            yt_video_id      TEXT,
            track_name       TEXT,
            artist_name      TEXT,
            synced_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (spotify_track_id, yt_playlist_id)
        );

        CREATE TABLE IF NOT EXISTS pending_review (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            spotify_track_id TEXT,
            yt_playlist_id   TEXT,
            track_name       TEXT,
            artist_name      TEXT,
            spotify_duration INTEGER,
            candidates_json  TEXT,   -- en iyi adaylar JSON olarak
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (spotify_track_id, yt_playlist_id)
        );

        CREATE TABLE IF NOT EXISTS playlist_snapshots (
            spotify_playlist_id TEXT PRIMARY KEY,
            snapshot_id         TEXT,
            yt_playlist_id      TEXT,
            updated_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Bir Spotify listesinde EN SON gordugumuz track id'leri.
        -- Yeni eklenenleri tespit etmek icin onceki hal ile kiyaslariz.
        CREATE TABLE IF NOT EXISTS playlist_track_state (
            spotify_playlist_id TEXT,
            spotify_track_id    TEXT,
            PRIMARY KEY (spotify_playlist_id, spotify_track_id)
        );

        -- Ters yon (YouTube -> Spotify) tamamen ayri tutulur.
        CREATE TABLE IF NOT EXISTS reverse_playlist_state (
            yt_playlist_id TEXT,
            yt_video_id    TEXT,
            PRIMARY KEY (yt_playlist_id, yt_video_id)
        );

        CREATE TABLE IF NOT EXISTS reverse_synced_tracks (
            yt_playlist_id     TEXT,
            yt_video_id        TEXT,
            spotify_playlist_id TEXT,
            spotify_track_id   TEXT,
            track_name         TEXT,
            artist_name        TEXT,
            synced_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (yt_playlist_id, yt_video_id, spotify_playlist_id)
        );

        -- Apple Music kolu (am_sync.py). Yalniz ekler; Apple'in API'si silemez.
        CREATE TABLE IF NOT EXISTS am_playlists (
            spotify_playlist_id TEXT PRIMARY KEY,
            am_playlist_id      TEXT,
            name                TEXT
        );

        CREATE TABLE IF NOT EXISTS am_synced (
            spotify_track_id TEXT,
            am_playlist_id   TEXT,
            am_song_id       TEXT,
            track_name       TEXT,
            artist_name      TEXT,
            via              TEXT,   -- isrc | search | existing
            synced_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (spotify_track_id, am_playlist_id)
        );

        -- Ters yon (Apple -> Spotify): Apple listesinin son gorulen hali (kitaplik oge id'si
        -- kalicidir; katalog id'si bazi ogelerde yok).
        CREATE TABLE IF NOT EXISTS am_rev_seen (
            am_playlist_id TEXT,
            lib_id         TEXT,
            catalog_id     TEXT,
            title          TEXT,
            artist         TEXT,
            PRIMARY KEY (am_playlist_id, lib_id)
        );

        -- Apple'da eklenen ama Spotify karsiligi kesin bulunamayan sarkilar
        CREATE TABLE IF NOT EXISTS am_rev_pending (
            am_playlist_id  TEXT,
            lib_id          TEXT,
            catalog_id      TEXT,
            title           TEXT,
            artist          TEXT,
            reason          TEXT,
            candidates_json TEXT,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (am_playlist_id, lib_id)
        );

        -- Apple'dan silinen sarkilar: Spotify'dan silmek Osman'in onayiyla
        CREATE TABLE IF NOT EXISTS am_rev_removed (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            spotify_playlist_id TEXT,
            spotify_track_id    TEXT,
            playlist_name       TEXT,
            title               TEXT,
            artist              TEXT,
            status              TEXT DEFAULT 'bekliyor',  -- bekliyor | silindi | kalsin
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (spotify_playlist_id, spotify_track_id, status)
        );

        -- Spotify sarkisi <-> Apple katalog sarkisi denkligi. ISRC ile bulunan ya da
        -- Osman'in elle onayladigi eslesmeler; iki yon de bunu "ayni sarki" sayar.
        -- am_song_id bos ('') ise: Osman "Apple'da yok" dedi, bir daha onerilmez.
        CREATE TABLE IF NOT EXISTS am_links (
            spotify_track_id TEXT PRIMARY KEY,
            am_song_id       TEXT,
            via              TEXT,   -- isrc | manual | none
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS am_pending (
            spotify_track_id TEXT,
            am_playlist_id   TEXT,
            track_name       TEXT,
            artist_name      TEXT,
            candidates_json  TEXT,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (spotify_track_id, am_playlist_id)
        );
        """)
        # am_pending'e sonradan eklenen alanlar (eski tablolar icin)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(am_pending)")}
        for col, typ in (("reason", "TEXT"), ("spotify_duration", "INTEGER"), ("isrc", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE am_pending ADD COLUMN {col} {typ}")


def is_synced(spotify_track_id, yt_playlist_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM synced_tracks WHERE spotify_track_id=? AND yt_playlist_id=?",
            (spotify_track_id, yt_playlist_id)
        ).fetchone()
        return row is not None


def mark_synced(spotify_track_id, yt_playlist_id, yt_video_id, track_name, artist_name):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO synced_tracks
               (spotify_track_id, yt_playlist_id, yt_video_id, track_name, artist_name)
               VALUES (?,?,?,?,?)""",
            (spotify_track_id, yt_playlist_id, yt_video_id, track_name, artist_name)
        )


def add_pending(spotify_track_id, yt_playlist_id, track_name, artist_name,
                spotify_duration, candidates_json):
    with get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO pending_review
               (spotify_track_id, yt_playlist_id, track_name, artist_name,
                spotify_duration, candidates_json)
               VALUES (?,?,?,?,?,?)""",
            (spotify_track_id, yt_playlist_id, track_name, artist_name,
             spotify_duration, candidates_json)
        )


def get_pending(yt_playlist_id=None):
    with get_db() as conn:
        if yt_playlist_id:
            return conn.execute(
                "SELECT * FROM pending_review WHERE yt_playlist_id=? ORDER BY created_at",
                (yt_playlist_id,)
            ).fetchall()
        return conn.execute(
            "SELECT * FROM pending_review ORDER BY created_at"
        ).fetchall()


def get_yt_playlist_for_spotify(spotify_playlist_id):
    """Spotify liste id'sinden, ona karsilik gelen YouTube liste id'sini bul."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT yt_playlist_id FROM playlist_snapshots WHERE spotify_playlist_id=?",
            (spotify_playlist_id,)
        ).fetchone()
        return row["yt_playlist_id"] if row else None


def get_synced_for_playlist(yt_playlist_id):
    """Bu YouTube listesine BIZIM ekledigimiz (DB'de kayitli) sarkilari dondur.
    Donen: [{spotify_track_id, yt_video_id, track_name, artist_name}]"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT spotify_track_id, yt_video_id, track_name, artist_name
               FROM synced_tracks WHERE yt_playlist_id=?""",
            (yt_playlist_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_synced(spotify_track_id, yt_playlist_id):
    """Bir sarkiyi synced kaydindan sil (YouTube'dan silindiginde)."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM synced_tracks WHERE spotify_track_id=? AND yt_playlist_id=?",
            (spotify_track_id, yt_playlist_id)
        )


def remove_pending(pending_id):
    with get_db() as conn:
        conn.execute("DELETE FROM pending_review WHERE id=?", (pending_id,))


def get_snapshot(spotify_playlist_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT snapshot_id FROM playlist_snapshots WHERE spotify_playlist_id=?",
            (spotify_playlist_id,)
        ).fetchone()
        return row["snapshot_id"] if row else None


def set_snapshot(spotify_playlist_id, snapshot_id, yt_playlist_id):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO playlist_snapshots
               (spotify_playlist_id, snapshot_id, yt_playlist_id, updated_at)
               VALUES (?,?,?,CURRENT_TIMESTAMP)""",
            (spotify_playlist_id, snapshot_id, yt_playlist_id)
        )


def get_track_state(spotify_playlist_id):
    """Bu listede EN SON gordugumuz Spotify track id'lerini kume olarak dondur.
    Hic kayit yoksa None dondur (ilk kez goruluyor demektir)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT spotify_track_id FROM playlist_track_state WHERE spotify_playlist_id=?",
            (spotify_playlist_id,)
        ).fetchall()
        if not rows:
            return None
        return {r["spotify_track_id"] for r in rows}


def set_track_state(spotify_playlist_id, track_ids):
    """Bu listenin guncel track id'lerini kaydet (eskisini silip yenisini yaz)."""
    with get_db() as conn:
        conn.execute(
            "DELETE FROM playlist_track_state WHERE spotify_playlist_id=?",
            (spotify_playlist_id,)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO playlist_track_state (spotify_playlist_id, spotify_track_id) VALUES (?,?)",
            [(spotify_playlist_id, tid) for tid in track_ids]
        )


def get_reverse_track_state(yt_playlist_id):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT yt_video_id FROM reverse_playlist_state WHERE yt_playlist_id=?",
            (yt_playlist_id,)
        ).fetchall()
        if not rows:
            return None
        return {r["yt_video_id"] for r in rows}


def set_reverse_track_state(yt_playlist_id, video_ids):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM reverse_playlist_state WHERE yt_playlist_id=?", (yt_playlist_id,)
        )
        conn.executemany(
            "INSERT OR IGNORE INTO reverse_playlist_state (yt_playlist_id, yt_video_id) VALUES (?,?)",
            [(yt_playlist_id, vid) for vid in video_ids]
        )


def mark_reverse_synced(yt_playlist_id, yt_video_id, spotify_playlist_id,
                        spotify_track_id, track_name, artist_name):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reverse_synced_tracks
               (yt_playlist_id, yt_video_id, spotify_playlist_id, spotify_track_id,
                track_name, artist_name) VALUES (?,?,?,?,?,?)""",
            (yt_playlist_id, yt_video_id, spotify_playlist_id, spotify_track_id,
             track_name, artist_name)
        )


def get_reverse_synced(yt_playlist_id, spotify_playlist_id):
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM reverse_synced_tracks
               WHERE yt_playlist_id=? AND spotify_playlist_id=?""",
            (yt_playlist_id, spotify_playlist_id)
        ).fetchall()
        return [dict(r) for r in rows]


def remove_reverse_synced(yt_playlist_id, yt_video_id, spotify_playlist_id):
    with get_db() as conn:
        conn.execute(
            """DELETE FROM reverse_synced_tracks
               WHERE yt_playlist_id=? AND yt_video_id=? AND spotify_playlist_id=?""",
            (yt_playlist_id, yt_video_id, spotify_playlist_id)
        )


# --- Apple Music kolu ---

def get_am_playlist(spotify_playlist_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT am_playlist_id FROM am_playlists WHERE spotify_playlist_id=?",
            (spotify_playlist_id,)).fetchone()
        return row["am_playlist_id"] if row else None


def set_am_playlist(spotify_playlist_id, am_playlist_id, name):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO am_playlists VALUES (?,?,?)",
                     (spotify_playlist_id, am_playlist_id, name))


def am_done_ids(am_playlist_id, include_pending=True):
    """Bu Apple listesi icin islenmis (eklenmis ya da beklemede) Spotify id'leri."""
    with get_db() as conn:
        ids = {r[0] for r in conn.execute(
            "SELECT spotify_track_id FROM am_synced WHERE am_playlist_id=?",
            (am_playlist_id,))}
        if include_pending:
            ids |= {r[0] for r in conn.execute(
                "SELECT spotify_track_id FROM am_pending WHERE am_playlist_id=?",
                (am_playlist_id,))}
        return ids


def am_song_ids(am_playlist_id):
    with get_db() as conn:
        return {r[0] for r in conn.execute(
            "SELECT am_song_id FROM am_synced WHERE am_playlist_id=?", (am_playlist_id,))}


def mark_am_synced(rows):
    """rows: [(spotify_track_id, am_playlist_id, am_song_id, name, artists, via)]"""
    with get_db() as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO am_synced
               (spotify_track_id, am_playlist_id, am_song_id, track_name, artist_name, via)
               VALUES (?,?,?,?,?,?)""", rows)
        conn.executemany(
            "DELETE FROM am_pending WHERE spotify_track_id=? AND am_playlist_id=?",
            [(r[0], r[1]) for r in rows])


def add_am_pending(spotify_track_id, am_playlist_id, name, artists, candidates_json,
                   reason="", spotify_duration=None, isrc=""):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO am_pending
               (spotify_track_id, am_playlist_id, track_name, artist_name, candidates_json,
                reason, spotify_duration, isrc)
               VALUES (?,?,?,?,?,?,?,?)""",
            (spotify_track_id, am_playlist_id, name, artists, candidates_json,
             reason, spotify_duration, isrc))


def get_am_pending(am_playlist_id=None):
    with get_db() as conn:
        if am_playlist_id:
            rows = conn.execute("SELECT * FROM am_pending WHERE am_playlist_id=?",
                                (am_playlist_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM am_pending").fetchall()
        return [dict(r) for r in rows]


def get_am_link(spotify_track_id):
    """Donen: None (bilinmiyor) | {am_song_id, via}"""
    with get_db() as conn:
        row = conn.execute("SELECT am_song_id, via FROM am_links WHERE spotify_track_id=?",
                           (spotify_track_id,)).fetchone()
        return dict(row) if row else None


def set_am_link(spotify_track_id, am_song_id, via):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO am_links (spotify_track_id, am_song_id, via) "
                     "VALUES (?,?,?)", (spotify_track_id, am_song_id or "", via))


def spotify_for_am_song(am_song_id):
    with get_db() as conn:
        row = conn.execute("SELECT spotify_track_id FROM am_links WHERE am_song_id=?",
                           (am_song_id,)).fetchone()
        return row[0] if row else None


def remove_am_pending(spotify_track_id):
    """Bir sarkinin tum listelerdeki bekleyen kayitlarini sil; silinen satirlari dondur."""
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM am_pending WHERE spotify_track_id=?", (spotify_track_id,))]
        conn.execute("DELETE FROM am_pending WHERE spotify_track_id=?", (spotify_track_id,))
        return rows


def clear_am_none(spotify_track_id):
    """'Apple'da yok' kararini sil. Silindiyse True."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM am_links WHERE spotify_track_id=? AND via='none'",
                           (spotify_track_id,))
        return cur.rowcount > 0


# --- Ters yon (Apple -> Spotify) ---

def rev_seen(am_playlist_id):
    with get_db() as conn:
        return {r["lib_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM am_rev_seen WHERE am_playlist_id=?", (am_playlist_id,))}


def set_rev_seen(am_playlist_id, items):
    with get_db() as conn:
        conn.execute("DELETE FROM am_rev_seen WHERE am_playlist_id=?", (am_playlist_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO am_rev_seen VALUES (?,?,?,?,?)",
            [(am_playlist_id, i["lib_id"], i.get("catalog_id"), i["title"], i["artist"])
             for i in items])


def add_rev_pending(am_playlist_id, item, reason, candidates_json):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO am_rev_pending (am_playlist_id, lib_id, catalog_id, title, "
            "artist, reason, candidates_json) VALUES (?,?,?,?,?,?,?)",
            (am_playlist_id, item["lib_id"], item.get("catalog_id"), item["title"],
             item["artist"], reason, candidates_json))


def get_rev_pending():
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT p.*, a.name AS playlist_name, a.spotify_playlist_id FROM am_rev_pending p "
            "LEFT JOIN am_playlists a ON a.am_playlist_id=p.am_playlist_id ORDER BY created_at")]


def remove_rev_pending(am_playlist_id, lib_id):
    with get_db() as conn:
        conn.execute("DELETE FROM am_rev_pending WHERE am_playlist_id=? AND lib_id=?",
                     (am_playlist_id, lib_id))


def add_rev_removed(spotify_playlist_id, spotify_track_id, playlist_name, title, artist):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO am_rev_removed (spotify_playlist_id, spotify_track_id, "
            "playlist_name, title, artist) VALUES (?,?,?,?,?)",
            (spotify_playlist_id, spotify_track_id, playlist_name, title, artist))


def get_rev_removed(status="bekliyor"):
    with get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM am_rev_removed WHERE status=? ORDER BY id", (status,))]


def set_rev_removed_status(removed_id, status):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM am_rev_removed WHERE id=?", (removed_id,)).fetchone()
        conn.execute("UPDATE am_rev_removed SET status=? WHERE id=?", (status, removed_id))
        return dict(row) if row else None
