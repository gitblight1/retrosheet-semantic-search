"""Conditional fetch (rsse/util/download.py, spec/01-CORPUS.md §2).

Retrosheet reissues corrected files. Until these ran, a reissue was invisible:
`fetch_season` skipped when the zip was *present*, never when it was
*current*, so the exact digest check in `ingest_file` downstream never got the
chance to fire.

Every test here is offline. The project has been rate-limited off the real
server once, and a test suite that reaches for the network is a test suite
nobody can run twice in a minute.
"""

import io
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from rsse.util import download


def a_zip(**files: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return buf.getvalue()


class Response:
    def __init__(self, body: bytes, headers: dict):
        self._body, self.headers = body, headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(body: bytes, headers: dict | None = None):
    """A urlopen that always answers 200 with `body`."""
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        return Response(body, headers or {})

    urlopen.calls = calls
    return urlopen


def refuse(code: int, reason: str = "nope"):
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req)
        raise urllib.error.HTTPError(req.full_url, code, reason, {}, None)

    urlopen.calls = calls
    return urlopen


class Conditional(unittest.TestCase):
    def setUp(self):
        import shutil, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patch = mock.patch.object(download, "DELAY_SECONDS", 0)
        patch.start()
        self.addCleanup(patch.stop)

    def seed(self, body: bytes, state: dict | None = None):
        """Put a season archive on disk as though a previous fetch left it."""
        with mock.patch("urllib.request.urlopen", serve(body, {})):
            return download.fetch_season(2024, self.tmp, state=state)

    # -- the 304 path ----------------------------------------------------

    def test_a_304_reports_unchanged_and_rewrites_nothing(self):
        self.seed(a_zip(**{"2024BOS.EVA": b"id,BOS202404010\r\n"}))
        before = (self.tmp / "2024eve.zip").read_bytes()
        with mock.patch("urllib.request.urlopen", refuse(304, "Not Modified")):
            got = download.fetch_season(2024, self.tmp, refresh=True,
                                        state={})
        self.assertEqual(got.status, "unchanged")
        self.assertEqual(got.changed_files, ())
        self.assertEqual((self.tmp / "2024eve.zip").read_bytes(), before)

    def test_a_304_is_not_retried(self):
        self.seed(a_zip(**{"2024BOS.EVA": b"x"}))
        urlopen = refuse(304, "Not Modified")
        with mock.patch("urllib.request.urlopen", urlopen):
            download.fetch_season(2024, self.tmp, refresh=True, state={})
        self.assertEqual(len(urlopen.calls), 1, "a 304 is a success, not a failure")

    def test_the_stored_validators_are_sent(self):
        self.seed(a_zip(**{"2024BOS.EVA": b"x"}))
        url = f"{download.BASE}/events/2024eve.zip"
        state = {url: {"etag": '"abc"', "last_modified": "Mon, 01 Apr 2024 00:00:00 GMT"}}
        urlopen = refuse(304)
        with mock.patch("urllib.request.urlopen", urlopen):
            download.fetch_season(2024, self.tmp, refresh=True, state=state)
        sent = urlopen.calls[0]
        self.assertEqual(sent.get_header("If-none-match"), '"abc"')
        self.assertEqual(sent.get_header("If-modified-since"),
                         "Mon, 01 Apr 2024 00:00:00 GMT")

    # -- a server that answers with a body -------------------------------

    def test_identical_bytes_count_as_unchanged(self):
        """Not every server sends validators. The digest is the authority."""
        body = a_zip(**{"2024BOS.EVA": b"id,BOS202404010\r\n"})
        state = {}
        self.seed(body, state)
        with mock.patch("urllib.request.urlopen", serve(body, {})):
            got = download.fetch_season(2024, self.tmp, refresh=True,
                                        state=state)
        self.assertEqual(got.status, "unchanged")
        self.assertEqual(got.changed_files, ())

    def test_different_bytes_report_which_files_changed(self):
        state = {}
        self.seed(a_zip(**{"2024BOS.EVA": b"id,BOS202404010\r\n",
                           "2024NYA.EVA": b"id,NYA202404010\r\n"}), state)
        reissued = a_zip(**{"2024BOS.EVA": b"id,BOS202404010\r\ncom,\"fixed\"\r\n",
                            "2024NYA.EVA": b"id,NYA202404010\r\n"})
        with mock.patch("urllib.request.urlopen", serve(reissued, {})):
            got = download.fetch_season(2024, self.tmp, refresh=True,
                                        state=state)
        self.assertEqual(got.status, "changed")
        self.assertEqual([p.name for p in got.changed_files], ["2024BOS.EVA"],
                         "only the file whose bytes moved")

    # -- not asking is still the default ---------------------------------

    def test_without_refresh_a_present_archive_costs_no_request(self):
        self.seed(a_zip(**{"2024BOS.EVA": b"x"}))
        urlopen = serve(b"should not be called")
        with mock.patch("urllib.request.urlopen", urlopen):
            got = download.fetch_season(2024, self.tmp)
        self.assertEqual(got.status, "present")
        self.assertEqual(len(urlopen.calls), 0)

    # -- failing fast ----------------------------------------------------

    def test_a_404_costs_one_request_not_four(self):
        urlopen = refuse(404, "Not Found")
        with mock.patch("urllib.request.urlopen", urlopen):
            got = download.fetch_season(1776, self.tmp, state={})
        self.assertEqual(got.status, "failed")
        self.assertEqual(len(urlopen.calls), 1)

    def test_a_429_is_retried(self):
        urlopen = refuse(429, "Too Many Requests")
        with mock.patch("urllib.request.urlopen", urlopen), \
                mock.patch.object(download, "BACKOFF_SECONDS", 0):
            download.fetch_season(2024, self.tmp, state={})
        self.assertEqual(len(urlopen.calls), download.MAX_ATTEMPTS)


class State(unittest.TestCase):
    def setUp(self):
        import shutil, tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_round_trip(self):
        download.save_state(self.tmp, {"u": {"etag": '"a"'}})
        self.assertEqual(download.load_state(self.tmp), {"u": {"etag": '"a"'}})

    def test_a_missing_or_corrupt_state_file_is_empty_not_fatal(self):
        self.assertEqual(download.load_state(self.tmp), {})
        (self.tmp / download.STATE_FILE).write_text("{not json")
        self.assertEqual(download.load_state(self.tmp), {})

    def test_validators_are_recorded_from_the_response(self):
        with mock.patch("urllib.request.urlopen",
                        serve(a_zip(**{"x.EVA": b"x"}),
                              {"ETag": '"v1"', "Last-Modified": "Mon, 01 Apr 2024 00:00:00 GMT"})), \
                mock.patch.object(download, "DELAY_SECONDS", 0):
            state = {}
            download.fetch_season(2024, self.tmp, state=state)
        entry = state[f"{download.BASE}/events/2024eve.zip"]
        self.assertEqual(entry["etag"], '"v1"')
        self.assertEqual(entry["last_modified"], "Mon, 01 Apr 2024 00:00:00 GMT")
        self.assertIn("sha256", entry)


if __name__ == "__main__":
    unittest.main()
