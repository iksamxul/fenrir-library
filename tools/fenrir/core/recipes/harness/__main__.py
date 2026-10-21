"""python -m core.recipes.harness <recipe.json> [<recipe.json> ...] [--json] [--source builtin|library|local|imported]

Runs each recipe's whole life under the harness (run.py) and prints its report: the short text, or the JSON with --json.
The exit code is 0 when every recipe passed, 1 when one failed, and 3 when none failed but one could not be run far
enough to vouch for it (a part of Fenrir it needs is not here yet)."""
import json
import sys
from concurrent.futures import ThreadPoolExecutor

from . import run


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    source = argv[argv.index("--source") + 1] if "--source" in argv and argv.index("--source") + 1 < len(argv) else "builtin"
    files = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--source")]
    if not files:
        sys.stderr.write(__doc__)
        return 2
    with ThreadPoolExecutor(max_workers=min(4, len(files))) as pool:  # recipes run side by side: each has ports of its own
        reports = list(pool.map(lambda f: run.run(f, origin=source), files))
    if as_json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=1))
    else:
        print("\n\n".join(run.text(r) for r in reports))
    states = {r["status"] for r in reports}
    return 1 if "failed" in states else 3 if "incomplete" in states else 0


if __name__ == "__main__":
    sys.exit(main())
