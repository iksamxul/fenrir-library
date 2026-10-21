# Valheim (example entry)

This folder is a worked example of a library entry, not an entry. It holds Fenrir's own built-in Valheim recipe under
the id `example-valheim`, laid out exactly as `recipes/<id>/` is laid out.

It is kept outside `recipes/`, so it is never signed into the library's index. Fenrir already ships Valheim, and the
ids of Fenrir's own recipes are reserved, so a copy in the library would only offer the same server twice.

The library's checks run on it to test themselves:

```
python -I tools/check.py all examples/example-valheim
```

The rest of this file shows what an entry's README.md holds: its research notes.

## Sources

- Fenrir's built-in Valheim recipe, `fenrir/recipes/builtin/valheim.json`. Its facts came unchanged from Fenrir 2.x's
  Valheim server. The ready checks are new in 3.0.
- The server is Steam app 896660, installed by SteamCMD without signing in. The game friends play is Steam app 892970.

An entry for a new game names where each fact comes from: the game's own server guide, its developers' notes, a
community wiki page. Links go in the recipe's `research.sources` as well.

## The live run

Not run for this example. An entry says here whether its recipe ran on a real server, on which platform and when, and
what happened. The recipe's `tests.live` records the result.

## Quirks

- The password must be at least 5 characters and must not be part of the server's name. Valheim refuses either.
- With the join code (crossplay), friends join with a six-digit code through Valheim's own relay. No ports need opening.
- The Linux build is not yet checked on a real server.
