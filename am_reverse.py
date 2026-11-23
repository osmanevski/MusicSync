"""
Apple Music -> Spotify (ters yon). am_sync.py her gece ileri yondan ONCE calistirir.

- Apple listesine Osman'in ekledigi sarki Spotify'daki ayni listeye eklenir: siralanan
  listelerde (sort_watchlist, Firtina haric) en uste, ozel listelerde sona.
- Eslestirme: once am_links (ISRC ya da onayli), sonra Spotify'da isrc: aramasi. Kesin
  olmayan sarki eklenmez, am_rev_pending'e (Ozan/Osman) gider.
- Apple'dan silinen sarki Spotify'dan SILINMEZ: am_rev_removed'a yazilir, Osman onaylarsa
  silinir (sonra YouTube gece senkronu da siler).
- Dongu yok: ileri yonun ekledigi sarkilar am_synced'de; burada "bizim" sayilip yutulur.

Apple degisikligi kitaplik oge id'siyle izlenir (am_rev_seen). Bir liste ilk kez
gorulunce yalniz anlik goruntu alinir, hicbir sey yazilmaz.
"""
import json

import db
import matcher
import spotify_client as spc
import sort_playlists

MAX_REMOVED_PER_LIST = 15   # bir gecede bundan fazla silinme: veri hatasi say, kaydetme
ISRC_MAX_DURATION_DIFF = 10


def _key(title, artist):
    return matcher.track_key(title, artist)


def _float_to_top(sp_playlist_id, name):
    return sp_playlist_id in sort_playlists.read_sort_ids() and not sort_playlists.is_protected(name)


def _search_isrc(sp, isrc):
    try:
        items = sp.search(q=f"isrc:{isrc}", type="track", limit=5)["tracks"]["items"]
    except Exception:
        items = []
    return [{"spotify_id": t["id"], "title": t.get("name") or "",
             "artists": ", ".join(a.get("name") for a in t.get("artists") or [] if a.get("name")),
             "duration_sec": round((t.get("duration_ms") or 0) / 1000), "is_music": True}
            for t in items if t.get("id")]


