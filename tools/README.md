# The checks' tools

What the *Library checks* workflow runs on every submission, and what you can run on your own PC.

```
tools/take.py            step 1: reads a submission (the issue form as text, or a pull request's list of files)
tools/check.py           steps 2 to 6: schema, policy, harness, downloads, What is inside
tools/report.py          step 7: the report comment
tools/requirements.txt   jsonschema and the packages it needs, at exact versions (the schema step only)
tools/blocked.json       the anti-cheat lists: games Fenrir hosts no server for, and games it plays with anti-cheat off
tools/reserved-ids.json  the ids of Fenrir's own recipes, which the library never takes
tools/MANIFEST.json      which Fenrir the copy below came from, and every file's SHA-256
tools/fenrir/            the pinned copy of Fenrir's own checks
```

`take.py` and `report.py` use Python's standard library only and import nothing of Fenrir. `take.py` runs in the only
job that can write to the repository, so it reads the form and writes files, and runs nothing else.

## tools/fenrir

A copy of the parts of Fenrir that check a recipe, byte for byte, made at a Fenrir release by Fenrir's
`fenrir/build/library_tools.py`. `MANIFEST.json` names the Fenrir version and commit, and the SHA-256 of every file.

- **The validator and the policy**: `core/recipes/schema.py`, `policy.py`, `values.py`, `load.py`.
- **The share check and the trust prompt**: `core/recipes/share.py`, `trust.py`, `core/scrub.py`,
  `core/games/sharecheck.py`.
- **The harness**: `core/recipes/harness/`, its fake server, fake SteamCMD, fake downloads and fake Docker.
- **What the harness runs for real.** The harness runs Fenrir's own driver, so the start, the stop, the Ctrl+C helper,
  the backups and the updates are Fenrir's real code; only the game's program is fake. So the copy also carries the
  driver (`core/recipes/drivers/`, `writers.py`, `probes.py`, `players.py`), the 2.x pieces it stands on
  (`core/games/servers.py`, `steamcmd.py`, `consolectl.py` and others), and the engine's base: paths, messages, the
  OS layer, and the sealed store that keeps a server's secret settings.
- **The English messages** the checks say (`i18n/en/msg*.json`, `time.json`), and the recipe schema.

Two files are not Fenrir's own, and say so at their top:

- `gamekit/catalog.py` holds only what the policy reads of the game kit's catalog: the anti-cheat lists, from
  `blocked.json`. Fenrir's catalog also holds the game kit's mod recipes, which no check needs.
- `app.py` holds only Fenrir's `--ctrl-c` and `--console-type` helpers, which the driver starts to send Ctrl+C to a
  server's console so it saves.

Nothing else of Fenrir is here: no window, no website, no settings, no keys. The checks keep Fenrir's data in a new
temporary folder, never a real Fenrir's.

## Refreshing the copy

At each Fenrir release, in Fenrir's repository:

```
python fenrir/build/library_tools.py --library <this repository's folder> --check
```

It rewrites `tools/fenrir/`, `blocked.json`, `reserved-ids.json`, `MANIFEST.json` and `schema/recipe-1.json`, and
nothing else. `--check` then runs the copy here, on `examples/example-valheim` and on Fenrir's own recipes read as
library recipes, and fails if the copy needs anything it does not carry. Commit the result to `main`.

## Running a check

```
python -m pip install -r tools/requirements.txt
python -I tools/check.py all recipes/<id>          schema, policy, harness, What is inside
python -I tools/check.py downloads recipes/<id>    each download fetched and hashed
```

`-I` keeps Python from reading anything from the folder it runs in or from your environment. The harness runs on your
PC's own platform, Windows or Linux, with Python 3.14.
