"""Bitrate limits reach Jellyfin only as valid, positive, in-range integers.

Measured against a throw-away Jellyfin 10.11.8 (2026-11-25), these are the
values the server treats specially in ``DeviceProfile.MaxStreamingBitrate``:

- ``0`` or negative: **no limit at all**, *and* it defeats the administrator's
  ``RemoteClientBitrateLimit``. The server takes ``min(client, admin)``, so a
  remote viewer under a 3 Mbps limit got Direct Play of a 25 Mbps file. A
  forced transcode at 0 was encoded at **64 kbps** (``-maxrate 64000``).
- omitted: the server's own default of 8 Mbps.
- ``null``: no client limit, and no video bitrate at all on a forced transcode.
- above 2,147,483,647: HTTP 400 for the whole PlaybackInfo request, so
  playback fails.

So "unlimited" is never spelled with any of those. It is the ceiling
2,147,483 kbps, i.e. 2,147,483,000 bps, which is what every local session
already sent. It measured as Direct Play of everything, including a file of
unknown bitrate, while still being capped by every administrator limit
(``min(2147483000, 3000000)`` -- 2,808,000 video + 192,000 audio).

The settings can arrive from the Settings UI, a hand-edited ``conf.json``, a
CLI override, a quality-menu override, or code assigning the attribute
directly. Each route is covered here, and the profile builder normalises again
at the end so that no route can skip the check.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import math
import os
import shutil
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim import bitrate_limits as BL
from jellyfin_mpv_shim import conf
from jellyfin_mpv_shim.conf import Settings, settings

INT32_MAX = 2147483647
UNLIMITED = BL.UNLIMITED_KBPS

#: (input, default, expected). Every shape a value can take on the way in.
NORMALIZE_TABLE = [
    # valid values are preserved exactly
    (10000, 10000, 10000),
    (1, 10000, 1),
    (320, 10000, 320),
    (2147483, 10000, 2147483),
    ("4000", 10000, 4000),
    (" 12 ", 10000, 12),
    ("+15", 10000, 15),
    (3000.0, 10000, 3000),            # an integral JSON number
    # zero or below: Unlimited (approved decision 1)
    (0, 10000, UNLIMITED),
    (-1, 10000, UNLIMITED),
    (-(2 ** 40), 10000, UNLIMITED),
    ("0", 10000, UNLIMITED),
    ("-5", 10000, UNLIMITED),
    (-0.0, 10000, UNLIMITED),
    # above the supported maximum: Unlimited (approved decision 2)
    (2147484, 10000, UNLIMITED),
    (3000000, 10000, UNLIMITED),
    (INT32_MAX, 10000, UNLIMITED),
    (10 ** 30, 10000, UNLIMITED),
    (1e9, 10000, UNLIMITED),
    ("99999999999999999999", 10000, UNLIMITED),
    # malformed: the default (approved decision 4)
    (None, 10000, 10000),
    (True, 10000, 10000),             # a JSON true is not 1 kbps
    (False, 2147483, 2147483),
    ("abc", 10000, 10000),
    ("", 10000, 10000),
    ("12.5", 10000, 10000),
    ("1e9", 10000, 10000),
    ("0x10", 10000, 10000),
    (12.5, 10000, 10000),
    (float("nan"), 10000, 10000),
    (float("inf"), 10000, 10000),
    (float("-inf"), 10000, 10000),
    ([], 10000, 10000),
    ({}, 10000, 10000),
    ([4000], 10000, 10000),
]


class NormalizeTest(unittest.TestCase):
    def test_table(self):
        for value, default, expected in NORMALIZE_TABLE:
            with self.subTest(value=value, default=default):
                got = BL.normalize_kbps(value, default)
                self.assertEqual(got, expected)
                self.assertIs(type(got), int)

    def test_the_result_is_always_in_range(self):
        for value, default, _expected in NORMALIZE_TABLE:
            with self.subTest(value=value):
                got = BL.normalize_kbps(value, default)
                self.assertTrue(BL.MIN_KBPS <= got <= UNLIMITED)

    def test_wire_bps_is_kbps_times_1000_and_never_overflows(self):
        for value, default, expected in NORMALIZE_TABLE:
            with self.subTest(value=value):
                bps = BL.wire_bps(value, default)
                self.assertEqual(bps, expected * 1000)
                self.assertIs(type(bps), int)
                self.assertTrue(1000 <= bps <= 2147483000 < INT32_MAX)

    def test_unlimited_is_the_existing_local_default(self):
        """No new sentinel: the value local sessions already send."""
        self.assertEqual(UNLIMITED, Settings.local_kbps)
        self.assertEqual(BL.wire_bps(UNLIMITED, 10000), 2147483000)

    def test_an_invalid_default_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            BL.normalize_kbps("abc", "also bad")

    def test_ui_input_accepts_only_the_supported_range(self):
        for value in ("1", "320", "10000", " 4000 ", "2147483", 2147483, 1):
            with self.subTest(value=value):
                self.assertTrue(BL.is_valid_input(value))
        for value in ("0", "-1", 0, -5, "2147484", INT32_MAX, "abc", "",
                      "12.5", None, True, False, 12.5, float("nan")):
            with self.subTest(value=value):
                self.assertFalse(BL.is_valid_input(value))


def _model_effective_limit(client_bps, admin_bps, remote):
    """Jellyfin 10.11.8's ``MediaInfoHelper.GetMaxBitrate`` +
    ``StreamBuilder.IsBitrateLimitExceeded``: the limit a decision is made
    against, None meaning no limit. Checked against the measured rows in
    MeasuredReferenceTest below."""
    effective = client_bps
    if admin_bps and admin_bps > 0 and remote:
        effective = min(client_bps if client_bps is not None else admin_bps,
                        admin_bps)
    if effective is None or effective <= 0:
        return None
    return effective


#: The HighRate test file's bitrate as the 10.11.8 server probed it.
HIGH_RATE_BPS = 25274295
DIRECT_PLAY = "DirectPlay"


def _model_decision(client_bps, admin_bps, remote, source_bps=HIGH_RATE_BPS):
    """DIRECT_PLAY, or the limit a transcode is made at."""
    limit = _model_effective_limit(client_bps, admin_bps, remote)
    if limit is None or source_bps <= limit:
        return DIRECT_PLAY
    return limit


class MeasuredReferenceTest(unittest.TestCase):
    """Rows measured on the throw-away 10.11.8 server (Phase 2A).

    ``(client_bps, admin_bps, remote, outcome)`` for the 25.27 Mbps file.
    The outcome is DIRECT_PLAY, or the transcode's video + audio target,
    which is the limit applied (2,808,000 + 192,000 = 3,000,000)."""

    MEASURED = [
        (10000000, 0, False, 10000000),
        (2147483000, 0, False, DIRECT_PLAY),
        (0, 0, False, DIRECT_PLAY),
        (-1, 0, False, DIRECT_PLAY),
        (2147483000, 3000000, False, DIRECT_PLAY),      # loopback / LAN
        (2147483000, 3000000, True, 3000000),
        (2147483647, 3000000, True, 3000000),
        (0, 3000000, True, DIRECT_PLAY),                # the bypass
        (-1, 3000000, True, DIRECT_PLAY),               # the bypass
        (2147483647, 15000000, True, 15000000),  # user limit replaces server
        (8000000, 15000000, True, 8000000),
        (10000000, 30000000, True, 10000000),
        (2147483647, 30000000, True, DIRECT_PLAY),
    ]

    def test_the_model_reproduces_the_measurements(self):
        for client, admin, remote, measured in self.MEASURED:
            with self.subTest(client=client, admin=admin, remote=remote):
                self.assertEqual(_model_decision(client, admin, remote),
                                 measured)

    def test_no_setting_can_defeat_an_administrator_limit(self):
        """Whatever is stored, the profile we send keeps a remote admin
        limit in force -- the property the 0/negative values broke."""
        admin = 3000000
        for value, default, _expected in NORMALIZE_TABLE:
            for remote in (True, False):
                with self.subTest(value=value, remote=remote), \
                        _patched(remote_kbps=value, local_kbps=value):
                    from jellyfin_mpv_shim.utils import get_profile
                    p = get_profile(is_remote=remote)
                    eff = _model_effective_limit(
                        p["MaxStreamingBitrate"], admin, remote=True)
                    self.assertIsNotNone(eff)
                    self.assertLessEqual(eff, admin)


class _patched:
    """Patch settings attributes for one block, bypassing validation, the way
    a CLI override or a direct assignment would."""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.p = mock.patch.multiple(settings, **self.values)
        self.p.start()
        return self

    def __exit__(self, *exc):
        self.p.stop()
        return False


class ProfileTest(unittest.TestCase):
    """The negotiation payload, exactly."""

    def _profile(self, **kw):
        from jellyfin_mpv_shim.utils import get_profile
        return get_profile(**kw)

    def assertWireValid(self, profile, expected_bps):
        for key in ("MaxStreamingBitrate", "MaxStaticBitrate"):
            self.assertIn(key, profile)
            self.assertIs(type(profile[key]), int)
            self.assertEqual(profile[key], expected_bps, key)

    def test_defaults_are_unchanged(self):
        """The values Phase 2A captured from the real client: remote 10 Mbps,
        local 2,147,483,000."""
        with _patched(remote_kbps=Settings.remote_kbps,
                      local_kbps=Settings.local_kbps):
            self.assertWireValid(self._profile(is_remote=True), 10000000)
            self.assertWireValid(self._profile(is_remote=False), 2147483000)
        self.assertEqual(Settings.remote_kbps, 10000)

    def test_valid_settings_are_sent_as_is(self):
        with _patched(remote_kbps=4000, local_kbps=80000):
            self.assertWireValid(self._profile(is_remote=True), 4000000)
            self.assertWireValid(self._profile(is_remote=False), 80000000)

    def test_settings_assigned_directly_are_still_normalised(self):
        """Bypassing the Settings UI and the loader -- the CLI override and
        `settings.remote_kbps = x` both land here."""
        for value, _default, _expected in NORMALIZE_TABLE:
            with self.subTest(value=value), \
                    _patched(remote_kbps=value, local_kbps=value):
                self.assertWireValid(
                    self._profile(is_remote=True),
                    BL.normalize_kbps(value, Settings.remote_kbps) * 1000)
                self.assertWireValid(
                    self._profile(is_remote=False),
                    BL.normalize_kbps(value, Settings.local_kbps) * 1000)

    def test_a_malformed_setting_falls_back_to_that_setting_s_default(self):
        with _patched(remote_kbps="abc", local_kbps=None):
            self.assertWireValid(self._profile(is_remote=True), 10000000)
            self.assertWireValid(self._profile(is_remote=False), 2147483000)

    def test_an_explicit_override_is_normalised(self):
        """The quality menu's override (`Video.trs_ovr`), which the osc bridge
        parses with a bare `int()`."""
        with _patched(remote_kbps=10000):
            self.assertWireValid(self._profile(is_remote=True,
                                               video_bitrate=4000), 4000000)
            self.assertWireValid(self._profile(is_remote=True,
                                               video_bitrate=0), 2147483000)
            self.assertWireValid(self._profile(is_remote=True,
                                               video_bitrate=-3), 2147483000)
            self.assertWireValid(
                self._profile(is_remote=True, video_bitrate=3000000),
                2147483000)
            # Malformed: the setting it was overriding.
            self.assertWireValid(
                self._profile(is_remote=True, video_bitrate="abc"), 10000000)
            self.assertWireValid(
                self._profile(is_remote=True, video_bitrate=True), 10000000)

    def test_forced_transcode_keeps_a_valid_bitrate(self):
        """A forced transcode at 0 was encoded at 64 kbps (measured)."""
        with _patched(remote_kbps=0):
            p = self._profile(is_remote=True, force_transcode=True)
        self.assertEqual(p["DirectPlayProfiles"], [])
        self.assertWireValid(p, 2147483000)

    def test_no_top_level_bitrate_is_sent(self):
        """A top-level MaxStreamingBitrate overrides the profile, and a
        top-level 0 was measured to defeat the admin limit. The client sends
        none; the request body is captured from the real apiclient."""
        from jellyfin_apiclient_python.api import API

        captured = {}
        api = API.__new__(API)
        api.items = lambda path, method, json=None, **kw: captured.update(
            path=path, method=method, json=json)
        with _patched(remote_kbps=0):
            api.get_play_info("item", self._profile(is_remote=True), None,
                              None)
        body = captured["json"]
        self.assertNotIn("MaxStreamingBitrate", body)
        self.assertNotIn("MaxStaticBitrate", body)
        self.assertEqual(set(body), {"UserId", "AutoOpenLiveStream",
                                     "IsPlayback", "DeviceProfile"})
        self.assertEqual(body["DeviceProfile"]["MaxStreamingBitrate"],
                         2147483000)


class SettingsUiTest(unittest.TestCase):
    """`set_setting` refuses what it cannot store safely, so the field
    reports the error instead of saving a value that would be rewritten."""

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg

        self.cfg = cfg
        self._saved = (settings.save, settings.remote_kbps,
                       settings.local_kbps)
        settings.save = lambda *a, **k: None   # no config file under test

    def tearDown(self):
        (settings.save, settings.remote_kbps,
         settings.local_kbps) = self._saved

    def test_valid_input_is_stored(self):
        for key in ("remote_kbps", "local_kbps"):
            for text, value in (("4000", 4000), ("1", 1),
                                ("2147483", 2147483), (" 320 ", 320)):
                with self.subTest(key=key, text=text):
                    self.assertTrue(self.cfg.set_setting(key, text))
                    self.assertEqual(getattr(settings, key), value)

    def test_invalid_input_is_refused_and_nothing_changes(self):
        for key in ("remote_kbps", "local_kbps"):
            setattr(settings, key, 4000)
            for text in ("0", "-1", "2147484", "99999999999", "abc", "12.5",
                         "", None, True):
                with self.subTest(key=key, text=text):
                    self.assertFalse(self.cfg.set_setting(key, text))
                    self.assertEqual(getattr(settings, key), 4000)

    def test_other_int_settings_are_not_affected(self):
        """The range check is for the two bitrate keys only."""
        saved = settings.playback_timeout
        try:
            self.assertTrue(self.cfg.set_setting("playback_timeout", "0"))
            self.assertEqual(settings.playback_timeout, 0)
        finally:
            settings.playback_timeout = saved


class ConfigFileTest(unittest.TestCase):
    """A hand-edited conf.json, on load. Normalised on every load, not only
    once: a file edited after the upgrade is just as reachable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "conf.json")
        self._config_path = conf.config_path

    def tearDown(self):
        conf.config_path = self._config_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, **values):
        """A complete, current file -- so a load that changes nothing has no
        reason to write it back."""
        data = Settings().dict()
        data["config_version"] = conf.CONFIG_VERSION
        for k, v in values.items():
            if v is _MISSING:
                data.pop(k)
            else:
                data[k] = v
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=4)
        with open(self.path, "rb") as fh:
            return fh.read()

    def _load(self):
        s = Settings()
        self.assertTrue(s.load(self.path, create=False))
        with open(self.path, encoding="utf-8") as fh:
            return s, json.load(fh)

    def test_valid_values_are_preserved_and_the_file_is_not_rewritten(self):
        for remote, local in ((10000, 2147483), (4000, 80000), (1, 2147483),
                              (2147483, 2147483)):
            with self.subTest(remote=remote, local=local):
                before = self._write(remote_kbps=remote, local_kbps=local)
                s, _disk = self._load()
                self.assertEqual((s.remote_kbps, s.local_kbps), (remote, local))
                with open(self.path, "rb") as fh:
                    self.assertEqual(fh.read(), before,
                                     "a valid config was rewritten")

    def test_the_stored_values_are_migrated_and_saved(self):
        cases = [
            # (stored, expected remote, expected on disk)
            (0, UNLIMITED),
            (-5, UNLIMITED),
            (2147484, UNLIMITED),
            (99999999999, UNLIMITED),
            (1e12, UNLIMITED),
            ("0", UNLIMITED),
            ("4000", 4000),
            ("abc", Settings.remote_kbps),
            (True, Settings.remote_kbps),
            (None, Settings.remote_kbps),
            (12.5, Settings.remote_kbps),
            ([1], Settings.remote_kbps),
            ({"kbps": 1}, Settings.remote_kbps),
        ]
        for stored, expected in cases:
            with self.subTest(stored=stored):
                self._write(remote_kbps=stored)
                s, disk = self._load()
                self.assertEqual(s.remote_kbps, expected)
                self.assertIs(type(s.remote_kbps), int)
                self.assertEqual(disk["remote_kbps"], expected)

    def test_the_local_key_too(self):
        for stored, expected in ((0, UNLIMITED), (-1, UNLIMITED),
                                 (5000000, UNLIMITED), (False, UNLIMITED),
                                 ("x", UNLIMITED), (60000, 60000)):
            with self.subTest(stored=stored):
                self._write(local_kbps=stored)
                s, disk = self._load()
                self.assertEqual(s.local_kbps, expected)
                self.assertEqual(disk["local_kbps"], expected)

    def test_a_missing_key_takes_the_default(self):
        self._write(remote_kbps=_MISSING, local_kbps=_MISSING)
        s, disk = self._load()
        self.assertEqual((s.remote_kbps, s.local_kbps),
                         (Settings.remote_kbps, Settings.local_kbps))
        self.assertEqual(disk["remote_kbps"], 10000)

    def test_idempotent(self):
        """The second load finds nothing to do and leaves the file alone."""
        self._write(remote_kbps=-1, local_kbps="huh")
        self._load()
        with open(self.path, "rb") as fh:
            first = fh.read()
        s, _disk = self._load()
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), first)
        self.assertEqual((s.remote_kbps, s.local_kbps),
                         (UNLIMITED, Settings.local_kbps))

    def test_the_config_version_is_not_bumped(self):
        """Not a versioned step: an install already at the current version
        is normalised all the same, and the version stays what it was, so a
        downgrade reads the file exactly as before."""
        data = Settings().dict()
        data.update(config_version=conf.CONFIG_VERSION, remote_kbps=0)
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        s, disk = self._load()
        self.assertEqual(s.remote_kbps, UNLIMITED)
        self.assertEqual(disk["config_version"], conf.CONFIG_VERSION)


_MISSING = object()


if __name__ == "__main__":
    unittest.main()
