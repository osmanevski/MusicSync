"""
Spotify -> Apple Music senkronu (yalniz ekleme).

YouTube kolundan (sync.py) bagimsizdir: kendi tablolari (am_*) vardir, onun
fark durumuna dokunmaz. Apple'in genel API'si kitaplik listesinden silemedigi ve
siralayamadigi icin bu kol:
  - Spotify listesinde olup Apple listesine henuz islenmemis sarkilari ekler,
  - Spotify'dan silinen sarkiyi Apple'da birakir,
  - ilk dolumda Spotify sirasini korur; sonraki yeniler listenin SONUNA eklenir.

Eslestirme once ISRC ile (kesin), bulunamazsa katalog aramasi + matcher skoru ile.
Esik altindakiler am_pending'e gider, eklenmez.

Kullanim:
  python am_sync.py --dry-run               -> onizleme (kullanici anahtari gerekmez)
  python am_sync.py                         -> watchlist'teki listeler
  python am_sync.py --playlist <spotify_id> -> tek liste
  python am_sync.py --retry-pending         -> bekleyenleri yeniden dene
  python am_sync.py --pending               -> bekleyenleri listele
"""
import sys
import json
import time
import argparse

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
import db
import matcher
import spotify_client as spc
import applemusic_client as amc
import telegram_notify
from sync import read_watchlist

AM_WATCHLIST_FILE = "am_watchlist.txt"   # yalniz Apple'a giden ek listeler (YouTube'a gitmez)


