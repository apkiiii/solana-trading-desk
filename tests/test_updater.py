from __future__ import annotations

import hashlib

import httpx
import pytest

from core.updater import (UpdateError, check_for_update, download_update, parse_checksums, parse_release)
from core.version import __version__, is_newer, parse_version

PREFIX = "SolanaTradingDesk-Setup-"
REPO = "apkiiii/solana-trading-desk"


def _release(tag: str, *, with_sums: bool = True, prerelease: bool = False) -> dict:
    version = tag.lstrip("v")
    assets = [{"name": f"{PREFIX}{version}.exe", "size": 11,
               "browser_download_url": f"https://dl.example/{PREFIX}{version}.exe"}]
    if with_sums:
        assets.append({"name": "SHA256SUMS.txt", "browser_download_url": "https://dl.example/SHA256SUMS.txt"})
    return {"tag_name": tag, "name": f"Version {version}", "body": "Nouveautés", "draft": False,
            "prerelease": prerelease, "assets": assets, "html_url": "https://github.com/x"}


def test_version_comparison():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("0.10") == (0, 10, 0)
    assert is_newer("0.10.0", "0.9.9")
    assert not is_newer("v0.4.0", "0.4.0")
    assert not is_newer("0.3.9", "0.4.0")


def test_release_parsing_requires_installer_and_checksums():
    assert parse_release(_release("v9.9.9"), PREFIX).version == "9.9.9"
    assert parse_release(_release("v9.9.9", with_sums=False), PREFIX) is None
    assert parse_release(_release("v9.9.9", prerelease=True), PREFIX) is None


def _transport(release: dict | None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/releases/latest"
        assert request.headers["user-agent"].startswith("SolanaTradingDesk/")
        if release is None:
            return httpx.Response(status)
        return httpx.Response(200, json=release)
    return httpx.MockTransport(handler)


def test_check_for_update_newer_same_and_none():
    newer = check_for_update(REPO, PREFIX, transport=_transport(_release("v99.0.0")))
    assert newer is not None and newer.version == "99.0.0"
    assert check_for_update(REPO, PREFIX, transport=_transport(_release(f"v{__version__}"))) is None
    assert check_for_update(REPO, PREFIX, transport=_transport(None, 404)) is None
    with pytest.raises(UpdateError):
        check_for_update(REPO, PREFIX, transport=_transport(None, 500))


def test_parse_checksums():
    sums = parse_checksums(f"{'a' * 64}  file.exe\n{'B' * 64} *other.exe\nbad line\n")
    assert sums == {"file.exe": "a" * 64, "other.exe": "b" * 64}


def _download_transport(payload: bytes, declared_hash: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{declared_hash}  {PREFIX}99.0.0.exe\n")
        return httpx.Response(200, content=payload)
    return httpx.MockTransport(handler)


def test_download_verifies_checksum(tmp_path):
    info = parse_release(_release("v99.0.0"), PREFIX)
    payload = b"installer!!"
    good = hashlib.sha256(payload).hexdigest()
    seen = []
    path = download_update(info, tmp_path, transport=_download_transport(payload, good),
                           progress=lambda done, total: seen.append(done))
    assert path.read_bytes() == payload and seen[-1] == len(payload)


def test_download_with_wrong_checksum_is_deleted(tmp_path):
    info = parse_release(_release("v99.0.0"), PREFIX)
    with pytest.raises(UpdateError):
        download_update(info, tmp_path, transport=_download_transport(b"tampered", "0" * 64))
    assert list(tmp_path.iterdir()) == []
