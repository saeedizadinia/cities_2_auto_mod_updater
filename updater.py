"""Paradox Mods / Cities Skylines II manual folder updater.
Run `python updater.py --help`. Python 3.10+.
"""

from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

APP = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "CS2ManualModUpdater"
DEFAULT_MODS = (
    Path(os.getenv("USERPROFILE", str(Path.home())))
    / "AppData/LocalLow/Colossal Order/Cities Skylines II/Mods"
)
CONFIG = APP / "config.json"
LOG = APP / "updater.log"
API = "https://api.paradox-interactive.com/mods"
CONTENT = "https://modscontent.paradox-interactive.com"
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")
MAX_ARCHIVE = 2 * 1024**3
MAX_UNPACKED = 4 * 1024**3


class UpdaterError(Exception):
    pass


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def default_config():
    return {
        "mods_dir": str(DEFAULT_MODS),
        "winrar_path": r"C:\Program Files\WinRAR\WinRAR.exe",
        "mappings": {"move-it": {"mod_id": 74324, "name": "Move It"}},
        "allow_automatic_install": False,
        "keep_backups": 3,
    }


def load_config():
    APP.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        atomic_json(CONFIG, default_config())
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(cfg.get("mappings"), dict):
        raise UpdaterError("Invalid mappings in config.")
    return cfg


def setup_log():
    APP.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def slug(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.casefold())).strip("-")


def is_link_like(path):
    """Detect symlinks and, where supported by Python, Windows junctions."""
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def parse_folder(name):
    m = re.fullmatch(r"(.+)_([0-9]+)", name)
    if not m:
        return None
    return slug(m.group(1)), int(m.group(2))


def network():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(
        {"User-Agent": "CS2ManualModUpdater/1.0", "Accept": "application/json"}
    )
    return s


def metadata(session, mod_id):
    r = session.get(
        API, params={"modId": int(mod_id), "os": "Windows"}, timeout=(10, 30)
    )
    r.raise_for_status()
    payload = r.json()
    m = payload.get("modDetail") if isinstance(payload, dict) else None
    if not isinstance(m, dict):
        raise UpdaterError("API returned no modDetail; endpoint might have changed.")
    display_name = m.get("displayName")
    name = m.get("name")
    revision = m.get("modVersion")
    game = m.get("game")
    if (
        not isinstance(name, str)
        or not isinstance(game, str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]+", name)
        or not re.fullmatch(r"[a-zA-Z0-9_-]+", game)
    ):
        raise UpdaterError("Unsafe or missing mod name/game from API.")
    if not re.fullmatch(r"\d+", str(revision)):
        raise UpdaterError(
            f"Non-numeric modVersion {revision!r}; cannot compare with folder suffix."
        )
    # Important: validate game to avoid installing mods from a different game.
    if slug(game) not in (
        "cities-skylines-2",
        "cities-skylines-ii",
        "cities-skylines2",
    ) and game not in ("cities_skylines_2",):
        raise UpdaterError(f"Unexpected game identifier: {game}")
    return name, int(revision), game, display_name


def search_mods(session, query, page_size=25):
    """Search published Cities: Skylines II mods using Paradox's web API."""
    r = session.post(
        API,
        params={"metadata": "yes"},
        json={
            "gameName": "cities_skylines_2",
            "searchQuery": query,
            "page": 0,
            "pageSize": page_size,
            "sortBy": "best",
            "orderBy": "desc",
        },
        timeout=(10, 45),
    )
    r.raise_for_status()
    payload = r.json()
    mods = payload.get("mods") if isinstance(payload, dict) else None
    if not isinstance(mods, list):
        raise UpdaterError("Mod search returned an unexpected response")
    return [
        item
        for item in mods
        if isinstance(item, dict)
        and item.get("game") == "cities_skylines_2"
        and item.get("state") == "published"
        and item.get("os") in ("Windows", "Any")
        and str(item.get("modId", "")).isdigit()
        and isinstance(item.get("displayName"), str)
    ]


def _compact_name(text):
    return slug(text).replace("-", "")


def _folder_search_terms(key, installed_path):
    terms = [key.replace("-", " ")]
    # Compact folder names often preserve CamelCase in their main DLL filename.
    for dll in installed_path.glob("*.dll"):
        stem = re.sub(r"_(?:win|linux|mac)_.*$", "", dll.stem, flags=re.I)
        if _compact_name(stem) != _compact_name(key):
            continue
        separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
        if separated.casefold() not in {term.casefold() for term in terms}:
            terms.append(separated)
    return terms