def read_am_targets():
    """Apple kolunun izledigi Spotify liste id'leri: ortak watchlist + am_watchlist.txt."""
    ids = list(read_watchlist())
    try:
        with open(AM_WATCHLIST_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and line not in ids:
                    ids.append(line)
    except FileNotFoundError:
        pass
    return ids

# ISRC eslesse de bariz tutarsizlik varsa yazma, incelemeye gonder
ISRC_MAX_DURATION_DIFF = 10   # saniye
LIBRARY_WAIT_SEC = 300        # kitapliga eklenen sarkilarin dusmesi icin en uzun bekleme
ISRC_MIN_NAME_SIM = 0.35      # isim ve sanatci ikisi birden bunun altindaysa supheli


def isrc_doubt(track, cand):
    """ISRC eslesmesinde suphe sebebi; temizse bos string."""
    dur = abs((track.get("duration_sec") or 0) - (cand.get("duration_sec") or 0))
    if track.get("duration_sec") and cand.get("duration_sec") and dur > ISRC_MAX_DURATION_DIFF:
        return f"ISRC ayni ama sure farki {dur} sn"
    if (matcher.similarity(track["name"], cand["title"]) < ISRC_MIN_NAME_SIM and
            matcher.similarity(track["artists"], cand["artists"]) < ISRC_MIN_NAME_SIM):
        return "ISRC ayni ama isim ve sanatci tutmuyor"
    return ""


def resolve(am, tracks):
    """Spotify sarkilarini Apple katalog id'sine cevir.

    Sira: once am_links (ISRC ya da Osman'in onayi), sonra ISRC. ISRC'siz ya da
    Apple'da ISRC'si bulunmayan sarki EKLENMEZ: en iyi tahmin ve adaylarla
    incelemeye gider. Apple'a yanlis eklenen sarki API ile geri alinamadigi icin
    tahminler Osman onaylamadan yazilmaz.
    Donen: (cozulen [(track, song_id, via)], bekleyen [(track, scored, sebep)], yok_sayisi)
    """
    resolved, pending, none = [], [], 0
    rest = []
    for t in tracks:
        link = db.get_am_link(t["id"])
        if link is None:
            rest.append(t)
        elif link["am_song_id"]:
            resolved.append((t, link["am_song_id"], link["via"]))
        else:
            none += 1  # Osman: Apple'da yok

    by_isrc = am.songs_by_isrc([t["isrc"] for t in rest if t.get("isrc")])
    for t in rest:
        cands = by_isrc.get(t.get("isrc") or "")
        if cands:
            # Ayni kodlu birden cok surumden en uygununu sec, sonra saglamasini yap
            best, _, scored = matcher.best_match(t, cands)
            doubt = isrc_doubt(t, best)
            if doubt:
                pending.append((t, scored, doubt))
            else:
                resolved.append((t, best["am_id"], "isrc"))
            continue
        try:
            cands = am.search_songs(t["name"], t["artists"])
        except amc.AuthError:
            raise
        except Exception as e:
            print(f"  ! Arama hatasi: {t['artists']} - {t['name']}: {e}")
            cands = []
        _, _, scored = matcher.best_match(t, cands)
        pending.append((t, scored, "ISRC yok" if not t.get("isrc") else
                        "ISRC Apple'da bulunamadi"))
    return resolved, pending, none


def sync_playlist(am, sp, pl, library=None, dry_run=False, retry_pending=False):
    name = pl["name"]
    tracks = spc.get_tracks(sp, pl["id"])
    am_pid = db.get_am_playlist(pl["id"])
    existing = set()
    adopted = False

    if am_pid:
        existing = db.am_song_ids(am_pid)
    elif library is not None:
        # Ayni adli, duzenlenebilir bir Apple listesi varsa onu benimse; icindekileri atla
        match = next((p for p in library if p["name"] == name and p["can_edit"]), None)
        if match:
            am_pid, adopted = match["id"], True
            existing = am.playlist_catalog_ids(am_pid)

    done = db.am_done_ids(am_pid, include_pending=not retry_pending) if am_pid else set()
    todo, seen_sp = [], set()
    for t in tracks:  # Spotify sirasi (en yeni en ustte)
        if t["id"] in done or t["id"] in seen_sp:
            continue
        seen_sp.add(t["id"])
        todo.append(t)

    if not todo:
        print(f"[ATLA] '{name}' guncel.")
        return {"name": name, "added": 0, "pending": 0, "isrc": 0, "manual": 0, "none": 0}

    tag = "[KURU] " if dry_run else ""
    print(f"\n{tag}[APPLE] '{name}': {len(todo)} islenecek sarki"
          + (" (var olan Apple listesi benimsendi)" if adopted else ""))
    resolved, pending, none = resolve(am, todo)

    seen = set(existing)
    plan = []  # [(track, song_id, via, eklenecek_mi)]
    for t, sid, via in resolved:
        plan.append((t, sid, via, sid not in seen))
        seen.add(sid)
    to_add = [p for p in plan if p[3]]
    stats = {"name": name, "added": len(to_add), "pending": len(pending),
             "isrc": sum(1 for p in plan if p[2] == "isrc"),
             "manual": sum(1 for p in plan if p[2] == "manual"), "none": none}

    if dry_run:
        for t, scored, why in pending[:10]:
            best = f"{scored[0][0]['artists']} - {scored[0][0]['title']} ({scored[0][1]})" \
                if scored else "aday yok"
            print(f"  ? [{why}] {t['artists']} - {t['name']}  -> {best}")
        if len(pending) > 10:
            print(f"  ? ... ve {len(pending) - 10} tane daha")
        print(f"  -> [KURU] Eklenecek: {stats['added']} (ISRC {stats['isrc']}, "
              f"onayli {stats['manual']}) | Inceleme: {stats['pending']} | "
              f"Zaten var: {len(plan) - len(to_add)}")
        return stats

    if not am_pid:
        am_pid = am.create_playlist(name)
        print(f"  (Apple'da '{name}' olusturuldu)")
    db.set_am_playlist(pl["id"], am_pid, name)

    for t, sid, via in resolved:
        if via == "isrc":
            db.set_am_link(t["id"], sid, "isrc")
    # Zaten listede olanlar: yalniz kayda gecir
    db.mark_am_synced([(t["id"], am_pid, sid, t["name"], t["artists"], "existing")
                       for t, sid, via, add in plan if not add])
    # Once kitapliga: kitaplikta olmayan sarki listeye arkadan dusup sirayi bozuyor
    if to_add:
        ids = [sid for _, sid, _, _ in to_add]
        am.add_to_library(ids)
        left = am.wait_in_library(ids, timeout=LIBRARY_WAIT_SEC)
        if left:
            print(f"  ! {len(left)} sarki {LIBRARY_WAIT_SEC} sn icinde kitapliga dusmedi; "
                  "liste sirasi bozulabilir")
    # Parca parca ekle ve her parcayi hemen kaydet: yarida kesilirse tekrar eklenmez
    for i in range(0, len(to_add), amc.ADD_BATCH):
        chunk = to_add[i:i + amc.ADD_BATCH]
        am.add_tracks(am_pid, [sid for _, sid, _, _ in chunk])
        db.mark_am_synced([(t["id"], am_pid, sid, t["name"], t["artists"], via)
                           for t, sid, via, _ in chunk])
    for t, scored, why in pending:
        db.add_am_pending(t["id"], am_pid, t["name"], t["artists"], json.dumps(
            [{"am_id": c["am_id"], "title": c["title"], "artists": c["artists"],
              "duration_sec": c["duration_sec"], "isrc": c.get("isrc", ""), "score": s}
             for c, s in scored[:3]], ensure_ascii=False),
            reason=why, spotify_duration=t.get("duration_sec"), isrc=t.get("isrc", ""))
    print(f"  -> Eklendi: {stats['added']} | Supheli: {stats['pending']}")
    return stats


# --- Inceleme: Osman'in onayi ---

def review_queue():
    """Bekleyen sarkilar, sarki basina tek satir (ayni sarki birden cok listede olabilir)."""
    seen, out = set(), []
    for r in db.get_am_pending():
        if r["spotify_track_id"] in seen:
            continue
        seen.add(r["spotify_track_id"])
        r["candidates"] = json.loads(r.get("candidates_json") or "[]")
        out.append(r)
    return out


def approve(am, spotify_track_id, am_song_id):
    """Osman'in sectigi Apple sarkisini 'ayni sarki' say: baglantiyi yaz ve bu sarkinin
    beklendigi tum Apple listelerine ekle. Eklenen liste sayisini dondurur."""
    db.set_am_link(spotify_track_id, am_song_id, "manual")
    rows = db.remove_am_pending(spotify_track_id)
    n = 0
    for r in rows:
        pid = r["am_playlist_id"]
        if am_song_id not in db.am_song_ids(pid):
            am.add_to_library([am_song_id])
            am.add_tracks(pid, [am_song_id])
            n += 1
        db.mark_am_synced([(spotify_track_id, pid, am_song_id, r["track_name"],
                            r["artist_name"], "manual")])
    return n


def reject(spotify_track_id):
    """Apple'da karsiligi yok: bir daha onerilmez, eklenmez."""
    db.set_am_link(spotify_track_id, "", "none")
    db.remove_am_pending(spotify_track_id)


def _summary(results):
    changed = [r for r in results if r["added"] or r["pending"]]
    if not changed:
        return ("<b>🍎 Spotify → Apple Music</b>\n\n"
                f"Calisti. {len(results)} liste kontrol edildi, degisiklik yok.")
    lines = ["<b>🍎 Spotify → Apple Music</b>", ""]
    for r in changed:
        parts = []
        if r["added"]:
            parts.append(f"+{r['added']} eklendi")
        if r["pending"]:
            parts.append(f"?{r['pending']} incelemede")
        lines.append(f"• <b>{r['name']}</b>: " + ", ".join(parts))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--playlist")
    ap.add_argument("--retry-pending", action="store_true")
    ap.add_argument("--pending", action="store_true")
    ap.add_argument("--review", action="store_true", help="Bekleyenleri Mac'te tek tek onayla")
    # Ozan (yapay zeka) icin makine okunur komutlar
    ap.add_argument("--queue-json", action="store_true", help="Bekleyenleri JSON yaz")
    ap.add_argument("--search", metavar="TERIM", help="Apple katalogunda ara, JSON yaz")
    ap.add_argument("--approve", nargs=2, metavar=("SPOTIFY_ID", "APPLE_ID"))
    ap.add_argument("--reject", metavar="SPOTIFY_ID", help="Apple'da karsiligi yok")
    ap.add_argument("--unreject", metavar="SPOTIFY_ID",
                    help="'yok' kararini geri al; sarki sonraki turda yeniden aranir")
    args = ap.parse_args()

    db.init_db()
    if args.pending:
        for r in review_queue():
            best = r["candidates"][0] if r["candidates"] else None
            guess = f"{best['artists']} - {best['title']} ({best['score']})" if best else "aday yok"
            print(f"{r['artist_name']} - {r['track_name']}  ->  {guess}")
        return
    if args.review:
        return _cli_review()
    if args.queue_json:
        out = [{k: r.get(k) for k in ("spotify_track_id", "track_name", "artist_name",
                                      "spotify_duration", "isrc", "reason", "candidates")}
               for r in review_queue()]
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    if args.search:
        am = amc.Client(amc.make_developer_token())
        print(json.dumps(am.search_songs(args.search, "", limit=10), ensure_ascii=False, indent=1))
        return
    if args.approve:
        n = approve(amc.get_client(), args.approve[0], args.approve[1])
        print(f"onaylandi, {n} listeye eklendi")
        return
    if args.unreject:
        print("geri alindi" if db.clear_am_none(args.unreject) else "yok kaydi bulunamadi")
        return
    if args.reject:
        reject(args.reject)
        print("Apple'da yok olarak isaretlendi")
        return

    user = amc.read_user_token()
    if not user and not args.dry_run:
        raise SystemExit("Apple Music kullanici anahtari yok: Mac'te `python am_token.py`.")

    try:
        am = amc.Client(amc.make_developer_token(), user)
        library = None
        if user:
            am.detect_storefront()
            library = am.library_playlists()

        sp = spc.get_client()
        playlists = spc.list_playlists(sp)
        if args.playlist:
            targets = [p for p in playlists if p["id"] == args.playlist]
        else:
            ids = read_am_targets()
            targets = [p for p in playlists if p["id"] in ids]
        if not targets:
            print("Senkronlanacak liste yok.")
            return

        results = []
        for pl in targets:
            r = sync_playlist(am, sp, pl, library=library, dry_run=args.dry_run,
                              retry_pending=args.retry_pending)
            results.append(r)
    except amc.AuthError as e:
        print(f"! Apple Music yetki hatasi: {e}")
        if not args.dry_run:
            telegram_notify.send(
                "<b>🍎 Apple Music senkronu durdu</b>\n\nAnahtar gecersiz ya da suresi "
                "dolmus. Mac'te <code>python am_token.py</code> calistirip yeni anahtari "
                "sunucuya kopyala.")
        raise SystemExit(1)

    if args.dry_run:
        tot = {k: sum(r[k] for r in results) for k in ("added", "pending", "isrc", "manual")}
        print(f"\n[KURU] Toplam: eklenecek {tot['added']} (ISRC {tot['isrc']}, "
              f"onayli {tot['manual']}), incelemeye {tot['pending']}. Hicbir sey yazilmadi.")
    else:
        telegram_notify.send(_summary(results))


def _cli_review():
    am = amc.get_client()
    q = review_queue()
    print(f"{len(q)} sarki inceleme bekliyor.")
    for i, r in enumerate(q, 1):
        print("=" * 60)
        print(f"{i}/{len(q)}  SPOTIFY: {r['artist_name']} - {r['track_name']}")
        for j, c in enumerate(r["candidates"], 1):
            print(f"  [{j}] {c['artists']} - {c['title']} ({c['duration_sec']}s) skor={c['score']}")
        ch = input("Secim (numara / s=atla / x=Apple'da yok / q=cik): ").strip().lower()
        if ch == "q":
            break
        if ch == "x":
            reject(r["spotify_track_id"])
        elif ch.isdigit() and 1 <= int(ch) <= len(r["candidates"]):
            n = approve(am, r["spotify_track_id"], r["candidates"][int(ch) - 1]["am_id"])
            print(f"  Onaylandi, {n} listeye eklendi.")


if __name__ == "__main__":
    main()
