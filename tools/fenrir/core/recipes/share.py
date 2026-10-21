"""Share to the library (3.0, LIB-2; docs/v3-design/library.md section 3.2): Check before sharing on this PC, then the
submission as a pre-filled GitHub issue address, or the recipe on the clipboard when the address would be too long.

    check(recipe)          the validator and the library's policy, the share check, then the fake-server harness's whole life
    scan(doc)              the share check for recipes: no path of this PC, no name of this PC or its account, no secret (the
                           store's canary, core/scrub.py, and the share check's token shapes, core/games/sharecheck.py), no
                           network address; an email address or a random-looking string only warns
    submission(recipe, …)  what is sent: the recipe, its notes, the harness report, the live run
    issue(submission)      {url, clipboard, paste}: the address of GitHub's new-issue form with the submission filled in
    prepare(recipe, …)     all of it, as the API answers it

Fenrir only builds the address and the text. The person sends the submission from their own GitHub account, as crash
reports are sent (PLAN-V3 section 7, answer 14): Fenrir holds no GitHub token and calls no GitHub API. Any failure is said
plainly, and then there is no address at all."""
import getpass
import ipaddress
import json
import math
import os
import re
import socket
import urllib.parse
from collections import Counter
from pathlib import Path

from ..i18n import M
from . import RecipeError, load

REPO = "iksamxul/fenrir-library"
NEW_ISSUE = f"https://github.com/{REPO}/issues/new"
TEMPLATE = "submit-recipe.yml"  # its fields, by id: recipe, notes, live, harness (LIB-1 keeps the same ids)
URL_MAX = 7000  # the whole address: longer, and the recipe goes on the clipboard instead
LIVE = ("yes", "no", "disk")  # the live run: done, not done, not possible because of the disk
DRIVE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|]*")
UNC = re.compile(r"(?<![\\\w])\\\\[A-Za-z0-9._$-]+\\[^\s\"'<>|]*")
HOME = re.compile(r"(?<![\w.-])/(?:home|Users)/[^/\s\"'<>|]+[^\s\"'<>|]*|(?<![\w.-])/root(?:/[^\s\"'<>|]*)?(?![\w.-])")
IPV4 = re.compile(r"(?<![\w.-])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w-]|\.[0-9])")
IPV6 = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
RANDOM = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
HEX_HASH = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})")
HASH_KEYS = {"sha256", "digest", "iconSha256", "sha1", "md5"}  # a checksum is meant to look random
LOCAL_ADDRESSES = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "::", "::1"}  # this machine or every address: no one's own