def find_mod_mapping(session, key, installed_path):
    """Return one unambiguous exact-name match plus useful review candidates."""
    candidates = {}
    terms = _folder_search_terms(key, installed_path)
    # Inspect a full result page so a lower-ranked duplicate exact name cannot
    # be hidden behind the first result and silently selected.
    for term in terms:
        for item in search_mods(session, term, 100):
            candidates[int(item["modId"])] = item
    exact = [
        item
        for item in candidates.values()
        if _compact_name(item["displayName"]) == _compact_name(key)
    ]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, exact

    ranked = sorted(
        candidates.values(),
        key=lambda item: SequenceMatcher(None, slug(item["displayName"]), key).ratio(),
        reverse=True,
    )
    return None, ranked[:3]


def auto_map_mods(cfg, session, emit=lambda s: None):
    """Populate missing mappings, never replacing a manual/existing mapping."""
    mods_dir = Path(os.path.expandvars(cfg["mods_dir"])).expanduser()
    found = installed_mods(mods_dir)
    pending = [
        (key, entries[0][0])
        for key, entries in sorted(found.items())
        if len(entries) == 1 and not cfg["mappings"].get(key, {}).get("mod_id")
    ]
    if not pending:
        emit("All uniquely installed mods already have ID mappings.")
        return 0, 0

    emit(f"Automatically searching IDs for {len(pending)} unmapped mods...")
    results = {}
    with ThreadPoolExecutor(max_workers=min(6, len(pending))) as pool:
        jobs = {
            pool.submit(find_mod_mapping, session, key, path): key
            for key, path in pending
        }
        for job in as_completed(jobs):
            key = jobs[job]
            try:
                results[key] = job.result()
            except Exception as exc:
                logging.exception("Automatic ID search failed for %s", key)
                results[key] = (None, [])
                emit(f"Auto-map error for {key}: {exc}")

    mapped = 0
    for key, _path in pending:
        match, suggestions = results[key]
        if match:
            cfg["mappings"][key] = {
                "mod_id": int(match["modId"]),
                "name": match["displayName"],
                "source": "automatic",
            }
            mapped += 1
            emit(f'Auto-mapped {key} -> {match["displayName"]} (ID {match["modId"]})')
        elif suggestions:
            choices = ", ".join(
                f'{item["displayName"]} (ID {item["modId"]})' for item in suggestions
            )
            emit(f"Review {key} manually; closest results: {choices}")
        else:
            emit(f"No safe ID match found for {key}; use Edit Mod ID.")
    unresolved = len(pending) - mapped
    emit(f"Automatic ID search finished: {mapped} mapped, {unresolved} need review.")
    return mapped, unresolved


def make_download_url(game, name, revision):
    # Observed Paradox content scheme; never permit external redirects in downloader.
    return f"{CONTENT}/{quote(game)}/{quote(name)}/content/sources/{quote(name)}_{revision}.zip"


def installed_mods(mods_dir):
    if not mods_dir.exists():
        raise UpdaterError(f"Mods directory does not exist: {mods_dir}")
    result = {}
    for p in mods_dir.iterdir():
        if p.is_dir() and not p.is_symlink():
            parsed = parse_folder(p.name)
            if parsed:
                result.setdefault(parsed[0], []).append((p, parsed[1]))
    return result


@dataclass
class Plan:
    key: str
    installed_path: Path
    installed_revision: int
    online_revision: int | None
    mod_id: int | None
    api_name: str | None
    game: str | None
    status: str
    detail: str = ""


