#!/usr/bin/env python3
"""Step 7 of the library's checks: the report (docs/v3-design/library.md section 3.3). One comment on the pull request
with each step's result and "What is inside", kept up to date on each push, and on an issue a short note pointing to it.

    python -I tools/report.py --out <folder>

It reads the other jobs' answers from the environment, as the workflow passes them:
    TAKE                  the take step's answer (tools/take.py), one line of JSON
    SCHEMA POLICY HARNESS_WINDOWS HARNESS_LINUX DOWNLOADS INSIDE
                          each step's answer (tools/check.py), one line of JSON; empty when the step did not run
    RESULT_<STEP>         each job's result as GitHub says it: success, failure, cancelled or skipped
    SOURCE                issue or pull_request; ISSUE and PR their numbers; HEAD the commit checked; RUN the run's address

and writes into the folder: pr.md (the pull request's comment), issue.md (the issue's), and state (success or failure,
for the commit's status). Everything taken from a submission is shown inside code blocks, so none of it is read as
Markdown, a link or a mention. Standard library only; nothing of Fenrir is imported."""
import json
import os
import sys
from pathlib import Path

MARK = "<!-- fenrir-library-report -->"
STEPS = (("schema", "SCHEMA", "2. Schema"), ("policy", "POLICY", "3. Policy"),
         ("harness-windows", "HARNESS_WINDOWS", "4. Harness on Windows"), ("harness-linux", "HARNESS_LINUX", "4. Harness on Linux"),
         ("downloads", "DOWNLOADS", "5. Downloads"), ("inside", "INSIDE", "6. What is inside"))


def answer(name):
    try:
        v = json.loads(os.environ.get(name) or "null")
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def fence(lines):
    text = "\n".join(str(x) for x in lines)
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}text\n{text}\n{ticks}"


def build():
    take, source = answer("TAKE") or {}, os.environ.get("SOURCE") or ""
    head, run = (os.environ.get("HEAD") or "")[:12], os.environ.get("RUN") or ""
    if source == "issues" and take.get("ok") and not (os.environ.get("PR") and head):
        take = {**take, "ok": False, "problems": ["The form was read, but it could not be put on a pull request: the recipe may be in the "
                                                  "library already exactly as submitted. The workflow's run says why" + (f": {run}" if run else ".")]}
    policy = answer("POLICY") or {}
    plats = policy.get("platforms") or []
    linux_only = "linux" in plats and "windows" not in plats
    rows, fails, warns, required_ok = [], [], [], bool(take.get("ok"))
    rows.append(("1. Take the submission", "passed" if take.get("ok") else "failed"))
    if not take.get("ok"):
        fails += [f"Take the submission: {p}" for p in take.get("problems") or ["it could not be read"]]
    for job, env, label in STEPS:
        got, result = answer(env), os.environ.get(f"RESULT_{env}") or "skipped"
        shown_only = job == "harness-linux" and not linux_only
        if got is not None:
            said = "passed" if got.get("ok") else "failed"
        elif result == "skipped":
            said = "not run" if job.startswith("harness") and plats and job.split("-")[1] not in plats else "not run: an earlier step failed"
        else:
            said = {"failure": "failed", "cancelled": "cancelled"}.get(result, result)
        if job == "inside":
            said = "shown below" if got is not None else said
        if shown_only and said in ("passed", "failed"):
            said += " (shown only: Fenrir's Linux harness is new, so this does not decide)"
        rows.append((label, said))
        if got is not None:
            fails += [f"{label[3:]}: {x}" for x in got.get("fails") or []]
            warns += [f"{label[3:]}: {x}" for x in got.get("warns") or []]
        needed = job not in ("inside",) and not shown_only and not (job.startswith("harness") and plats and job.split("-")[1] not in plats)
        if needed and take.get("ok") and not (got is not None and got.get("ok")):
            required_ok = False
    state = "success" if required_ok else "failure"
    rid = take.get("id") or policy.get("id") or "?"
    where = f"issue #{take.get('issue') or os.environ.get('ISSUE')}" if source == "issues" else "this pull request"
    body = [MARK, f"## The library's checks: {'passed' if required_ok else 'not passed'}", "",
            f"Recipe `{rid}`" + (f" {policy.get('version')}" if policy.get("version") else "") + f", from {where}"
            + (f", at commit {head}" if head else "") + (f". [The run]({run})" if run else "") + ".", "",
            "| Step | Result |", "|---|---|", *(f"| {a} | {b} |" for a, b in rows), ""]
    if fails:
        body += ["### What failed", "", fence(fails), ""]
    if warns:
        body += ["### Look at these", "", "They do not fail the checks, but the reviewer reads them.", "", fence(warns), ""]
    inside = answer("INSIDE")
    if inside and inside.get("notes"):
        body += ["### What is inside", "", "What Fenrir's trust prompt shows before anyone uses this recipe:", "", fence(inside["notes"]), ""]
    for env, label in (("HARNESS_WINDOWS", "The harness on Windows"), ("HARNESS_LINUX", "The harness on Linux")):
        h = answer(env)
        if h and h.get("notes"):
            body += [f"<details><summary>{label}</summary>", "", fence(h["notes"]), "", "</details>", ""]
    body += ["Passing these checks publishes nothing. Fenrir's maker reviews the pull request, merges it, and signs "
             "the library on their own PC, where every check runs again. Nothing from the library runs on anyone's PC until "
             "they answer Fenrir's \"What is inside\" prompt, where No is the default."]
    pr = "\n".join(body) + "\n"
    if not take.get("ok"):
        issue = (f"{MARK}\nThe submission could not be taken:\n\n{fence(take.get('problems') or ['it could not be read'])}\n\n"
                 "Edit this issue's description to fix it, and the checks run again.\n")
    else:
        issue = (f"{MARK}\nThank you. This submission is pull request #{take.get('pr') or os.environ.get('PR')}, where the library's "
                 f"checks report on it: {'they passed' if required_ok else 'they have not passed yet'}. To change the submission, edit this "
                 "issue's description, and the pull request and its checks follow.\n")
    return pr, issue, state


def main(argv):
    if "--out" not in argv[:-1]:
        sys.stderr.write(__doc__)
        return 2
    out = Path(argv[argv.index("--out") + 1])
    out.mkdir(parents=True, exist_ok=True)
    pr, issue, state = build()
    (out / "pr.md").write_text(pr, "utf-8")
    (out / "issue.md").write_text(issue, "utf-8")
    (out / "state").write_text(state, "utf-8")
    print(pr)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(pr.replace(MARK, "") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
