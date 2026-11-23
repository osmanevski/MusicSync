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
import am_sync
import applemusic_client as amc


def T(i, name, artists="A", dur=200, isrc=""):
    return {"id": i, "name": name, "artists": artists, "duration_sec": dur, "isrc": isrc}


def C(am_id, title, artists="A", dur=200, isrc=""):
    return {"am_id": am_id, "title": title, "artists": artists, "duration_sec": dur,
            "isrc": isrc, "is_music": True}


class Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code = code
        self._body = body
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class TestClient(unittest.TestCase):
    def test_isrc_lookup_batches_by_25_and_keys_upper(self):
        http = MagicMock()
        http.request.side_effect = [
            Resp(200, {"data": [{"id": "1", "attributes": {"name": "S", "isrc": "tr1"}}]}),
            Resp(200, {"data": []}),
        ]
        c = amc.Client("dev", session=http, storefront="tr")
        out = c.songs_by_isrc([f"I{n}" for n in range(30)])
        self.assertEqual(http.request.call_count, 2)
        self.assertEqual(out["TR1"][0]["am_id"], "1")

    def test_persistent_401_raises_auth_error(self):
        http = MagicMock()
        http.request.return_value = Resp(401, headers={"Retry-After": "0"})
        c = amc.Client("dev", "user", session=http)
        with self.assertRaises(amc.AuthError):
            c.library_playlists()

    def test_flaky_401_is_retried(self):
        http = MagicMock()
        http.request.side_effect = [Resp(401, headers={"Retry-After": "0"}),
                                    Resp(200, {"data": []})]
        c = amc.Client("dev", session=http)
        self.assertEqual(c.songs_by_isrc(["X"]), {})

    def test_429_retries(self):
        http = MagicMock()
        http.request.side_effect = [Resp(429, headers={"Retry-After": "0"}),
                                    Resp(200, {"data": []})]
        c = amc.Client("dev", session=http)
        self.assertEqual(c.songs_by_isrc(["X"]), {})
        self.assertEqual(http.request.call_count, 2)

    def test_next_page_link_is_not_double_prefixed(self):
        http = MagicMock()
        http.request.side_effect = [
            Resp(200, {"data": [{"id": "p1"}], "next": "/v1/me/library/playlists?offset=100"}),
            Resp(200, {"data": [{"id": "p2"}]}),
        ]
        c = amc.Client("dev", "user", session=http)
        self.assertEqual([p["id"] for p in c.library_playlists()], ["p1", "p2"])
        self.assertEqual(http.request.call_args_list[1][0][1],
                         "https://api.music.apple.com/v1/me/library/playlists?offset=100")

    def test_user_call_without_token_refuses(self):
        c = amc.Client("dev", None, session=MagicMock())
        with self.assertRaises(amc.AuthError):
            c.create_playlist("x")


class TestAmSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = config.DB_PATH
        config.DB_PATH = os.path.join(self.tmp.name, "t.db")
        db.init_db()
        self.pl = {"id": "sp1", "name": "Liste"}
        self.am = MagicMock()
        self.am.songs_by_isrc.return_value = {"I1": [C("s1", "Bir", isrc="I1")]}
        self.am.search_songs.side_effect = lambda name, artists: (
            [C("s2", "Iki")] if name == "Iki" else [C("zz", "Tamamen Baska", "B", 40)])
        self.am.create_playlist.return_value = "p.new"
        am_sync.LIBRARY_SETTLE_SEC = 0
        self.tracks = [T("t1", "Bir", isrc="I1"), T("t2", "Iki"), T("t3", "Uc")]
        am_sync.spc.get_tracks = MagicMock(return_value=self.tracks)

    def tearDown(self):
        config.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        r = am_sync.sync_playlist(self.am, None, self.pl, library=None, dry_run=True)
        self.assertEqual((r["added"], r["pending"], r["isrc"]), (1, 2, 1))
        self.am.create_playlist.assert_not_called()
        self.am.add_tracks.assert_not_called()
        self.assertIsNone(db.get_am_playlist("sp1"))
        self.assertIsNone(db.get_am_link("t1"))

    def test_only_isrc_is_written_guesses_wait_for_review(self):
        r = am_sync.sync_playlist(self.am, None, self.pl, library=[])
        self.assertEqual((r["added"], r["pending"]), (1, 2))
        self.am.add_tracks.assert_called_once_with("p.new", ["s1"])
        self.am.add_to_library.assert_called_once_with(["s1"])
        self.assertEqual(db.get_am_link("t1"), {"am_song_id": "s1", "via": "isrc"})
        q = am_sync.review_queue()
        self.assertEqual([x["spotify_track_id"] for x in q], ["t2", "t3"])
        self.assertEqual(q[0]["candidates"][0]["am_id"], "s2")  # en iyi tahmin basta

        # Ikinci tur: bekleyenler yeniden aranmaz, bir sey eklenmez
        self.am.reset_mock()
        r = am_sync.sync_playlist(self.am, None, self.pl, library=[])
        self.assertEqual(r["added"], 0)
        self.am.add_tracks.assert_not_called()
        self.am.search_songs.assert_not_called()

    def test_approve_links_adds_everywhere_and_is_reused(self):
        am_sync.sync_playlist(self.am, None, self.pl, library=[])
        pl2 = {"id": "sp2", "name": "Diger"}
        self.am.create_playlist.return_value = "p.two"
        am_sync.sync_playlist(self.am, None, pl2, library=[])
        self.am.reset_mock()

        n = am_sync.approve(self.am, "t2", "s2")
        self.assertEqual(n, 2)
        self.assertEqual(db.get_am_link("t2"), {"am_song_id": "s2", "via": "manual"})
        self.assertNotIn("t2", [x["spotify_track_id"] for x in am_sync.review_queue()])

        # Yeni bir listede ayni sarki: aramadan, onayli baglantiyla eklenir
        self.am.reset_mock()
        self.am.create_playlist.return_value = "p.three"
        am_sync.spc.get_tracks.return_value = [T("t2", "Iki")]
        r = am_sync.sync_playlist(self.am, None, {"id": "sp3", "name": "Uc"}, library=[])
        self.assertEqual((r["added"], r["manual"]), (1, 1))
        self.am.search_songs.assert_not_called()
        self.am.add_tracks.assert_called_once_with("p.three", ["s2"])

    def test_reject_is_never_proposed_again(self):
        am_sync.sync_playlist(self.am, None, self.pl, library=[])
        am_sync.reject("t3")
        self.assertNotIn("t3", [x["spotify_track_id"] for x in am_sync.review_queue()])
        am_sync.spc.get_tracks.return_value = [T("t3", "Uc")]
        self.am.reset_mock()
        r = am_sync.sync_playlist(self.am, None, {"id": "sp9", "name": "X"}, library=[])
        self.assertEqual((r["added"], r["pending"], r["none"]), (0, 0, 1))
        self.am.search_songs.assert_not_called()

    def test_isrc_match_with_wrong_duration_goes_to_review(self):
        self.am.songs_by_isrc.return_value = {"I1": [C("s1", "Bir", dur=320, isrc="I1")]}
        r = am_sync.sync_playlist(self.am, None, self.pl, library=[])
        self.assertEqual(r["isrc"], 0)
        q = {x["spotify_track_id"]: x for x in am_sync.review_queue()}
        self.assertIn("sure farki", q["t1"]["reason"])
        self.assertIsNone(db.get_am_link("t1"))

    def test_unreject_reopens_track(self):
        am_sync.sync_playlist(self.am, None, self.pl, library=[])
        am_sync.reject("t3")
        self.assertTrue(db.clear_am_none("t3"))
        self.assertIsNone(db.get_am_link("t3"))
        r = am_sync.sync_playlist(self.am, None, self.pl, library=[])
        self.assertEqual(r["pending"], 1)

    def test_adopts_existing_playlist_and_skips_songs_already_there(self):
        self.am.playlist_catalog_ids.return_value = {"s1"}
        lib = [{"id": "p.old", "name": "Liste", "can_edit": True}]
        r = am_sync.sync_playlist(self.am, None, self.pl, library=lib)
        self.am.create_playlist.assert_not_called()
        self.am.add_tracks.assert_not_called()
        self.assertEqual(r["added"], 0)
        self.assertEqual(db.am_song_ids("p.old"), {"s1"})

    def test_two_spotify_tracks_same_apple_song_added_once(self):
        self.am.songs_by_isrc.return_value = {"I1": [C("s1", "Bir", isrc="I1")]}
        am_sync.spc.get_tracks.return_value = [T("t1", "Bir", isrc="I1"),
                                               T("t9", "Bir", isrc="I1")]
        am_sync.sync_playlist(self.am, None, self.pl, library=[])
        self.am.add_tracks.assert_called_once_with("p.new", ["s1"])
        self.assertEqual(db.am_done_ids("p.new"), {"t1", "t9"})

    def test_failed_chunk_keeps_earlier_chunks_recorded(self):
        n = amc.ADD_BATCH + 5
        am_sync.spc.get_tracks.return_value = [T(f"t{i}", f"S{i}", isrc=f"I{i}")
                                               for i in range(n)]
        self.am.songs_by_isrc.return_value = {f"I{i}": [C(f"s{i}", f"S{i}", isrc=f"I{i}")]
                                              for i in range(n)}
        self.am.add_tracks.side_effect = [None, RuntimeError("kesildi")]
        with self.assertRaises(RuntimeError):
            am_sync.sync_playlist(self.am, None, self.pl, library=[])
        self.assertEqual(len(db.am_done_ids("p.new")), amc.ADD_BATCH)


if __name__ == "__main__":
    unittest.main()
