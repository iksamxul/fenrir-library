# Recipes

Each entry is one folder, named by its recipe's id:

```
recipes/<id>/recipe.json    the recipe: one JSON document (schema/recipe-1.json)
recipes/<id>/README.md      its research notes: where the facts come from, the live run, quirks
recipes/<id>/icon.png       optional: a PNG or WebP picture of 64 KB at most (icon.webp, or the name the recipe's "icon" gives)
```

Nothing else goes in the folder. [examples/example-valheim/](../examples/example-valheim/) is a complete entry to copy
the shape of. It is Fenrir's own Valheim recipe under another id, kept outside `recipes/` so it is never signed.

## recipe.json

The schema is [schema/recipe-1.json](../schema/recipe-1.json). The checks also hold every entry to these rules:

- **`id`**: 2 to 40 characters, a lower-case letter first, then lower-case letters, digits and dashes. It is the
  folder's name. Ids are shared with Fenrir's own recipes, whose ids are reserved ([tools/reserved-ids.json](../tools/reserved-ids.json)),
  and an id ending in a dash and eight hex digits is refused (Fenrir gives those to a second server of a recipe).
- **`version`**: three numbers, such as `1.0.0`. It goes up with every accepted change, and a withdrawn version is
  never accepted again.
- **`license`**: `CC0-1.0`, the licence the submission form's last box agrees to and the rest of the library is under.
  Fenrir's schema also knows `MIT` and `Apache-2.0`; the checks let those through, and the reviewer decides.
- **`authors`**: your name, and your GitHub account if you like.
- **`antiCheat`**: `none`, or `off` with the anti-cheat named and every switch Fenrir keeps off. A game whose anti-cheat
  cannot be switched off gets no recipe ([tools/blocked.json](../tools/blocked.json)).
- **`get`**: SteamCMD signed in anonymously, or https downloads, each with its `sha256`. Nothing that needs an account.
- **No code.** No `hooks`, no download resolvers: only Fenrir's own recipes may use those.
- **`tests.harness`**: what the fake server plays, so the harness can run the recipe's whole life.
- **`research`**: its notes and sources, as README.md has them, and `tests.live` once it ran on a real server.

Fenrir writes a recipe one key a line (`json.dumps(recipe, indent=1)`), and a submission made with the form is written
that way. Recipes are hashed byte for byte, so keep the file as it is once it has been reviewed.

## README.md

The research notes, in plain words:

- **Sources**: where each fact comes from. The game's own server guide, its developers' notes, a community wiki page.
- **The live run**: whether it ran on a real server, on which platform and when, and what happened.
- **Quirks**: what surprised you. Settings the game refuses, a port it also needs, a world it has to be given.

## Sending one

From Fenrir, *Share to the library* on your recipe fills in the form for you. With git, open a pull request that adds
or changes one folder here. Either way the checks run on it, and Fenrir's maker reviews it.
