"""How JellyHome Player names itself on the wire.

There are four separate identities, and only the first is changed by
renaming the client:

* the HTTP ``User-Agent`` (``constants.USER_AGENT``): a label in access logs;
* the Jellyfin ``Authorization: MediaBrowser Client=..., Device=...,
  DeviceId=..., Version=...`` header, which is what the server uses to
  recognise the device, list the session and bind the access token;
* ``X-Application`` (``<Client>/<Version>``), built by the apiclient;
* the update check, which sends no custom headers at all.

These tests drive the real client construction (``ClientManager.client_factory``
and the browser's ``ServerConn``) and a real apiclient request, captured by a
transport adapter, with every socket connect refused, so nothing leaves the
machine.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import re
import socket
import sys
import types
import unittest
from unittest import mock

import requests
from requests.adapters import BaseAdapter

sys.argv = ["test"]

from jellyfin_mpv_shim import clients, constants, log_utils  # noqa: E402
from jellyfin_mpv_shim import update_check as uc  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "jellyfin_mpv_shim")

SERVER = "https://jf.example"
TOKEN = "0123456789abcdef0123456789abcdef"
DEVICE_ID = "6c0a1f6e-2b9e-4f55-9d36-3f4c1d2a7b10"
DEVICE_NAME = "living-room"

#: RFC 9110 section 10.1.5: product = token ["/" product-version].
_TCHAR = r"[!#$%&'*+.^_`|~0-9A-Za-z-]"
PRODUCT = re.compile(r"^(%s+)/(%s+)$" % (_TCHAR, _TCHAR))


def _refuse(*_a, **_k):
    raise AssertionError("a test tried to open a real network connection")


class _Recorder(BaseAdapter):
    """Answers every request with an empty JSON 200 and keeps it."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"{}"
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Date"] = "Fri, 27 Nov 2026 12:00:00 GMT"
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


