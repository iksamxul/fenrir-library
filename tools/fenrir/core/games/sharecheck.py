"""A check before a mod of the host's own goes to friends (2.2), after universal-modder's publish check (um/publish.py,
github.com/rehan-remade/universal-modder, MIT). Friends download what "Let friends get it" marks from this PC; a mod a
person made (a mashup most of all) can hold, without anyone meaning it to:

    stops    the game's own art, sound, levels or archives, or any big file identical to one of the game's (or another game's
             of the mashup): redistributing game files;
             an API key, a token or a private key; a .env file or a key file
             a secret Fenrir keeps, or one of its known shapes (3.0, core/scrub.py: the canary)
    warns    a small file, a licence text or a runtime library that matches one of the game's (games and mods ship the same
             ones); names a decompiler gives or its header in source files; a big engine archive; this PC's user folder written
             into a file, or built into a program by the compiler (the path of its debug file)

It checks exactly the files friends would get (core/games/share.py payload()), when "Let friends get it" is pressed and
again whenever a newer copy of the mod is packed. Files Fenrir itself placed in a game's folder are not the game's, and
.fenrir-kept (what Fenrir set aside there) is skipped; the game's originals in .fenrir-originals still count. It is a check,
not legal advice."""
import hashlib
import os
import re
import time
from pathlib import Path

SECRETS = [  # (what, pattern): after um/publish.py SECRET_PATTERNS
    ("a fal key", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9a-f]{32}\b")),
    ("a fal key", re.compile(r"FAL_KEY\s*[=:]\s*['\"]?[A-Za-z0-9:_\-]{20,}")),
    ("an Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("an OpenAI key", re.compile(r"\bsk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{32,}|[A-Za-z0-9]{32,})")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,})")),
    ("an AWS key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
]
DECOMP = [
    ("names Ghidra gives", re.compile(r"\b(?:FUN|DAT|LAB|PTR)_[0-9a-fA-F]{6,}\b")),
    ("names IDA gives", re.compile(r"\b(?:sub|loc|unk|off|dword|qword)_[0-9A-F]{5,}\b")),
    ("a decompiler's header", re.compile(r"^\s*(?://|#|--|;)\s*(?:Decompiled with|Decompiler:|ILSpy|dnSpy|JetBrains decompiler)|^#region Assembly ", re.I | re.M)),
]
CODE_EXT = {".cs", ".c", ".cpp", ".h", ".hpp", ".py", ".lua", ".js", ".ts", ".rs", ".java", ".kt", ".gd", ".rpy", ".psc", ".gml",
            ".hlsl", ".glsl", ".as", ".vb", ".il"}
TEXT_EXT = CODE_EXT | {".json", ".toml", ".ini", ".cfg", ".txt", ".md", ".xml", ".yaml", ".yml", ".sh", ".ps1", ".bat", ".env", ".pem", ".asc", ".conf"}
BIN_EXT = {".dll", ".asi", ".exe", ".so"}  # programs: the compiler writes the path of the debug file into them
KEY_FILES = re.compile(r"^(id_(rsa|dsa|ecdsa|ed25519)|.*\.(pem|key|ppk|p12|pfx|asc|keystore|jks))$", re.I)
ARCHIVE_EXT = {".pak", ".utoc", ".ucas", ".bsa", ".ba2", ".vpk", ".rpf", ".pck", ".assets", ".bundle", ".sga", ".big", ".wad", ".bdt", ".archive"}
ASSET_EXT = ARCHIVE_EXT | {".png", ".dds", ".tga", ".jpg", ".jpeg", ".bmp", ".ogg", ".wav", ".mp3", ".flac", ".bnk", ".wem", ".fsb", ".bik", ".usm",
                           ".mesh", ".fbx", ".nif", ".uasset", ".umap", ".ubulk", ".resource", ".ytd", ".ydr", ".yft", ".esm", ".esp", ".esl"}
# files games and mods ship alike: a match is worth a look, never proof of a copied game file
SHARED_NAMES = re.compile(r"^(vcruntime|msvcp|msvcr|concrt|vccorlib|ucrtbase|api-ms-win-|d3dcompiler_|d3dx|xinput|xaudio|newtonsoft\.json|"
                          r"0harmony|harmony|mono\.cecil|monomod|bepinex|system\.|microsoft\.|netstandard|dinput8|version|winmm|dsound|winhttp|"
                          r"license|licence|copying|notice|credits|readme|third.?party)", re.I)
USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'<>|]+", re.I)
USER_PATH_BIN = re.compile(rb"[A-Za-z]:\\Users\\[^\\\x00]{1,64}\\", re.I)
SKIP = {".git", ".vs", "obj", "node_modules", "__pycache__", ".history"}
SKIP_IN_GAME = {".fenrir-kept"}  # what Fenrir set aside in a game's folder: the game's originals are in .fenrir-originals
MIN_SIZE = 64  # smaller files match by chance (an empty config, a one-line text)
BIG = 64 * 1024  # a match this big (or any art, sound or archive) is a copied game file
TEXT_MAX = 4 * 1024 * 1024
BIN_MAX = 64 * 1024 * 1024
BUDGET = 25.0  # seconds for comparing with the games' files
_index = {}  # folder -> (time, {size: [paths]}): a game's folder read once in a while, not at every press