def plan_updates(cfg, session, emit=lambda s: None):
    mods_dir = Path(os.path.expandvars(cfg["mods_dir"])).expanduser()
    found = installed_mods(mods_dir)
    plans = []
    for key, entries in sorted(found.items()):
        if len(entries) != 1:
            for p, rev in entries:
                plans.append(
                    Plan(
                        key,
                        p,
                        rev,
                        None,
                        None,
                        None,
                        None,
                        "AMBIGUOUS",
                        "Multiple folders for same slug",
                    )
                )
            continue
        p, rev = entries[0]
        mapping = cfg["mappings"].get(key)
        if not mapping or not mapping.get("mod_id"):
            plans.append(
                Plan(
                    key,
                    p,
                    rev,
                    None,
                    None,
                    None,
                    None,
                    "UNMAPPED",
                    "Click Edit Mod ID to set the Paradox mod ID",
                )
            )
            continue
        try:
            mid = int(mapping["mod_id"])
            name, online, game, display_name = metadata(session, mid)
            manual = mapping.get("source") == "manual"
            if slug(display_name) != key and not manual:
                raise UpdaterError(
                    f"Name mismatch: folder {key!r}, API {display_name!r}. Confirm mapping and aliases."
                )
            status = (
                "UPDATE"
                if online > rev
                else ("CURRENT" if online == rev else "LOCAL_NEWER")
            )
            detail = (
                f'Manual ID points to "{display_name}"'
                if manual and slug(display_name) != key
                else ""
            )
            plans.append(Plan(key, p, rev, online, mid, name, game, status, detail))
        except Exception as exc:
            logging.exception("Metadata check failed for %s", key)
            plans.append(
                Plan(
                    key,
                    p,
                    rev,
                    None,
                    mapping.get("mod_id"),
                    None,
                    None,
                    "ERROR",
                    str(exc),
                )
            )
        emit(f"Checked {p.name}: {plans[-1].status}")
    return plans


