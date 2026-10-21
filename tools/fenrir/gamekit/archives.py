"""Mods arrive as .zip, .7z or .rar. Zip is read with Python's own zipfile; 7z and rar with the tar.exe Windows ships
(libarchive), which reads both on current Windows 11. Every archive is unpacked into a fresh folder and checked: no path
may leave it, and no link may point out of it."""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

LIMIT = 8 * 2 ** 30  # unpacked size an archive may claim before Fenrir refuses it
ARCHIVE_EXT = (".zip", ".7z", ".rar")


class ArchiveError(Exception):
    """Said to the host as it is."""


def kind_of(path):
    p = Path(path)
    with open(p, "rb") as f:
        head = f.read(8)
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        return "zip"
    if head[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if head[:7] == b"Rar!\x1a\x07\x00" or head[:8] == b"Rar!\x1a\x07\x01\x00":
        return "rar"
    return p.suffix.lower().lstrip(".") or "unknown"


def _check_rel(name):
    parts = [x for x in name.replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in parts:
        raise ArchiveError("The archive holds a path outside its own folder, so it was not used.")
    return parts


def unpack_zip(path, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if sum(i.file_size for i in infos) > LIMIT:
            raise ArchiveError("The archive claims to unpack to more than 8 GB, so it was not used.")
        for i in infos:
            parts = _check_rel(i.filename)
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise ArchiveError("The archive holds a link, so it was not used.")
            target = dest.joinpath(*parts)
            if base not in target.resolve().parents and target.resolve() != base:
                raise ArchiveError("The archive holds a path outside its own folder, so it was not used.")
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
    return dest


def _tar():
    for p in (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe", shutil.which("tar")):
        if p and Path(p).exists():
            return str(p)
    return None


def unpack_tar(path, dest):
    """7z and rar through Windows' tar.exe (bsdtar refuses absolute paths and '..' unless told otherwise); then the same
    check as for zip, on what actually landed."""
    tar = _tar()
    if not tar:
        raise ArchiveError("This Windows has no tar.exe to open .7z and .rar archives. Unpack it yourself and add the folder.")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([tar, "-xf", str(path), "-C", str(dest)], capture_output=True, text=True, timeout=900,
                       creationflags=0x08000000 if os.name == "nt" else 0, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["no reason given"]
        raise ArchiveError(f"Windows could not open this archive ({msg[0][:160]}). Unpack it yourself and add the folder.")
    base = dest.resolve()
    for p in dest.rglob("*"):
        if p.is_symlink() or (base not in p.resolve().parents and p.resolve() != base):
            raise ArchiveError("The archive holds a link or a path outside its own folder, so it was not used.")
    return dest


def unpack(path, dest=None):
    """Any supported archive into `dest` (a new temporary folder when none is given). Returns the folder."""
    dest = Path(dest) if dest else Path(tempfile.mkdtemp(prefix="fenrir-mod-"))
    k = kind_of(path)
    try:
        if k == "zip":
            return unpack_zip(path, dest)
        if k in ("7z", "rar"):
            return unpack_tar(path, dest)
    except zipfile.BadZipFile:
        raise ArchiveError("The archive is damaged.") from None
    raise ArchiveError("Fenrir cannot open this kind of file. Mods come as .zip, .7z or .rar, or as a folder.")


def single_root(folder):
    """Many archives wrap everything in one folder named after the mod: that folder, else the folder itself."""
    folder = Path(folder)
    items = [p for p in folder.iterdir() if p.name not in ("__MACOSX",)]
    if len(items) == 1 and items[0].is_dir():
        return items[0]
    return folder