def _sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sizes_of(folder, deadline):
    """{size: [path]} of a game's folder (cached ten minutes), and whether it was read whole before the deadline."""
    key = os.path.normcase(os.path.abspath(folder))
    hit = _index.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1], True
    out, whole = {}, True
    stack = [folder]
    while stack:
        if time.monotonic() > deadline:
            whole = False
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name.lower() not in SKIP_IN_GAME:
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            out.setdefault(e.stat().st_size, []).append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    if whole:
        _index[key] = (time.time(), out)
    return out, whole


def _canary(rel, content):
    """The canary (3.0, store.md §3.7): a value Fenrir keeps, or one of its secrets' shapes, in what friends would get. Said by
    what it is, never by its value."""
    from .. import scrub
    from ..i18n import M
    return [M("share.kept", file=rel, what=what) if kind == "kept" else M("share.format", file=rel, what=what) for kind, what in scrub.find(content)]


def folder_files(mod):
    """[(relative path, path)] of a folder, as check_files takes them."""
    out = []
    for dirpath, dirs, files in os.walk(mod):
        dirs[:] = [d for d in dirs if d not in SKIP]
        out += [(Path(dirpath, f).relative_to(mod).as_posix(), Path(dirpath, f)) for f in files]
    return out


def check(mod, games, budget=BUDGET):
    """check_files for every file of the folder `mod`."""
    return check_files(folder_files(Path(mod)), games, budget)


def check_files(files, games, budget=BUDGET):
    """{"fails": [...], "warns": [...], "files": n} for `files` [(relative path, path)]. `games`: [(name, folder, placed)],
    placed the keys (lower-case relative paths) of the files Fenrir put into that folder (None: its record could not be read,
    so a match may be one of Fenrir's own files)."""
    fails, warns = [], []
    sizes = {}
    for rel, f in files:
        try:
            s = f.stat().st_size
        except OSError:
            continue
        if s >= MIN_SIZE:
            sizes.setdefault(s, []).append((rel, f))
    hashes = {}
    deadline = time.monotonic() + budget
    for name, folder, placed in games:
        if not folder or not Path(folder).is_dir():
            continue
        index, whole = sizes_of(folder, deadline)
        late = not whole
        for s, mine in sizes.items():
            for gp in index.get(s, []):
                if time.monotonic() > deadline:
                    late = True
                    break
                grel = os.path.relpath(gp, folder).replace("\\", "/").lower()
                if placed is not None and grel in placed:
                    continue  # a mod's file Fenrir put there, not the game's
                try:
                    gh = _sha1(gp)
                except OSError:
                    continue
                for rel, f in mine:
                    if f not in hashes:
                        try:
                            hashes[f] = _sha1(f)
                        except OSError:
                            hashes[f] = None
                    if hashes[f] != gh:
                        continue
                    shared = SHARED_NAMES.match(Path(rel).name)
                    hard = not shared and (Path(rel).suffix.lower() in ASSET_EXT or s >= BIG)
                    note = "" if placed is not None else " (Fenrir's record of the files it put there could not be read: it may be one of those)"
                    if hard:
                        fails.append(f"{rel} is {name}'s own file ({grel}): friends get the game's files from their own copy{note}")
                    else:
                        warns.append(f"{rel} is the same as {name}'s {grel}: fine if it is a library or text both ship, not if it came from the game{note}")
            if late:
                break
        if late:
            warns.append(f"Fenrir could not compare every file with {name}'s in time: what it compared is listed here")
    for rel, f in files:
        ext, low = f.suffix.lower(), f.name.lower()
        if low.startswith(".env") or low.endswith(".env"):
            fails.append(f"{rel} is a .env file, where keys and passwords are kept")
            continue
        if KEY_FILES.match(low):
            fails.append(f"{rel} is a key or certificate file: it stays on this PC")
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if ext in ARCHIVE_EXT and size > 5 * 1024 * 1024:
            warns.append(f"{rel} is a big engine archive ({size >> 20} MB): make sure it holds only what you made, never the game's own assets")
        if ext in BIN_EXT and size <= BIN_MAX:
            try:
                data = f.read_bytes()
                if USER_PATH_BIN.search(data):
                    warns.append(f"{rel} has this PC's user folder built into it (the compiler writes the path of its debug file): "
                                 "friends see your user name in it")
                fails += _canary(rel, data)
            except OSError:
                pass
            continue
        text = None
        if (ext in TEXT_EXT or not ext) and size <= TEXT_MAX:
            try:
                raw = f.read_bytes()
                if ext or b"\x00" not in raw[:4096]:  # a file without an extension is read when it is text
                    text = raw.decode("utf-8", "replace")
            except OSError:
                text = None
        if text is None:
            continue
        for what, rx in SECRETS:
            if rx.search(text):
                fails.append(f"{rel} holds {what}: take it out (and replace the key, since it has been in a file)")
                break
        fails += _canary(rel, text)
        if ext in CODE_EXT:
            for what, rx in DECOMP:
                found = rx.findall(text)
                if found:
                    warns.append(f"{rel} has {what} ({len(found)}, like {str(found[0]).strip()[:40]}): decompiled code is the game maker's, write your own")
                    break
        if USER_PATH.search(text):
            warns.append(f"{rel} names a folder of this PC's user (C:\\Users\\...): friends do not have it, and it shows your user name")
    return {"fails": list(dict.fromkeys(fails)), "warns": list(dict.fromkeys(warns)), "files": len(files)}