def download(session, url, dest):
    allowed = {"modscontent.paradox-interactive.com"}
    digest = hashlib.sha256()
    with session.get(url, stream=True, timeout=(15, 120), allow_redirects=True) as r:
        r.raise_for_status()
        for hop in list(r.history) + [r]:
            u = urlparse(hop.url)
            if u.scheme != "https" or u.hostname not in allowed:
                raise UpdaterError("Download redirected to an unapproved host")
        content_type = r.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            raise UpdaterError("Download returned HTML/JSON rather than an archive")
        n = 0
        with dest.open("wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                n += len(chunk)
                if n > MAX_ARCHIVE:
                    raise UpdaterError("Archive exceeds 2 GB safety limit")
                f.write(chunk)
                digest.update(chunk)
        if n < 22:
            raise UpdaterError("Downloaded archive is suspiciously small")
    with dest.open("rb") as f:
        signature = f.read(8)
    if not (
        signature.startswith(b"PK\x03\x04") or signature.startswith(b"Rar!\x1a\x07")
    ):
        raise UpdaterError("Downloaded content is not ZIP/RAR")
    return digest.hexdigest()


def winrar_binary(cfg):
    p = Path(os.path.expandvars(cfg.get("winrar_path", "")))
    if p.is_file():
        return p
    found = shutil.which("WinRAR.exe") or shutil.which("Rar.exe")
    if found:
        return Path(found)
    raise UpdaterError("WinRAR missing: configure winrar_path")


def safe_archive_names(archive, executable):
    """List a non-ZIP archive with WinRAR and reject unsafe paths."""
    proc = subprocess.run(
        [str(executable), "lb", str(archive)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=90,
    )
    if proc.returncode != 0:
        raise UpdaterError("WinRAR cannot list archive: " + proc.stderr[-500:])
    names = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
    if not names:
        raise UpdaterError("Empty archive")
    for name in names:
        clean = name.replace("\\", "/")
        bits = clean.split("/")
        if (
            clean.startswith("/")
            or re.match(r"^[a-zA-Z]:", clean)
            or ".." in bits
            or ":" in clean
            or any(x in ("", ".") for x in bits[:-1])
        ):
            raise UpdaterError("Unsafe archive entry: " + name)
    return names


def safe_archive_name(name):
    """Return normalized archive path parts, rejecting Windows/path traversal tricks."""
    clean = name.replace("\\", "/")
    bits = clean.split("/")
    if bits and bits[-1] == "":
        bits.pop()
    if (
        not bits
        or clean.startswith("/")
        or re.match(r"^[a-zA-Z]:", clean)
        or any(bit in ("", ".", "..") for bit in bits)
        or any(":" in bit for bit in bits)
    ):
        raise UpdaterError("Unsafe archive entry: " + name)
    return bits


def extract_zip(archive, destination):
    """Safely extract ZIP without depending on WinRAR console output."""
    destination.mkdir()
    try:
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            if not entries:
                raise UpdaterError("Empty archive")

            total_declared = 0
            seen = set()
            validated = []
            for info in entries:
                bits = safe_archive_name(info.filename)
                key = "/".join(bits).casefold()
                if key in seen:
                    raise UpdaterError("Duplicate archive entry: " + info.filename)
                seen.add(key)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise UpdaterError(
                        "Archive contains a symbolic link: " + info.filename
                    )
                total_declared += info.file_size
                if total_declared > MAX_UNPACKED:
                    raise UpdaterError("Extracted files exceed 4 GB limit")
                validated.append((info, bits))

            written = 0
            for info, bits in validated:
                target = destination.joinpath(*bits)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > MAX_UNPACKED:
                            raise UpdaterError("Extracted files exceed 4 GB limit")
                        output.write(chunk)
    except UpdaterError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UpdaterError(f"ZIP extraction failed: {exc}") from exc


def extract(archive, executable, destination):
    if zipfile.is_zipfile(archive):
        extract_zip(archive, destination)
        top = list(destination.iterdir())
        if len(top) == 1 and top[0].is_dir():
            return top[0]
        return destination

    if executable is None:
        raise UpdaterError("WinRAR is required to extract this non-ZIP archive")
    safe_archive_names(archive, executable)
    destination.mkdir()
    proc = subprocess.run(
        [str(executable), "x", "-o-", "-y", str(archive), str(destination) + os.sep],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=600,
    )
    if proc.returncode != 0:
        raise UpdaterError(
            "WinRAR extraction failed: " + (proc.stderr or proc.stdout)[-500:]
        )
    all_files = [p for p in destination.rglob("*") if p.is_file()]
    if not all_files:
        raise UpdaterError("Archive extracted no files")
    if any(is_link_like(p) for p in destination.rglob("*")):
        raise UpdaterError("Extracted archive contains links/junctions")
    total = sum(p.stat().st_size for p in all_files)
    if total > MAX_UNPACKED:
        raise UpdaterError("Extracted files exceed 4 GB limit")
    # Archive may wrap all contents in one root folder; unwrap exactly once.
    top = list(destination.iterdir())
    if len(top) == 1 and top[0].is_dir():
        return top[0]
    return destination


def deploy(plan, cfg, session, emit=lambda s: None):
    if (
        plan.status != "UPDATE"
        or plan.online_revision is None
        or plan.api_name is None
        or plan.game is None
    ):
        raise UpdaterError("No validated update to install")
    root = plan.installed_path.parent
    if not plan.installed_path.is_dir() or plan.installed_path.is_symlink():
        raise UpdaterError("Original folder is absent or linked")
    newer_name = f"{plan.key}_{plan.online_revision}"
    newer = root / newer_name
    if newer.exists():
        raise UpdaterError(f"Target already exists: {newer}")
    backup_root = APP / "backups" / plan.key
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = backup_root / f"{plan.installed_path.name}_{timestamp}"
    with tempfile.TemporaryDirectory(prefix="cs2mod_", dir=str(APP)) as tmp:
        tmp = Path(tmp)
        archive = tmp / "mod.zip"
        emit(f"Downloading {plan.key} revision {plan.online_revision}...")
        sha = download(
            session,
            make_download_url(plan.game, plan.api_name, plan.online_revision),
            archive,
        )
        emit(f"Extracting {plan.key}...")
        # Paradox currently serves ZIP files, which Python can extract reliably.
        # Keep WinRAR as a fallback in case the service returns RAR in the future.
        extractor = None if zipfile.is_zipfile(archive) else winrar_binary(cfg)
        staged = extract(archive, extractor, tmp / "unpacked")
        # Close the game before updating, and stage on the destination volume.
        prepared = root / f".cs2updater_staging_{plan.key}_{timestamp}"
        if prepared.exists():
            raise UpdaterError("Staging collision")
        try:
            shutil.copytree(staged, prepared)
            os.replace(plan.installed_path, backup)
            try:
                os.replace(prepared, newer)
            except Exception:
                os.replace(backup, plan.installed_path)
                raise
            receipt = {
                "timestamp": timestamp,
                "mod_id": plan.mod_id,
                "previous_folder": plan.installed_path.name,
                "installed_folder": newer.name,
                "revision": plan.online_revision,
                "sha256": sha,
                "backup": str(backup),
            }
            receipts = APP / "receipts"
            atomic_json(receipts / f"{plan.key}.json", receipt)
            emit(f"Installed {newer.name}; backup: {backup}")
            logging.info("Installed %s", receipt)
        finally:
            if prepared.exists():
                shutil.rmtree(prepared)
    # Retain at most N backups, but never delete backup referenced in newest receipt.
    retain = max(1, int(cfg.get("keep_backups", 3)))
    backups = sorted(
        (p for p in backup_root.iterdir() if p.is_dir()),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    for old in backups[retain:]:
        shutil.rmtree(old)


def rollback(key, cfg):
    receipt_file = APP / "receipts" / f"{key}.json"
    if not receipt_file.is_file():
        raise UpdaterError("No installed update receipt for this mod")
    rec = json.loads(receipt_file.read_text(encoding="utf-8"))
    mods = Path(os.path.expandvars(cfg["mods_dir"])).expanduser().resolve()
    current = mods / rec["installed_folder"]
    backup = Path(rec["backup"]).resolve()
    if not backup.is_relative_to((APP / "backups").resolve()):
        raise UpdaterError("Backup is outside the controlled backup directory")
    original = mods / rec["previous_folder"]
    if not current.is_dir() or not backup.is_dir() or original.exists():
        raise UpdaterError(
            "Cannot roll back: current/backup missing, or old folder already exists"
        )
    parked = mods / (".cs2rollback_" + key)
    if parked.exists():
        raise UpdaterError("Rollback staging folder already exists")
    os.replace(current, parked)
    try:
        os.replace(backup, original)
    except Exception:
        os.replace(parked, current)
        raise
    # Keep failed update parked for manual examination; no silent deletion.
    receipt_file.unlink()
    return f"Restored {original.name}. Superseded update kept at {parked}"


def clear_backups():
    """Delete updater-owned backups and their now-unusable rollback receipts."""
    app_root = APP.resolve()
    backup_root = APP / "backups"
    receipts_root = APP / "receipts"
    if backup_root.name != "backups" or backup_root.parent.resolve() != app_root:
        raise UpdaterError("Refusing to clear an unexpected backup path")
    if backup_root.exists() and not backup_root.is_dir():
        raise UpdaterError("Backup path is not a directory")
    if is_link_like(backup_root):
        raise UpdaterError("Refusing to clear a linked backup directory")
    if receipts_root.exists() and not receipts_root.is_dir():
        raise UpdaterError("Receipts path is not a directory")
    if is_link_like(receipts_root):
        raise UpdaterError("Refusing to clear a linked receipts directory")

    files = 0
    size = 0
    if backup_root.exists():
        for path in backup_root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                files += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
        for child in list(backup_root.iterdir()):
            if is_link_like(child):
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        backup_root.mkdir(parents=True)

    # A receipt cannot support rollback after its referenced backup is removed.
    if receipts_root.exists():
        for receipt in receipts_root.glob("*.json"):
            receipt.unlink()

    mib = size / (1024 * 1024)
    return (
        f"Cleared {files} backup files ({mib:.1f} MiB). Rollback receipts were cleared."
    )


def execute(apply=False, callback=print):
    setup_log()
    cfg = load_config()
    s = network()
    mapped, _unresolved = auto_map_mods(cfg, s, callback)
    if mapped:
        atomic_json(CONFIG, cfg)
    plans = plan_updates(cfg, s, callback)
    if apply:
        for p in plans:
            if p.status == "UPDATE":
                try:
                    deploy(p, cfg, s, callback)
                except Exception as e:
                    logging.exception("Failed installing %s", p.key)
                    callback(f"ERROR updating {p.key}: {e}")
    return plans


def main():
    parser = argparse.ArgumentParser(
        description="CS2 manual Paradox mod updater (preview by default)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Install validated updates, keeping backups",
    )
    parser.add_argument(
        "--rollback", metavar="SLUG", help="Restore most recent backup for slug"
    )
    parser.add_argument("--gui", action="store_true", help="Open desktop GUI")
    args = parser.parse_args()
    if args.gui:
        from gui import launch

        launch()
    elif args.rollback:
        print(rollback(slug(args.rollback), load_config()))
    else:
        plans = execute(args.apply)
        for p in plans:
            print(
                f'{p.installed_path.name:35} {str(p.online_revision or "-"):6} {p.status:13} {p.detail}'
            )


if __name__ == "__main__":
    main()