class _NoNetwork(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection"):
            p = mock.patch(target, _refuse)
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _record(client):
        rec = _Recorder()
        client.http.session.mount("https://", rec)
        client.http.session.mount("http://", rec)
        return rec

    def _manager_client(self):
        """The client the playback/cast side uses, built by the real factory."""
        stub = types.SimpleNamespace(device_name=DEVICE_NAME,
                                     device_id=DEVICE_ID)
        client = clients.ClientManager.client_factory(stub)
        client.config.auth(SERVER, "user-1", TOKEN, True)
        client.logged_in = True
        client.start(websocket=False)
        self.addCleanup(client.stop)
        return client

    def _browser_conn(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import ServerConn

        conn = ServerConn({"uuid": "srv-1", "name": "Home", "address": SERVER,
                           "user_id": "user-1", "token": TOKEN},
                          DEVICE_ID, DEVICE_NAME, True)
        self.addCleanup(conn.stop)
        return conn.client


class UserAgentFormatTest(unittest.TestCase):
    def test_it_is_jellyhome_player_and_the_client_version(self):
        self.assertEqual(constants.USER_AGENT,
                         "JellyHome-Player/" + constants.CLIENT_VERSION)

    def test_it_is_one_valid_http_product_token(self):
        m = PRODUCT.match(constants.USER_AGENT)
        self.assertIsNotNone(m, constants.USER_AGENT)
        self.assertEqual(m.group(1), "JellyHome-Player")

    def test_the_upstream_name_is_gone(self):
        for old in ("Jellyfin-MPV-Shim", "MPV-Shim", "Shim"):
            self.assertNotIn(old, constants.USER_AGENT)

    def test_the_version_is_not_bumped_in_this_change(self):
        # The version scheme is decided separately; this change renames only.
        self.assertEqual(constants.CLIENT_VERSION, "3.3.0")
        self.assertEqual(constants.USER_AGENT, "JellyHome-Player/3.3.0")


class OneVersionTest(unittest.TestCase):
    """Every place the version is stated reads CLIENT_VERSION."""

    def test_the_user_agent_interpolates_client_version(self):
        self.assertEqual(constants.USER_AGENT.rsplit("/", 1)[1],
                         constants.CLIENT_VERSION)

    def test_no_literal_version_in_the_user_agent_definition(self):
        with open(os.path.join(PACKAGE, "constants.py"),
                  encoding="utf-8") as fh:
            line = [ln for ln in fh if ln.startswith("USER_AGENT")]
        self.assertEqual(len(line), 1)
        self.assertIn("CLIENT_VERSION", line[0])
        self.assertNotRegex(line[0], r"\d+\.\d+\.\d+")

    def test_the_package_version_is_client_version(self):
        with open(os.path.join(ROOT, "pyproject.toml"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('dynamic = ["version"]', text)
        self.assertIn(
            'version = { attr = "jellyfin_mpv_shim.constants.CLIENT_VERSION" }',
            text)

    def test_the_command_line_version_is_client_version(self):
        from jellyfin_mpv_shim import args

        parser = args._build_parser()
        with mock.patch("sys.stdout") as out, self.assertRaises(SystemExit):
            parser.parse_args(["--version"])
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn(constants.CLIENT_VERSION, printed)


class JellyfinApiRequestTest(_NoNetwork):
    """What a real apiclient request carries, from both client builders."""

    def _check(self, client):
        rec = self._record(client)
        client.jellyfin.get_system_info_new()
        self.assertEqual(len(rec.sent), 1)
        sent = rec.sent[0]
        self.assertTrue(sent.url.startswith(SERVER + "/"), sent.url)
        h = sent.headers
        self.assertEqual(h["User-Agent"], "JellyHome-Player/3.3.0")
        self.assertEqual(h["X-Application"], "JellyHome Player/3.3.0")
        auth = h["Authorization"]
        self.assertTrue(auth.startswith("MediaBrowser "), auth)
        self.assertIn('Client="JellyHome Player"', auth)
        self.assertIn('Device="%s"' % DEVICE_NAME, auth)
        self.assertIn('DeviceId="%s"' % DEVICE_ID, auth)
        self.assertIn('Version="%s"' % constants.CLIENT_VERSION, auth)
        self.assertIn('Token="%s"' % TOKEN, auth)
        for value in h.values():
            self.assertNotIn("Shim", value)
        # The token travels in the header only, never in the url.
        self.assertNotIn(TOKEN, sent.url)

    def test_the_playback_client(self):
        self._check(self._manager_client())

    def test_the_browser_client(self):
        self._check(self._browser_conn())

    def test_both_builders_present_the_same_identity(self):
        a = self._manager_client().http._get_default_headers()
        b = self._browser_conn().http._get_default_headers()
        self.assertEqual(a, b)


class DeviceIdentityIsUnchangedTest(_NoNetwork):
    """The user agent is independent of everything the server keys on."""

    def test_authorization_does_not_depend_on_the_user_agent(self):
        before = self._manager_client().http._get_authenication_header()
        with mock.patch.object(clients, "USER_AGENT", "Something-Else/9"):
            after = self._manager_client().http._get_authenication_header()
        self.assertEqual(before, after)
        self.assertNotIn("JellyHome-Player", before)

    def test_the_device_id_is_the_one_handed_in(self):
        """`client_factory` uses the manager's device id unchanged, which is
        the saved credential's (users.py) or ``settings.client_uuid``."""
        cfg = self._manager_client().config.data
        self.assertEqual(cfg["app.device_id"], DEVICE_ID)
        self.assertEqual(cfg["app.device_name"], DEVICE_NAME)
        self.assertEqual(cfg["app.name"], constants.USER_APP_NAME)

    def test_the_manager_starts_from_the_stored_device_identity(self):
        from jellyfin_mpv_shim.conf import settings

        with open(os.path.join(PACKAGE, "clients.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("self.device_id = settings.client_uuid", src)
        self.assertIsInstance(settings.client_uuid, str)
        self.assertNotIn("Shim", settings.client_uuid)

    def test_the_user_agent_is_never_persisted(self):
        """Nothing that writes conf.json, users.json or cred.json reads the
        user agent, so no stored file changes with it."""
        for name in ("conf.py", "users.py", "conffile.py"):
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as fh:
                self.assertNotIn("USER_AGENT", fh.read(), name)


class ArtworkRequestTest(unittest.TestCase):
    def _store(self, auth=None):
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import ThumbnailStore

        store = ThumbnailStore.__new__(ThumbnailStore)
        store._auth = dict(auth or {})
        return store

    def test_artwork_carries_the_new_agent(self):
        headers = self._store()._headers_for(SERVER + "/Items/1/Images/Primary")
        self.assertEqual(headers, {"User-Agent": "JellyHome-Player/3.3.0"})

    def test_the_token_still_goes_only_to_its_own_origin(self):
        header = 'MediaBrowser Token="%s"' % TOKEN
        store = self._store({("https", "jf.example", None): header})
        own = store._headers_for(SERVER + "/Items/1/Images/Primary")
        self.assertEqual(own["Authorization"], header)
        self.assertEqual(own["User-Agent"], "JellyHome-Player/3.3.0")
        other = store._headers_for("https://cdn.example/poster.jpg")
        self.assertEqual(other, {"User-Agent": "JellyHome-Player/3.3.0"})


class EverySetterUsesTheConstantTest(unittest.TestCase):
    """No first-party module spells a user agent of its own."""

    def _sources(self):
        for base, _dirs, files in os.walk(PACKAGE):
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(base, name)
                    with open(path, encoding="utf-8") as fh:
                        yield os.path.relpath(path, ROOT), fh.read()

    def test_the_upstream_agent_is_not_in_the_package(self):
        for rel, text in self._sources():
            with self.subTest(path=rel):
                self.assertNotIn("Jellyfin-MPV-Shim", text)

    def test_every_user_agent_assignment_uses_the_constant(self):
        pattern = re.compile(
            r"""(?:["']User-Agent["']\s*:|["']http\.user_agent["']\]\s*=)"""
            r"""\s*(.+)""")
        found = 0
        for rel, text in self._sources():
            for m in pattern.finditer(text):
                found += 1
                with self.subTest(path=rel, line=m.group(0)):
                    self.assertTrue(m.group(1).startswith("USER_AGENT"),
                                    m.group(0))
        # clients.py, repository.py and thumbnails.py.
        self.assertGreaterEqual(found, 3)


class TokenRedactionTest(_NoNetwork):
    def test_a_logged_header_dict_loses_its_token_and_keeps_the_agent(self):
        headers = self._manager_client().http._get_default_headers()
        out = log_utils.sanitize(headers)
        text = out if isinstance(out, str) else str(out)
        self.assertNotIn(TOKEN, text)
        self.assertIn('Token="REDACTED"', text)
        self.assertIn("JellyHome-Player/3.3.0", text)
        self.assertIn("JellyHome Player/3.3.0", text)

    def test_the_agent_text_is_not_mangled_by_redaction(self):
        self.assertEqual(log_utils.sanitize(constants.USER_AGENT),
                         constants.USER_AGENT)


class UpdateCheckTest(unittest.TestCase):
    """The update check is unchanged: it adds no headers, so it neither sends
    this user agent nor any other identifying header of ours."""

    def test_the_release_lookup_sends_no_custom_headers(self):
        seen = []

        def get(url, **kw):
            seen.append(kw)
            raise requests.ConnectionError("offline in tests")

        checker = uc.UpdateChecker.__new__(uc.UpdateChecker)
        with mock.patch.object(uc.requests, "get", get):
            try:
                checker._resolve_latest(
                    "https://github.com/braisravina/jellyhome-player/releases/")
            except requests.ConnectionError:
                pass
        self.assertEqual(len(seen), 1)
        self.assertNotIn("headers", seen[0])

    def test_the_update_module_does_not_use_the_agent(self):
        with open(os.path.join(PACKAGE, "update_check.py"),
                  encoding="utf-8") as fh:
            self.assertNotIn("USER_AGENT", fh.read())


class NoUnintendedRequestsTest(_NoNetwork):
    def test_building_either_client_sends_nothing(self):
        for build in (self._manager_client, self._browser_conn):
            with self.subTest(build=build.__name__):
                rec = self._record(build())
                self.assertEqual(rec.sent, [])

    def test_one_api_call_is_one_request(self):
        client = self._manager_client()
        rec = self._record(client)
        client.jellyfin.get_system_info_new()
        self.assertEqual([r.method for r in rec.sent], ["GET"])


if __name__ == "__main__":
    unittest.main()
