#!/usr/bin/env python3
"""Step 1 of the library's checks: take the submission.

    python -I tools/take.py issue --event <event.json> --out <folder>
        A submission made with the issue form (.github/ISSUE_TEMPLATE/submit-recipe.yml). The issue's text is read as
        text and nothing in it is run: the form's fields are found by their headings, the recipe is read as JSON (one
        object, no key twice), and its id is checked before it names a folder. Writes recipe.json (the recipe written
        the one way Fenrir writes it), README.md (the research notes) and pr-body.md into the folder.

    python -I tools/take.py pr --files <files.json> --base <folder>
        A pull request: the files it changes, as GitHub's API lists them. It must change one recipe's folder,
        recipes/<id>/, and nothing else: recipe.json, README.md and an icon there.

Both print what they found, and write their answer for the next jobs to $GITHUB_OUTPUT (single-line JSON, so no text
from the submission can add an output of its own). The exit code is 0 when the submission was taken, 1 when it was not.

This script uses Python's standard library only and imports nothing of Fenrir: it runs in the one job that may write to
the repository (a branch and a pull request), so it never runs the checks themselves. Those run afterwards, in jobs
that can only read."""
import json
import os
import re
import sys
from pathlib import Path

ID = re.compile(r"[a-z][a-z0-9-]{1,39}")  # the recipe schema's id ($defs/id)
SECOND_SERVER = re.compile(r".+-[0-9a-f]{8}")  # ids Fenrir keeps for a second server of a recipe
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
RECIPE_MAX = 256 * 1024  # Fenrir's own limit for one recipe file
NOTES_MAX = 60 * 1024  # README.md stays under the 64 KB the signing step allows
ENTRY_FILES = {"recipe.json", "README.md", "icon.png", "icon.webp"}
LIVE = {"yes": "yes", "no": "no", "disk": "disk"}
CONFIRMATIONS = (
    "This game has no anti-cheat, or its server runs with it off",
    "Nothing here needs an account to download",
    "I may share these facts under CC0",
)
# The form's headings, in the form's order (submit-recipe.yml): id, recipe, notes, live, harness, the confirmations.
FIELDS = (("id", "Recipe id"), ("recipe", "Recipe"), ("notes", "Research notes"), ("live", "Live run"),
          ("harness", "Harness on your PC"), ("confirm", "Confirmations"))
NO_RESPONSE = "_No response_"
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f‎‏‪-‮⁦-⁩]")


# ---------------------------------------------------------------------------------------------------- answers
def output(**answer):
    """The answer for the next jobs: all of it as one line of JSON (answer), and beside it ok (true or false) and the
    recipe's id once it has been checked, which the workflow uses as they are."""
    path = os.environ.get("GITHUB_OUTPUT")
    lines = [f"answer={json.dumps(answer, ensure_ascii=True, separators=(',', ':'))}", f"ok={'true' if answer.get('ok') else 'false'}"]
    if answer.get("ok") and ID.fullmatch(str(answer.get("id") or "")):
        lines.append(f"id={answer['id']}")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def plain(text, limit):
    """One line of text from the submission, safe to put in a title or a commit message."""
    text = CONTROL.sub("", str(text or "")).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


# ---------------------------------------------------------------------------------------------------- the issue form
def fields(body):
    """{field id: its text} from an issue form's body. Each heading is looked for after the one before it, in the
    form's order, so text inside a field cannot move another field."""
    lines = str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    marks, at = [], 0
    for fid, label in FIELDS:
        found = next((i for i in range(at, len(lines)) if re.fullmatch(r"###\s+" + re.escape(label) + r"\s*", lines[i])), None)
        if found is None:
            marks.append((fid, None))
            continue
        marks.append((fid, found))
        at = found + 1
    out = {}
    places = [m for m in marks if m[1] is not None]
    for n, (fid, start) in enumerate(places):
        end = places[n + 1][1] if n + 1 < len(places) else len(lines)
        text = "\n".join(lines[start + 1:end]).strip("\n")
        out[fid] = "" if text.strip() == NO_RESPONSE else text
    for fid, start in marks:
        out.setdefault(fid, None)
    return out


