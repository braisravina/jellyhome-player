"""An HTTP source is played directly only when the server allowed Direct Play.

`Video._get_url_from_source` used to take the direct path when the source was
``Protocol: Http`` *or* ``SupportsDirectPlay`` was true. So the protocol alone
was enough, and an explicit refusal from the server was ignored.

Measured against a throw-away Jellyfin 10.11.8 (Phase 2A, 2026-11-25): with
the viewer's ``ForceRemoteSourceTranscoding`` policy on, an HTTP ``.strm``
came back from PlaybackInfo as the ``REFUSED`` source below. With the opt-in
``direct_paths`` enabled and the server either trusted or
``remote_direct_paths`` on, the client opened the origin URL directly. The
transcode the administrator had required never happened.

What stays the same:

- An HTTP source the server *does* allow (``SupportsDirectPlay: true``, which
  is what the same ``.strm`` measured without that policy) still plays
  directly when ``direct_paths`` is on.
- ``direct_paths`` stays opt-in. With it off, nothing here changes.
- The server's transcoding URL is used exactly as issued.
- A *Local* Connection Type is about bandwidth. It never grants the trust a
  direct path needs.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest
from types import SimpleNamespace as NS
from unittest import mock

from jellyfin_mpv_shim import network_locality
from jellyfin_mpv_shim.media import Video

SERVER = "https://jellyfin.example.invalid"
ORIGIN = "http://origin.example.invalid/low.mkv"
TRANSCODING_URL = ("/videos/f1fe/master.m3u8?MediaSourceId=src&VideoCodec=h264"
                   "&AudioCodec=aac&VideoBitrate=2808000&AudioBitrate=192000"
                   "&TranscodeReasons=DirectPlayError"
                   "&allowVideoStreamCopy=false&allowAudioStreamCopy=false")

#: PlaybackInfo's answer under ForceRemoteSourceTranscoding (measured shape).
REFUSED = {"Id": "src", "Protocol": "Http", "IsRemote": True, "Path": ORIGIN,
           "SupportsDirectPlay": False, "SupportsDirectStream": False,
           "SupportsTranscoding": True, "TranscodingUrl": TRANSCODING_URL,
           "MediaStreams": [{"Index": 0, "Type": "Video", "Codec": "h264"},
                            {"Index": 1, "Type": "Audio", "Codec": "aac"}]}

#: The same .strm without that policy (measured: Direct Play allowed).
ALLOWED = dict(REFUSED, SupportsDirectPlay=True, SupportsDirectStream=True,
               TranscodingUrl=None)


def _video(source, trusted):
    v = Video.__new__(Video)
    v.item_id = "f1fe"
    v.media_source = dict(source)
    v.parent = NS(direct_path_trusted=trusted)
    v.aid = None
    v.auth_via_header = True
    v.client = NS(config=NS(data={"auth.server": SERVER, "auth.token": "t"}))
    return v


def _url(source, trusted, direct_paths, remote_direct_paths=False):
    v = _video(source, trusted)
    with mock.patch.multiple("jellyfin_mpv_shim.media.settings",
                             direct_paths=direct_paths,
                             remote_direct_paths=remote_direct_paths,
                             path_substitutions=[]):
        url = v._get_url_from_source()
    return v, url


class ServerRefusalTest(unittest.TestCase):
    """The regression: each of these opened ORIGIN before the fix."""

    def test_a_trusted_server_s_refusal_is_respected(self):
        v, url = _url(REFUSED, trusted=True, direct_paths=True)
        self.assertEqual(url, SERVER + TRANSCODING_URL)
        self.assertTrue(v.is_transcode)
        self.assertFalse(v.direct_path)

    def test_remote_direct_paths_does_not_override_it_either(self):
        v, url = _url(REFUSED, trusted=False, direct_paths=True,
                      remote_direct_paths=True)
        self.assertEqual(url, SERVER + TRANSCODING_URL)
        self.assertTrue(v.is_transcode)

    def test_the_transcoding_url_is_used_exactly_as_issued(self):
        """Not rebuilt, not edited: the server measured as trusting every
        parameter in it, so the client must not change any."""
        _v, url = _url(REFUSED, trusted=True, direct_paths=True)
        self.assertEqual(url, SERVER + TRANSCODING_URL)
        self.assertIn("allowVideoStreamCopy=false", url)
        self.assertIn("VideoBitrate=2808000", url)

    def test_a_source_that_does_not_say_is_not_played_directly(self):
        """No SupportsDirectPlay at all fails closed, to the server's own
        routes, rather than reading the protocol as permission."""
        source = {k: v for k, v in REFUSED.items()
                  if k != "SupportsDirectPlay"}
        v, url = _url(source, trusted=True, direct_paths=True)
        self.assertEqual(url, SERVER + TRANSCODING_URL)
        self.assertFalse(v.direct_path)

    def test_a_refused_file_path_is_unchanged(self):
        """File sources already needed SupportsDirectPlay; still do."""
        source = dict(REFUSED, Protocol="File", Path="/srv/media/film.mkv")
        v, url = _url(source, trusted=True, direct_paths=True)
        self.assertEqual(url, SERVER + TRANSCODING_URL)


class LegitimateDirectPlayTest(unittest.TestCase):
    def test_an_allowed_http_source_still_plays_directly(self):
        v, url = _url(ALLOWED, trusted=True, direct_paths=True)
        self.assertEqual(url, ORIGIN)
        self.assertFalse(v.is_transcode)
        self.assertTrue(v.direct_path)

    def test_and_with_remote_direct_paths(self):
        _v, url = _url(ALLOWED, trusted=False, direct_paths=True,
                       remote_direct_paths=True)
        self.assertEqual(url, ORIGIN)

    def test_direct_paths_stays_opt_in(self):
        """Off (the default): the server's static stream, as before."""
        v, url = _url(ALLOWED, trusted=True, direct_paths=False)
        self.assertTrue(url.startswith(SERVER + "/Videos/f1fe/stream?"))
        self.assertIn("static=true", url)
        self.assertFalse(v.direct_path)

    def test_untrusted_without_remote_direct_paths_uses_the_server(self):
        _v, url = _url(ALLOWED, trusted=False, direct_paths=True)
        self.assertTrue(url.startswith(SERVER + "/Videos/f1fe/stream?"))

    def test_static_stream_needs_the_server_s_permission_too(self):
        """Unchanged: with direct streaming refused as well, the transcode."""
        _v, url = _url(REFUSED, trusted=False, direct_paths=False)
        self.assertEqual(url, SERVER + TRANSCODING_URL)


class LocalOverrideGrantsNoTrustTest(unittest.TestCase):
    """Choosing *Local* lifts the bandwidth cap; it is not a trust grant."""

    def test_local_override_keeps_auto_s_trust(self):
        untrusted = network_locality.Locality(False, False, "public-address")
        loc = network_locality.apply_override(network_locality.LOCAL,
                                              untrusted)
        self.assertTrue(loc.local)
        self.assertFalse(loc.direct_path_trusted)
        _v, url = _url(ALLOWED, trusted=loc.direct_path_trusted,
                       direct_paths=True)
        self.assertTrue(url.startswith(SERVER + "/Videos/f1fe/stream?"))

    def test_remote_override_removes_trust(self):
        trusted = network_locality.Locality(True, True, "private-address")
        loc = network_locality.apply_override(network_locality.REMOTE,
                                              trusted)
        self.assertFalse(loc.direct_path_trusted)


if __name__ == "__main__":
    unittest.main()