# ---------------------------------------------------------------------------------------------------- the share check
def _strings(x, at=""):
    """(JSON pointer, key, text) for every string in a document."""
    if isinstance(x, str):
        yield at or "/", at.rsplit("/", 1)[-1], x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _strings(v, f"{at}/{str(k).replace('~', '~0').replace('/', '~1')}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from _strings(v, f"{at}/{i}")


def _common_words():
    """Every word in Fenrir's own recipes: an account or a PC called server, steam or world would otherwise be found in
    every recipe there is, so such a name is not looked for."""
    words = set()
    for r in load.builtin().values():
        for _at, _k, s in _strings(r.doc):
            words.update(w.lower() for w in re.findall(r"[A-Za-z0-9]+", s))
    return words


def names_here():
    """{"pc": [...], "account": [...]}: this PC's names and the names of the account Fenrir runs as."""
    pc, account = set(), set()
    for n in (socket.gethostname(), os.environ.get("COMPUTERNAME"), os.environ.get("HOSTNAME")):
        if n:
            pc.update({n, n.split(".")[0]})
    for fn in (getpass.getuser, lambda: os.environ.get("USERNAME"), lambda: os.environ.get("USER"), lambda: Path.home().name):
        try:
            n = fn()
        except Exception:  # noqa: BLE001 - no account name to be had here
            n = None
        if n:
            account.add(n)
    return {"pc": sorted(pc), "account": sorted(account)}


def _name_rx(names, common):
    keep = sorted({n for n in names if len(n) >= 3 and n.lower() not in common}, key=len, reverse=True)
    return re.compile("|".join(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])" for n in keep), re.I) if keep else None


def _entropy(s):
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _address(text):
    """The first network address in a text that belongs to someone (not this machine's loopback, not every address)."""
    for rx in (IPV4, IPV6):
        for m in rx.finditer(text):
            try:
                ip = ipaddress.ip_address(m.group(0))
            except ValueError:
                continue
            if str(ip) not in LOCAL_ADDRESSES and not ip.is_loopback:
                return str(ip)
    return None


def _secrets(text):
    """[(kind, what)] of the secrets in a text, never their values: the store's canary (a value Fenrir keeps, or one of
    its own token shapes) and the share check's (keys and tokens of other services, a private key)."""
    from .. import scrub
    from ..games.sharecheck import SECRETS
    found = list(scrub.find(text))
    found += [("format", what) for what, rx in SECRETS if rx.search(text)]
    return list(dict.fromkeys(found))


def scan(doc, names=None):
    """The share check for a recipe: (fails, warns), each a list of messages saying where in the recipe. names: what to
    look for as this PC's and its account's names (names_here() when None)."""
    names = names_here() if names is None else names
    common = _common_words()
    pc_rx, account_rx = _name_rx(names.get("pc") or [], common), _name_rx(names.get("account") or [], common)
    fails, warns = [], []
    for at, key, s in _strings(doc):
        url = s.startswith(("https://", "http://"))
        for rx in (DRIVE, UNC, *(() if at.startswith("/docker/") else (HOME,))) if not url else ():  # a container's own folders are not this PC's
            m = rx.search(s)
            if m:
                fails.append(M("library.share.path", where=at, path=m.group(0)[:200]))
                break
        if not url and pc_rx and pc_rx.search(s):
            fails.append(M("library.share.pc_name", where=at))
        if not url and account_rx and account_rx.search(s):
            fails.append(M("library.share.account", where=at))
        for kind, what in _secrets(s):
            fails.append(M("library.share.kept", where=at, what=what) if kind == "kept" else M("library.share.format", where=at, what=what))
        try:
            addr = _address((urllib.parse.urlsplit(s).hostname or "") if url else s)
        except ValueError:  # an address that does not even parse: the validator says so
            addr = None
        if addr:
            fails.append(M("library.share.ip", where=at, address=addr))
        if not url and EMAIL.search(s):
            warns.append(M("library.share.email", where=at))
        if key not in HASH_KEYS and not url:
            for m in RANDOM.finditer(s):
                tok = m.group(0)
                if not HEX_HASH.fullmatch(tok) and _entropy(tok) >= 4.2:
                    warns.append(M("library.share.random", where=at))
                    break
    return list(dict.fromkeys(fails)), list(dict.fromkeys(warns))


# ---------------------------------------------------------------------------------------------------- Check before sharing
def text_of(recipe):
    """The recipe's JSON as it is sent: the document, one key a line, as the library keeps recipe.json."""
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def _harness(doc):
    from .harness import run
    return run.run(doc, origin="library")


def _failed_step(report):
    for st in (report or {}).get("steps") or []:
        if st.get("ok") is False:
            return st.get("step") or "?", str(st.get("said") or "")[:300]
    return "?", str((report or {}).get("status") or "")


def summary(report):
    """The harness report, short: {status, seconds, steps: [[step, ok]]}."""
    report = report or {}
    return {"status": report.get("status"), "seconds": report.get("seconds"),
            "steps": [[st.get("step"), st.get("ok")] for st in report.get("steps") or []]}


def check(recipe, harness=None, names=None):
    """Check before sharing (library.md section 3.2, step 1): the validator and the policy as the library reads a recipe,
    then the share check, then the harness's whole life (only for a recipe that passed the rest: one that does not
    validate never runs). {"ok", "fails", "warns", "harness"}: ok only when nothing failed."""
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    fails, warns, report = [], [], None
    try:
        load.read(text_of(doc).encode("utf-8"), "library")
    except RecipeError as e:
        fails.append(M("library.share.invalid", why=str(e)[:600]))
    rid = doc.get("id") if isinstance(doc, dict) else None
    if isinstance(rid, str) and rid in load.builtin():
        fails.append(M("library.share.builtin_id", id=rid))
    f2, w2 = scan(doc, names)
    fails += f2
    warns += w2
    if not ((doc.get("research") or {}).get("notes") if isinstance(doc.get("research"), dict) else None):
        warns.append(M("library.share.no_notes"))
    if (((doc.get("tests") or {}).get("live") or {}).get("result")) != "pass":
        warns.append(M("library.share.no_live"))
    if not fails:
        report = (harness or _harness)(doc)
        if (report or {}).get("status") != "passed":
            step, said = _failed_step(report)
            fails.append(M("library.share.harness", step=step, said=said or "-"))
    return {"ok": not fails, "fails": fails, "warns": warns, "harness": summary(report) if report else None}


def _harness_line(h):
    if not h:
        return "not run"
    steps = ", ".join(s for s, ok in h["steps"] if ok)
    return f"{h['status']} in {h['seconds']} s ({steps})"


def submission(recipe, report=None, notes=None, live="no"):
    """What is sent (library.md section 3.2, step 2): the recipe, its notes (its research notes when none are given), the
    harness report (summary()'s), and the live run (yes, no, or disk: not possible because of the disk)."""
    doc = recipe.doc if hasattr(recipe, "doc") else recipe
    notes = notes if isinstance(notes, str) and notes.strip() else ((doc.get("research") or {}).get("notes") or "")
    return {"id": doc.get("id"), "name": doc.get("name") or doc.get("id"), "recipe": text_of(doc), "notes": notes.strip()[:4000],
            "harness": _harness_line(report), "live": live if live in LIVE else "no"}


def _address_of(params):
    q = "&".join(f"{k}={urllib.parse.quote(str(v), safe='').replace('~', '%7E')}" for k, v in params.items())
    return f"{NEW_ISSUE}?{q}"


def issue(sub, limit=URL_MAX):
    """The address of the library's new-issue form, filled in: {url, clipboard, paste}. The recipe goes into the address
    while the whole stays within `limit` characters; else it is clipboard, for the page to put on the clipboard, and paste
    says where it goes. Fenrir opens the address; the person sends it."""
    base = {"template": TEMPLATE, "title": f"Recipe: {sub['name']}", "id": sub["id"], "live": sub["live"], "harness": sub["harness"],
            "notes": sub["notes"]}
    url = _address_of({**base, "recipe": sub["recipe"]})
    if len(url) <= limit:
        return {"url": url, "clipboard": None, "paste": None}
    notes = base["notes"]
    while len(_address_of(base)) > limit and notes:
        notes = notes[: int(len(notes) * 0.8)].rstrip()
        base["notes"] = notes + " (more in the recipe's research notes)" if notes else ""
    return {"url": _address_of(base), "clipboard": sub["recipe"], "paste": M("library.share.paste")}


def prepare(recipe, notes=None, live="no", harness=None, names=None):
    """Share to the library, as the API answers it: the checks, and when they all passed the address and what goes on the
    clipboard. Nothing is opened or sent here."""
    out = check(recipe, harness=harness, names=names)
    out.update(url=None, clipboard=None, paste=None, ready=None)
    if out["ok"]:
        out.update(issue(submission(recipe, out["harness"], notes, live)), ready=M("library.share.ready"))
    return out