def unfence(text):
    """The text inside the code fence GitHub puts around a field shown as code (render: json)."""
    lines = (text or "").strip("\n").split("\n")
    if lines and re.fullmatch(r"\s*(`{3,}|~{3,})\s*[A-Za-z0-9_-]*\s*", lines[0]):
        fence = lines[0].strip()[0] * 3
        lines = lines[1:]
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().startswith(fence) and lines[i].strip().strip(fence[0]) == "":
                lines = lines[:i]
                break
    return "\n".join(lines).strip()


class Twice(ValueError):
    pass


def _pairs(items):
    d = {}
    for k, v in items:
        if k in d:
            raise Twice(k)
        d[k] = v
    return d


def _constant(name):
    raise ValueError(f"{name} is not a number JSON allows")


def read_recipe(text):
    """(document, problem): the recipe read as strict JSON, one object, no key twice."""
    raw = (text or "").encode("utf-8")
    if not raw.strip():
        return None, "The Recipe box is empty. Paste the whole recipe.json there."
    if len(raw) > RECIPE_MAX:
        return None, f"The recipe is larger than {RECIPE_MAX // 1024} KB."
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except Twice as e:
        return None, f"The recipe says {e.args[0]!r} twice in one place. Each key may appear once."
    except RecursionError:
        return None, "The recipe is nested too deep."
    except ValueError as e:
        return None, f"The Recipe box does not hold JSON ({str(e)[:200]})."
    if not isinstance(doc, dict):
        return None, "The Recipe box holds JSON, but not one recipe (a JSON object)."
    return doc, None


def recipe_text(doc):
    """The recipe the one way Fenrir writes it for the library (core/recipes/share.py text_of): one key a line."""
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def take_issue(event_path, out):
    event = json.loads(Path(event_path).read_text("utf-8"))
    issue = event.get("issue") or {}
    number, login = int(issue.get("number") or 0), plain((issue.get("user") or {}).get("login"), 39)
    f = fields(issue.get("body"))
    problems = []
    for fid, label in FIELDS:
        if f.get(fid) is None:
            problems.append(f"The form's {label} section is missing. Open a new submission with the form, or put the section back.")
    rid = plain(f.get("id"), 80)
    doc, said = read_recipe(unfence(f.get("recipe")))
    if said:
        problems.append(said)
    if doc is not None:
        inner = doc.get("id")
        if not rid and isinstance(inner, str):
            rid = inner
        elif isinstance(inner, str) and inner != rid:
            problems.append(f"The Recipe id box says {rid!r}, and the recipe itself says {inner!r}. They must be the same.")
    if not ID.fullmatch(rid or ""):
        problems.append("The recipe id must be 2 to 40 characters: a lower-case letter first, then lower-case letters, digits and dashes.")
    elif SECOND_SERVER.fullmatch(rid) or rid.startswith("custom-"):
        problems.append(f"{rid} looks like the id Fenrir gives a second server or your own server. Choose a plain id for the game.")
    live = LIVE.get((f.get("live") or "").strip().lower())
    if f.get("live") is not None and live is None:
        problems.append("Live run must be yes, no or disk.")
    ticked = [bool(re.search(r"^\s*-\s*\[[xX]\]\s*" + re.escape(c) + r"\s*$", f.get("confirm") or "", re.M)) for c in CONFIRMATIONS]
    for c, ok in zip(CONFIRMATIONS, ticked):
        if not ok:
            problems.append(f"The box \"{c}\" is not ticked.")
    notes = (f.get("notes") or "").strip()
    if not notes:
        problems.append("The Research notes box is empty. Say where the facts come from: the game's own guide, the sources you read.")
    if len(notes.encode("utf-8")) > NOTES_MAX:
        problems.append(f"The research notes are longer than {NOTES_MAX // 1024} KB.")
    harness = plain(f.get("harness"), 400) or "not given"
    name = plain((doc or {}).get("name"), 60) or rid
    version = (doc or {}).get("version") if VERSION.fullmatch(str((doc or {}).get("version") or "")) else ""
    if problems:
        print("The submission could not be taken:\n  " + "\n  ".join(problems))
        output(ok=False, problems=problems, issue=number)
        return 1
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "recipe.json").write_bytes(recipe_text(doc).encode("utf-8"))
    readme = (f"# {name}\n\n{notes}\n\n## Submitted\n\n"
              f"- From issue #{number}" + (f" by @{login}" if login else "") + "\n"
              f"- Live run on a real server: {live}\n"
              f"- The harness on the submitter's PC: {harness}\n")
    (out / "README.md").write_bytes(readme.encode("utf-8"))
    (out / "pr-body.md").write_bytes((
        f"This pull request was made from issue #{number}" + (f", sent by @{login}" if login else "") + ".\n\n"
        f"It adds or changes `recipes/{rid}/` only. The library's checks ran on it in the issue's workflow run, and "
        "their report is the comment below. Merging publishes nothing by itself: the library is signed on Fenrir's "
        "maker's PC, where every check runs again.\n\n"
        f"Closes #{number}\n").encode("utf-8"))
    print(f"Taken: {rid} {version or '(no version)'} from issue #{number}")
    output(ok=True, id=rid, version=version, name=name, issue=number, title=f"Recipe: {name} (issue #{number})",
           message=f"Recipe {rid} {version} from issue #{number}".replace("  ", " "))
    return 0


