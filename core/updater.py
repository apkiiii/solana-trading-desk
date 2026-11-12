"""Mises à jour du logiciel via les « Releases » GitHub du dépôt public.

Déroulement :
1. ``check_for_update`` lit la dernière version publiée (API GitHub, sans compte).
2. Si elle est plus récente, l'application la propose ; rien ne s'installe sans ton clic.
3. ``download_update`` télécharge l'installateur et vérifie son empreinte SHA-256
   (fichier ``SHA256SUMS.txt`` publié avec la version). En cas d'écart, le fichier est supprimé.
4. ``launch_installer`` lance l'installateur en mode silencieux puis l'application se ferme ;
   l'installateur la relance. Tes données (``%LOCALAPPDATA%\\SolanaTradingDesk``) sont conservées.

Limite : l'empreinte protège contre un fichier corrompu ou modifié en route, pas contre une
prise de contrôle du compte GitHub — protège ce compte avec la double authentification.
"""
from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.version import __version__, is_newer

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
CHECKSUMS_NAME = "SHA256SUMS.txt"


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    title: str
    notes: str
    published_at: str | None
    installer_name: str
    installer_url: str
    installer_size: int | None
    checksums_url: str
    page_url: str | None


def _headers() -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "User-Agent": f"SolanaTradingDesk/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28"}


def parse_release(data: dict[str, Any], installer_prefix: str) -> UpdateInfo | None:
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    if not tag:
        return None
    assets = {a.get("name"): a for a in data.get("assets") or [] if isinstance(a, dict)}
    installer = next((a for name, a in assets.items()
                      if name and name.startswith(installer_prefix) and name.lower().endswith(".exe")), None)
    checksums = assets.get(CHECKSUMS_NAME)
    if installer is None or checksums is None:
        return None
    return UpdateInfo(
        version=tag.lstrip("vV"),
        title=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        published_at=data.get("published_at"),
        installer_name=str(installer["name"]),
        installer_url=str(installer["browser_download_url"]),
        installer_size=installer.get("size"),
        checksums_url=str(checksums["browser_download_url"]),
        page_url=data.get("html_url"),
    )


def check_for_update(repository: str, installer_prefix: str, *, current: str = __version__,
                     transport: httpx.BaseTransport | None = None, timeout: float = 15.0) -> UpdateInfo | None:
    """Retourne la mise à jour disponible, ou None si le logiciel est à jour."""
    url = f"{GITHUB_API}/repos/{repository}/releases/latest"
    try:
        with httpx.Client(headers=_headers(), timeout=timeout, transport=transport,
                          follow_redirects=True) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        raise UpdateError(f"Impossible de contacter GitHub : {type(exc).__name__}") from exc
    if response.status_code == 404:
        return None  # aucune version publiée pour l'instant
    if response.status_code != 200:
        raise UpdateError(f"GitHub a répondu {response.status_code}")
    info = parse_release(response.json(), installer_prefix)
    if info is None or not is_newer(info.version, current):
        return None
    return info


def parse_checksums(text: str) -> dict[str, str]:
    """Format « <sha256>  <nom de fichier> » (une ligne par fichier)."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            result[parts[-1].lstrip("*")] = parts[0].lower()
    return result


def download_update(info: UpdateInfo, dest_dir: Path, *, progress: Callable[[int, int | None], None] | None = None,
                    transport: httpx.BaseTransport | None = None) -> Path:
    """Télécharge l'installateur et vérifie son empreinte SHA-256."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / info.installer_name
    partial = target.with_suffix(".part")
    with httpx.Client(headers=_headers(), timeout=60.0, transport=transport, follow_redirects=True) as client:
        sums = client.get(info.checksums_url)
        if sums.status_code != 200:
            raise UpdateError("Fichier d'empreintes introuvable : mise à jour annulée.")
        expected = parse_checksums(sums.text).get(info.installer_name)
        if not expected:
            raise UpdateError("Empreinte de l'installateur absente : mise à jour annulée.")
        digest = hashlib.sha256()
        done = 0
        with client.stream("GET", info.installer_url) as response:
            if response.status_code != 200:
                raise UpdateError(f"Téléchargement impossible (HTTP {response.status_code}).")
            total = int(response.headers.get("content-length") or 0) or info.installer_size
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=256 * 1024):
                    handle.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    if digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError("L'empreinte du fichier téléchargé ne correspond pas : fichier supprimé, "
                          "mise à jour annulée.")
    partial.replace(target)
    return target


def launch_installer(installer: Path) -> None:
    """Lance l'installateur (Windows) en mode silencieux ; l'appelant doit ensuite fermer l'application."""
    if sys.platform != "win32":
        raise UpdateError("L'installation automatique n'est possible que sous Windows.")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],  # noqa: S603
                     creationflags=flags, close_fds=True)
