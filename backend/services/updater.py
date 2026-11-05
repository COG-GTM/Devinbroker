"""Self-update module — downloads latest GitHub release, backs up current files,
extracts the update over the project, and restarts the app.

Public API:
    perform_update(project_root)  -> dict   (download + backup + extract)
    schedule_restart(project_root)           (spawn detached start script, then exit)
"""
import os
import sys
import logging
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/BigBodyCobain/Shadowbroker/releases/latest"

BACKUP_DIR = os.path.join(tempfile.gettempdir(), "sb_update_backups")
_MAX_BACKUPS = 3

# ---------------------------------------------------------------------------
# Protected patterns — files/dirs that must NEVER be overwritten during update
# ---------------------------------------------------------------------------
_PROTECTED_DIRS = {"venv", "node_modules", ".next", "__pycache__", ".git", ".github", ".claude"}
_PROTECTED_EXTENSIONS = {".db", ".sqlite"}
_PROTECTED_NAMES = {
    ".env",
    "ais_cache.json",
    "carrier_cache.json",
    "geocode_cache.json",
}


def _is_protected(rel_path: str) -> bool:
    """Return True if *rel_path* (forward-slash separated) should be skipped."""
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1]

    # Check directory components
    for part in parts[:-1]:
        if part in _PROTECTED_DIRS:
            return True

    # Check filename
    if name in _PROTECTED_NAMES:
        return True
    _, ext = os.path.splitext(name)
    if ext.lower() in _PROTECTED_EXTENSIONS:
        return True

    return False


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _download_release(temp_dir: str) -> tuple:
    """Fetch latest release info and download the zip asset.
    Returns (zip_path, version_tag, download_url).
    """
    logger.info("Fetching latest release info from GitHub...")
    resp = requests.get(GITHUB_RELEASES_URL, timeout=15)
    resp.raise_for_status()
    release = resp.json()

    tag = release.get("tag_name", "unknown")
    assets = release.get("assets", [])

    # Find the .zip asset
    zip_url = None
    for asset in assets:
        url = asset.get("browser_download_url", "")
        if url.endswith(".zip"):
            zip_url = url
            break

    if not zip_url:
        raise RuntimeError("No .zip asset found in the latest release")

    logger.info(f"Downloading {zip_url} ...")
    zip_path = os.path.join(temp_dir, "update.zip")
    with requests.get(zip_url, stream=True, timeout=120) as dl:
        dl.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in dl.iter_content(chunk_size=1024 * 64):
                f.write(chunk)

    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError("Downloaded file is not a valid ZIP archive")

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    logger.info(f"Downloaded {size_mb:.1f} MB — ZIP validated OK")
    return zip_path, tag, zip_url


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------
def _prune_backups(backup_dir: str, keep: int = _MAX_BACKUPS) -> None:
    """Delete all but the newest *keep* backup zips in backup_dir."""
    try:
        backups = sorted(
            (os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
             if f.startswith("backup_") and f.endswith(".zip")),
            key=os.path.getmtime,
            reverse=True,
        )
    except OSError:
        return
    for old in backups[keep:]:
        try:
            os.remove(old)
        except OSError as e:
            logger.warning(f"Could not remove old backup {old}: {e}")