# ---------------------------------------------------------------------------------------------------- a pull request
def take_pr(files_path, base):
    """The files a pull request changes: one recipe's folder, and nothing else."""
    try:
        text = Path(files_path).read_text("utf-8")
        try:
            listed = json.loads(text)
        except ValueError:  # gh api --paginate --jq '.[]': one file a line
            listed = [json.loads(x) for x in text.splitlines() if x.strip()]
        if not isinstance(listed, list) or not all(isinstance(x, (dict, list)) for x in listed):
            raise ValueError("not a list of files")
    except (OSError, ValueError) as e:
        print(f"The pull request's files could not be read: {e}")
        output(ok=False, problems=["The pull request's files could not be read."])
        return 1
    if listed and isinstance(listed[0], list):  # gh api --paginate --slurp: one list per page
        listed = [x for page in listed for x in page]
    problems, ids = [], set()
    for x in listed:
        name, status = str(x.get("filename") or ""), str(x.get("status") or "")
        before = str(x.get("previous_filename") or "")
        parts = name.split("/")
        if len(parts) != 3 or parts[0] != "recipes" or parts[2] not in ENTRY_FILES:
            problems.append(f"{name}: a submission changes only recipes/<id>/recipe.json, README.md and its icon.")
            continue
        if parts[1].startswith(".") or not ID.fullmatch(parts[1]):
            problems.append(f"{name}: recipes/{parts[1]}/ is not a recipe's folder (its name is the recipe's id).")
            continue
        if status in ("removed", "renamed") or before:
            problems.append(f"{name}: a submission adds or changes files; it does not remove or move them. Withdrawals go through revoked.json, by Fenrir's maker.")
            continue
        ids.add(parts[1])
    if len(ids) > 1:
        problems.append(f"It changes {len(ids)} recipes ({', '.join(sorted(ids))}). Send one recipe per pull request.")
    if not ids and not problems:
        problems.append("It changes no recipe. Only Fenrir's maker changes files outside recipes/.")
    rid = next(iter(ids)) if len(ids) == 1 else ""
    if rid and not problems:
        has = {str(x.get("filename")).split("/")[2] for x in listed}
        exists = (Path(base) / "recipes" / rid / "recipe.json").is_file()
        if "recipe.json" not in has and not exists:
            problems.append(f"recipes/{rid}/ has no recipe.json.")
    if problems:
        print("The pull request is not a submission the library takes:\n  " + "\n  ".join(problems))
        output(ok=False, problems=problems)
        return 1
    print(f"Taken: recipes/{rid}/ from the pull request")
    output(ok=True, id=rid)
    return 0


def _arg(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv[:-1] else default


def main(argv):
    if argv[:1] == ["issue"] and _arg(argv, "--event") and _arg(argv, "--out"):
        return take_issue(_arg(argv, "--event"), _arg(argv, "--out"))
    if argv[:1] == ["pr"] and _arg(argv, "--files"):
        return take_pr(_arg(argv, "--files"), _arg(argv, "--base", "."))
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