def reverse_playlist(am, sp, sp_playlist_id, am_playlist_id, name, dry_run=False):
    """Doner: {"name", "added", "pending", "removed", "baseline"}"""
    stats = {"name": name, "added": 0, "pending": 0, "removed": 0, "baseline": False}
    items = am.playlist_items(am_playlist_id)
    seen = db.rev_seen(am_playlist_id)
    if not seen:
        if not dry_run:
            db.set_rev_seen(am_playlist_id, items)
        stats["baseline"] = True
        return stats

    now_ids = {i["lib_id"] for i in items}
    new = [i for i in items if i["lib_id"] not in seen]
    gone = [v for k, v in seen.items() if k not in now_ids]
    if not new and not gone:
        return stats

    sp_tracks = spc.get_tracks(sp, sp_playlist_id)
    sp_ids = {t["id"] for t in sp_tracks}
    sp_isrc = {t["isrc"]: t["id"] for t in sp_tracks if t.get("isrc")}
    sp_keys = {_key(t["name"], t["artists"]): t["id"] for t in sp_tracks}
    ours = db.am_song_ids(am_playlist_id)
    isrcs = am.isrc_of([i["catalog_id"] for i in new if i.get("catalog_id")])
    top = _float_to_top(sp_playlist_id, name)
    add_ids = []

    for i in new:
        cid = i.get("catalog_id")
        # 1) Ileri yonun (ya da onayin) ekledigi sarki: bizim, yut
        if cid and cid in ours:
            continue
        # 2) Spotify listesinde zaten var (ISRC ya da ad+sanatci): yalniz kayda gec
        isrc = isrcs.get(cid or "")
        already = sp_isrc.get(isrc) if isrc else None
        already = already or sp_keys.get(_key(i["title"], i["artist"]))
        if already:
            if cid and not dry_run:
                db.mark_am_synced([(already, am_playlist_id, cid, i["title"], i["artist"], "apple")])
            continue
        # 3) Spotify karsiligini bul
        sid, reason, scored = None, "", []
        link = db.spotify_for_am_song(cid) if cid else None
        if link:
            sid = link
        elif isrc:
            best, _, scored = matcher.best_match(
                {"name": i["title"], "artists": i["artist"], "duration_sec": i["duration_sec"]},
                _search_isrc(sp, isrc))
            if best and abs(best["duration_sec"] - i["duration_sec"]) <= ISRC_MAX_DURATION_DIFF:
                sid = best["spotify_id"]
            else:
                reason = "Spotify'da ISRC yok" if not best else "ISRC ayni ama sure tutmuyor"
        else:
            reason = "Apple katalog kaydi/ISRC yok"
        if not sid:
            if not scored:
                scored = matcher.best_match(
                    {"name": i["title"], "artists": i["artist"], "duration_sec": i["duration_sec"]},
                    spc.search_tracks(sp, i["title"], i["artist"]))[2]
            stats["pending"] += 1
            print(f"  ? [{reason}] {i['artist']} - {i['title']}")
            if not dry_run:
                db.add_rev_pending(am_playlist_id, i, reason, json.dumps(
                    [{"spotify_id": c["spotify_id"], "title": c["title"], "artists": c["artists"],
                      "duration_sec": c["duration_sec"], "score": s} for c, s in scored[:3]],
                    ensure_ascii=False))
            continue
        if sid in sp_ids or sid in add_ids:
            continue
        add_ids.append(sid)
        print(f"  + Apple'dan: {i['artist']} - {i['title']}")
        if not dry_run:
            db.mark_am_synced([(sid, am_playlist_id, cid or "", i["title"], i["artist"], "apple")])
            if cid and not db.get_am_link(sid):
                db.set_am_link(sid, cid, "apple")
    if add_ids and not dry_run:
        uris = [f"spotify:track:{x}" for x in add_ids]
        if top:
            # Apple sona ekler (en yeni sonda); Spotify'da en yeni en ustte olsun
            sp.playlist_add_items(sp_playlist_id, uris[::-1], position=0)
        else:
            sp.playlist_add_items(sp_playlist_id, uris)
    stats["added"] = len(add_ids)

    # Silinenler: yalniz kaydet, Osman onaylarsa Spotify'dan silinir
    if len(gone) > MAX_REMOVED_PER_LIST:
        print(f"  ! '{name}': {len(gone)} oge birden kayboldu; silme kaydi yapilmadi (kontrol et)")
    else:
        by_cid = {}
        with db.get_db() as conn:
            for r in conn.execute("SELECT spotify_track_id, am_song_id FROM am_synced "
                                  "WHERE am_playlist_id=?", (am_playlist_id,)):
                by_cid.setdefault(r[1], r[0])
        for g in gone:
            sid = by_cid.get(g.get("catalog_id") or "") or sp_keys.get(_key(g["title"], g["artist"]))
            if sid and sid in sp_ids:
                stats["removed"] += 1
                print(f"  - Apple'dan silinmis (onay bekliyor): {g['artist']} - {g['title']}")
                if not dry_run:
                    db.add_rev_removed(sp_playlist_id, sid, name, g["title"], g["artist"])
    if not dry_run:
        db.set_rev_seen(am_playlist_id, items)
    return stats


def run(am, sp, targets, dry_run=False):
    """targets: [{id, name}] Spotify listeleri. Apple karsiligi olmayan atlanir."""
    results = []
    for pl in targets:
        am_pid = db.get_am_playlist(pl["id"])
        if not am_pid:
            continue
        r = reverse_playlist(am, sp, pl["id"], am_pid, pl["name"], dry_run=dry_run)
        if r["added"] or r["pending"] or r["removed"]:
            print(f"[APPLE->SPOTIFY] '{pl['name']}': +{r['added']} ?{r['pending']} "
                  f"-{r['removed']} (onay bekliyor)")
        results.append(r)
    return results


def confirm_removal(sp, removed_id, keep=False):
    """Osman'in karari: Spotify'dan sil ya da kalsin."""
    row = db.set_rev_removed_status(removed_id, "kalsin" if keep else "silindi")
    if not row:
        return None
    if not keep:
        spc.remove_from_playlist(sp, row["spotify_playlist_id"], row["spotify_track_id"])
    return row
