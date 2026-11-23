import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.modules.setdefault("spotipy", MagicMock())
sys.modules.setdefault("spotipy.oauth2", MagicMock())
sys.modules.setdefault("ytmusicapi", MagicMock())
_dotenv = MagicMock()
_dotenv.load_dotenv = MagicMock()
sys.modules.setdefault("dotenv", _dotenv)

import config
import db
import am_reverse
import am_sync


def item(lib, cid, title, artist="A", dur=200):
    return {"lib_id": lib, "catalog_id": cid, "title": title, "artist": artist, "duration_sec": dur}


def sptr(i, name, artists="A", isrc="", dur=200):
    return {"id": i, "name": name, "artists": artists, "duration_sec": dur, "isrc": isrc, "pos": 0}


class TestReverse(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = config.DB_PATH
        config.DB_PATH = os.path.join(self.tmp.name, "t.db")
        db.init_db()
        db.set_am_playlist("sp1", "p.1", "37")
        db.mark_am_synced([("s1", "p.1", "c1", "Bir", "A", "isrc")])
        self.am = MagicMock()
        self.sp = MagicMock()
        self.sp.search.return_value = {"tracks": {"items": []}}
        self.spotify = [sptr("s1", "Bir", isrc="I1")]
        self._saved = (am_reverse.spc.get_tracks, am_reverse.spc.search_tracks,
                       getattr(am_reverse.spc, "remove_from_playlist", None),
                       am_reverse.sort_playlists.read_sort_ids, am_reverse.sort_playlists.is_protected)
        am_reverse.spc.get_tracks = MagicMock(side_effect=lambda sp, pid: list(self.spotify))
        am_reverse.spc.search_tracks = MagicMock(return_value=[])
        am_reverse.sort_playlists.read_sort_ids = MagicMock(return_value={"sp1"})
        am_reverse.sort_playlists.is_protected = MagicMock(return_value=False)
        self.am.isrc_of.return_value = {}

    def tearDown(self):
        (am_reverse.spc.get_tracks, am_reverse.spc.search_tracks, rm,
         am_reverse.sort_playlists.read_sort_ids, am_reverse.sort_playlists.is_protected) = self._saved
        if rm is not None:
            am_reverse.spc.remove_from_playlist = rm
        config.DB_PATH = self.old
        self.tmp.cleanup()

    def run_once(self, items):
        self.am.playlist_items.return_value = items
        return am_reverse.reverse_playlist(self.am, self.sp, "sp1", "p.1", "37")

    def test_first_run_is_baseline_only(self):
        r = self.run_once([item("i1", "c1", "Bir"), item("i9", "c9", "Dokuz")])
        self.assertTrue(r["baseline"])
        self.sp.playlist_add_items.assert_not_called()

    def test_new_apple_song_goes_to_spotify_top_via_isrc(self):
        self.run_once([item("i1", "c1", "Bir")])
        self.am.isrc_of.return_value = {"c2": "I2"}
        self.sp.search.return_value = {"tracks": {"items": [
            {"id": "s2", "name": "Iki", "duration_ms": 201000, "artists": [{"name": "A"}]}]}}
        r = self.run_once([item("i1", "c1", "Bir"), item("i2", "c2", "Iki")])
        self.assertEqual(r["added"], 1)
        self.sp.playlist_add_items.assert_called_once_with("sp1", ["spotify:track:s2"], position=0)
        self.assertEqual(db.get_am_link("s2"), {"am_song_id": "c2", "via": "apple"})
        # Ileri yon bu sarkiyi Apple'a geri eklemez
        self.assertIn("s2", db.am_done_ids("p.1"))

    def test_our_own_forward_adds_are_absorbed(self):
        self.run_once([item("i1", "c1", "Bir")])
        db.mark_am_synced([("s3", "p.1", "c3", "Uc", "A", "isrc")])
        r = self.run_once([item("i1", "c1", "Bir"), item("i3", "c3", "Uc")])
        self.assertEqual((r["added"], r["pending"]), (0, 0))
        self.sp.playlist_add_items.assert_not_called()

    def test_song_already_on_spotify_by_name_is_not_added(self):
        self.spotify.append(sptr("s4", "Dort"))
        self.run_once([item("i1", "c1", "Bir")])
        r = self.run_once([item("i1", "c1", "Bir"), item("i4", None, "Dort")])
        self.assertEqual(r["added"], 0)
        self.sp.playlist_add_items.assert_not_called()

    def test_unmatched_goes_to_pending_and_can_be_approved(self):
        self.run_once([item("i1", "c1", "Bir")])
        r = self.run_once([item("i1", "c1", "Bir"), item("i5", "c5", "Bes")])
        self.assertEqual(r["pending"], 1)
        self.sp.playlist_add_items.assert_not_called()
        msg = am_sync.rev_approve(self.sp, "p.1", "i5", "s5")
        self.assertIn("eklendi", msg)
        self.sp.playlist_add_items.assert_called_once_with("sp1", ["spotify:track:s5"], position=0)
        self.assertEqual(db.get_rev_pending(), [])

    def test_removed_in_apple_waits_for_confirmation(self):
        self.run_once([item("i1", "c1", "Bir")])
        r = self.run_once([])
        self.assertEqual(r["removed"], 1)
        am_reverse.spc.remove_from_playlist = MagicMock()
        rows = db.get_rev_removed()
        self.assertEqual([x["spotify_track_id"] for x in rows], ["s1"])
        am_reverse.spc.remove_from_playlist.assert_not_called()
        am_reverse.confirm_removal(self.sp, rows[0]["id"])
        am_reverse.spc.remove_from_playlist.assert_called_once_with(self.sp, "sp1", "s1")
        self.assertEqual(db.get_rev_removed(), [])

    def test_mass_disappearance_is_not_recorded(self):
        many = [item(f"i{n}", f"c{n}", f"S{n}") for n in range(20)]
        self.run_once(many)
        r = self.run_once([])
        self.assertEqual(r["removed"], 0)
        self.assertEqual(db.get_rev_removed(), [])

    def test_personal_list_appends_at_end(self):
        am_reverse.sort_playlists.read_sort_ids.return_value = set()
        self.run_once([item("i1", "c1", "Bir")])
        db.set_am_link("s6", "c6", "manual")
        self.run_once([item("i1", "c1", "Bir"), item("i6", "c6", "Alti")])
        self.sp.playlist_add_items.assert_called_once_with("sp1", ["spotify:track:s6"])


if __name__ == "__main__":
    unittest.main()
