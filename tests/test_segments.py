"""Tests for segmented (multi-connection) downloads of large files."""

from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from mc_launcher.core.net import (
    CancelledError,
    DownloadError,
    DownloadSpec,
    HttpClient,
)


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200, headers=None, on_read=None):
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}
        self._url = "https://example.com/file.bin"
        self._on_read = on_read
        self._read_once = False

    def read(self, size: int = -1) -> bytes:
        if self._on_read is not None and not self._read_once:
            self._read_once = True
            self._on_read()
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _full(payload: bytes, *, status: int = 200, on_read=None) -> _Response:
    return _Response(payload, status=status, on_read=on_read)


def _range(payload: bytes, start: int, end: int, on_read=None) -> _Response:
    body = payload[start : end + 1]
    return _Response(
        body,
        status=206,
        headers={
            "Content-Length": str(len(body)),
            "Content-Range": f"bytes {start}-{end}/{len(payload)}",
        },
        on_read=on_read,
    )


class SegmentedDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-segments-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.payload = bytes(range(256)) * 16  # 4096 bytes
        self.sha1 = hashlib.sha1(self.payload).hexdigest()

    def _patched(self):
        return mock.patch.multiple(
            "mc_launcher.core.net",
            SEGMENT_MIN_SIZE=1024,
            MIN_SEGMENT_SIZE=256,
        )

    def _client(self, opener, *, segments: int = 4, retries: int = 0) -> HttpClient:
        client = HttpClient(
            {"example.com"},
            check_dns=False,
            allow_private=True,
            retries=retries,
            segments=segments,
        )
        client.open = types.MethodType(opener, client)  # type: ignore[method-assign]
        return client

    def _spec(self, dest: Path) -> DownloadSpec:
        return DownloadSpec(
            dest=dest,
            url="https://example.com/file.bin",
            sha1=self.sha1,
            size=len(self.payload),
            label="large.bin",
        )

    def _no_leftovers(self, dest: Path) -> None:
        self.assertFalse(dest.with_name(dest.name + ".part").exists())
        self.assertEqual(list(dest.parent.glob(dest.name + ".part.seg*")), [])

    def test_segments_merge_and_verify(self) -> None:
        dest = self.work / "large.bin"
        calls: list[dict] = []

        def opener(_self, _url, headers=None, method="GET"):
            headers = dict(headers or {})
            calls.append(headers)
            range_header = headers.get("Range")
            if range_header:
                start_text, end_text = range_header.split("=")[1].split("-")
                return _range(self.payload, int(start_text), int(end_text))
            return _full(self.payload)

        client = self._client(opener)
        progress: list[int] = []
        with self._patched():
            written = client.download_file(self._spec(dest), progress=progress.append)
        self.assertEqual(written, len(self.payload))
        self.assertEqual(dest.read_bytes(), self.payload)
        self._no_leftovers(dest)
        range_calls = [item for item in calls if "Range" in item]
        self.assertEqual(len(range_calls), 4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(progress, sorted(progress))
        self.assertEqual(progress[-1], len(self.payload))
        self.assertTrue(all(0 <= item <= len(self.payload) for item in progress))

    def test_http_200_falls_back_to_single_stream(self) -> None:
        dest = self.work / "falls-back.bin"
        calls: list[dict] = []

        def opener(_self, _url, headers=None, method="GET"):
            headers = dict(headers or {})
            calls.append(headers)
            # A server that ignores Range returns 200 with the full body.
            return _full(self.payload)

        client = self._client(opener)
        with self._patched():
            written = client.download_file(self._spec(dest))
        self.assertEqual(written, len(self.payload))
        self.assertEqual(dest.read_bytes(), self.payload)
        self._no_leftovers(dest)
        self.assertTrue(any("Range" in item for item in calls))
        self.assertEqual(calls[-1], {})

    def test_cancel_removes_segments(self) -> None:
        dest = self.work / "cancelled.bin"
        cancel = threading.Event()

        def opener(_self, _url, headers=None, method="GET"):
            headers = dict(headers or {})
            range_header = headers.get("Range")
            if range_header:
                start_text, end_text = range_header.split("=")[1].split("-")
                return _range(
                    self.payload,
                    int(start_text),
                    int(end_text),
                    on_read=cancel.set,
                )
            return _full(self.payload)

        client = self._client(opener)
        with self._patched():
            with self.assertRaises(CancelledError):
                client.download_file(self._spec(dest), cancel=cancel)
        self.assertFalse(dest.exists())
        self._no_leftovers(dest)

    def test_segments_disabled_uses_single_connection(self) -> None:
        dest = self.work / "single.bin"
        calls: list[dict] = []

        def opener(_self, _url, headers=None, method="GET"):
            calls.append(dict(headers or {}))
            return _full(self.payload)

        client = self._client(opener, segments=0)
        with self._patched():
            client.download_file(self._spec(dest))
        self.assertEqual(dest.read_bytes(), self.payload)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("Range", calls[0])

    def test_existing_part_keeps_single_stream_resume(self) -> None:
        dest = self.work / "resumed.bin"
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(self.payload[:500])
        calls: list[dict] = []

        def opener(_self, _url, headers=None, method="GET"):
            headers = dict(headers or {})
            calls.append(headers)
            range_header = headers.get("Range")
            if range_header:
                start_text = range_header.split("=")[1].split("-")[0]
                return _range(self.payload, int(start_text), len(self.payload) - 1)
            return _full(self.payload)

        client = self._client(opener)
        progress: list[int] = []
        with self._patched():
            written = client.download_file(self._spec(dest), progress=progress.append)
        self.assertEqual(written, len(self.payload) - 500)
        self.assertEqual(dest.read_bytes(), self.payload)
        self.assertEqual(len(calls), 1)
        self.assertEqual(progress[0], 500)
        self._no_leftovers(dest)

    def test_stale_segment_is_resumed(self) -> None:
        dest = self.work / "stale.bin"
        part = dest.with_name(dest.name + ".part")
        first = part.with_name(part.name + ".seg0")
        dest.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(self.payload[:100])
        calls: list[dict] = []

        def opener(_self, _url, headers=None, method="GET"):
            headers = dict(headers or {})
            calls.append(headers)
            range_header = headers.get("Range")
            if range_header:
                start_text, end_text = range_header.split("=")[1].split("-")
                return _range(self.payload, int(start_text), int(end_text))
            return _full(self.payload)

        client = self._client(opener)
        with self._patched():
            written = client.download_file(self._spec(dest))
        self.assertEqual(dest.read_bytes(), self.payload)
        self.assertGreaterEqual(written, len(self.payload) - 100)
        self._no_leftovers(dest)
        resumed = [
            item["Range"]
            for item in calls
            if "Range" in item and item["Range"].startswith("bytes=100-")
        ]
        self.assertTrue(resumed)

    def test_segmented_hash_failure_falls_back_and_reports(self) -> None:
        dest = self.work / "bad.bin"
        corrupted = bytearray(self.payload)
        corrupted[0] ^= 0xFF

        def opener(_self, _url, headers=None, method="GET"):
            headers = dict(headers or {})
            range_header = headers.get("Range")
            if range_header:
                start_text, end_text = range_header.split("=")[1].split("-")
                return _range(bytes(corrupted), int(start_text), int(end_text))
            return _full(bytes(corrupted))

        client = self._client(opener)
        with self._patched():
            with self.assertRaises(DownloadError):
                client.download_file(self._spec(dest))
        self.assertFalse(dest.exists())
        self._no_leftovers(dest)


if __name__ == "__main__":
    unittest.main()