def _backup_current(project_root: str, backup_dir: str) -> str:
    """Create a backup zip of backend/ and frontend/ in backup_dir."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"backup_{stamp}.zip")
    logger.info(f"Backing up current files to {backup_path} ...")

    dirs_to_backup = ["backend", "frontend"]
    count = 0

    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dir_name in dirs_to_backup:
            dir_path = os.path.join(project_root, dir_name)
            if not os.path.isdir(dir_path):
                continue
            for root, dirs, files in os.walk(dir_path):
                # Prune protected directories from walk
                dirs[:] = [d for d in dirs if d not in _PROTECTED_DIRS]
                for fname in files:
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, project_root)
                    if _is_protected(rel):
                        continue
                    try:
                        zf.write(full, rel)
                        count += 1
                    except (PermissionError, OSError) as e:
                        logger.warning(f"Backup skip (locked): {rel} — {e}")

    logger.info(f"Backup complete: {count} files archived")
    return backup_path


# ---------------------------------------------------------------------------
# Extract & Copy
# ---------------------------------------------------------------------------
def _extract_and_copy(zip_path: str, project_root: str, temp_dir: str) -> int:
    """Extract the update zip and copy files over the project, skipping protected files.
    Returns count of files copied.
    """
    extract_dir = os.path.join(temp_dir, "extracted")
    logger.info("Extracting update zip...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    # Detect wrapper folder: if extracted root has a single directory that
    # itself contains frontend/ or backend/, use it as the real base.
    base = extract_dir
    entries = [e for e in os.listdir(base) if not e.startswith(".")]
    if len(entries) == 1:
        candidate = os.path.join(base, entries[0])
        if os.path.isdir(candidate):
            sub = os.listdir(candidate)
            if "frontend" in sub or "backend" in sub:
                base = candidate
                logger.info(f"Detected wrapper folder: {entries[0]}")

    copied = 0
    skipped = 0

    for root, dirs, files in os.walk(base):
        # Prune protected directories so os.walk never descends into them
        dirs[:] = [d for d in dirs if d not in _PROTECTED_DIRS]

        for fname in files:
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, base).replace("\\", "/")

            if _is_protected(rel):
                skipped += 1
                continue

            dst = os.path.abspath(os.path.join(project_root, rel))
            # Safety: never write outside the project root (zip path traversal)
            if not dst.startswith(os.path.abspath(project_root)):
                logger.warning(f"Safety skip (path traversal): {rel}")
                skipped += 1
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                copied += 1
            except (PermissionError, OSError) as e:
                logger.warning(f"Copy failed (skipping): {rel} — {e}")
                skipped += 1

    logger.info(f"Update applied: {copied} files copied, {skipped} skipped/protected")
    return copied


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------
def schedule_restart(project_root: str):
    """Spawn a detached process that re-runs start.bat / start.sh after a short
    delay, then forcefully exit the current Python process."""
    tmp = tempfile.mkdtemp(prefix="sb_restart_")

    if sys.platform == "win32":
        script = os.path.join(tmp, "restart.bat")
        with open(script, "w") as f:
            f.write("@echo off\n")
            f.write("timeout /t 3 /nobreak >nul\n")
            f.write(f'cd /d "{project_root}"\n')
            f.write("call start.bat\n")

        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            ["cmd", "/c", script],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        script = os.path.join(tmp, "restart.sh")
        with open(script, "w") as f:
            f.write("#!/bin/bash\n")
            f.write("sleep 3\n")
            f.write(f'cd "{project_root}"\n')
            f.write("bash start.sh\n")
        os.chmod(script, 0o755)
        subprocess.Popen(
            ["bash", script],
            start_new_session=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    logger.info("Restart script spawned — exiting current process")
    os._exit(0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def perform_update(project_root: str) -> dict:
    """Download the latest release, back up current files, and extract the update.

    Returns a dict with status info on success, or {"status": "error", "message": ...}
    on failure.  Does NOT trigger restart — caller should call schedule_restart()
    separately after the HTTP response has been sent.
    """
    temp_dir = tempfile.mkdtemp(prefix="sb_update_")
    try:
        zip_path, version, url = _download_release(temp_dir)
        backup_path = _backup_current(project_root, BACKUP_DIR)
        _prune_backups(BACKUP_DIR)
        copied = _extract_and_copy(zip_path, project_root, temp_dir)

        return {
            "status": "ok",
            "version": version,
            "files_updated": copied,
            "backup_path": backup_path,
            "message": f"Updated to {version} — {copied} files replaced. Restarting...",
        }
    except Exception as e:
        logger.error(f"Update failed: {e}", exc_info=True)
        return {
            "status": "error",
            "message": str(e),
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
