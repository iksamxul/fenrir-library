# Fenrir's community library

Recipes for game and app servers, shared by people who use Fenrir, checked, reviewed by Fenrir's maker, and signed.

A recipe is one JSON file that tells Fenrir how to get a dedicated server, set it up, run it, stop it and back it up.
Recipes are data: they hold no code. Fenrir ships recipes of its own; this library holds the ones people add.

## How a recipe gets here

1. **Share it from Fenrir.** On a recipe of your own, *Share to the library* checks it on your PC first. Then it opens
   this repository's *Submit a recipe* form with everything filled in. You send it from your own GitHub account:
   Fenrir holds no GitHub token and sends nothing by itself.

   If you use git, you can open a pull request instead. It adds or changes one folder, `recipes/<id>/`.
   [recipes/README.md](recipes/README.md) says what goes in it.
2. **The checks run** on every submission, issue or pull request alike (the *Library checks* workflow). A submission
   made with the form becomes a pull request on the branch `submission/<issue number>`, and the report is posted on it.
3. **Fenrir's maker reviews** the pull request: the report, "What is inside", the research notes and, where the disk
   allows, one run on a real server. Merging publishes nothing by itself.
4. **The library is signed** on Fenrir's maker's PC, with the library's own key, after every check has run again
   there. The signed index is published with Fenrir's website. Fenrir checks the signature, and then each recipe's
   SHA-256, before it uses anything from it.

## What gets checked

| Step | What | It fails when |
|---|---|---|
| 1. Take the submission | The form is read as text, never run, and written into `recipes/<id>/` on a branch. A pull request must change one recipe's folder and nothing else. | the form cannot be read; more than one recipe; a file outside `recipes/<id>/` |
| 2. Schema | `schema/recipe-1.json`, checked by jsonschema and by Fenrir's own validator. The two must agree. | either refuses |
| 3. Policy | Fenrir's rules for a library recipe: no anti-cheat, or one kept off; no game on the anti-cheat lists (`tools/blocked.json`); no hooks or resolvers (recipes hold no code); downloads over https, each pinned by its SHA-256; size limits; paths inside the server's own folders; patterns that cannot hang Fenrir, each timed on lines made to be slow; no secrets, addresses or paths of a PC; a licence; an id that is free and not withdrawn. | any rule |
| 4. Harness | The recipe's whole life on a fake server: install, configure, start, players, console, stop, backup, update, crash and forced stop. Only fakes run, and nothing is reached beyond 127.0.0.1. It runs on Windows, and on Linux for a recipe that lists Linux. | any step of the life |
| 5. Downloads | Each download is fetched and its SHA-256 compared with the recipe's, at most 2 GB and 10 minutes a file. Nothing is written to disk, run or unpacked. SteamCMD's app ids are not downloaded. | a different SHA-256; a host that does not answer |
| 6. What is inside | The plain-English summary Fenrir's trust prompt shows, posted with the report for the reviewer. | never |
| 7. Report | One comment on the pull request with each step's result, kept up to date. | |

The workflow itself runs nothing from a submission with write access to this repository. Its file,
[.github/workflows/check.yml](.github/workflows/check.yml), says how at the top.

Passing these checks is not enough to be published. Fenrir's maker reviews every entry, and signing runs every check
again on a PC this repository cannot reach.

## Nothing runs without your yes

Before a recipe from the library is used, Fenrir shows "What is inside": what it downloads and from where, the
program it runs and its arguments, the ports friends need, the files it writes, where it keeps its world, its
settings, its anti-cheat facts and whether it needs an account. No is the default. A recipe that changes asks again,
saying what changed.

## Withdrawals

`revoked.json` lists the versions that were withdrawn. Each entry has the recipe's `id` and `version` (or the file's
`sha256`) and always a `reason`:

```json
[{"id": "some-game", "version": "1.0.0", "reason": "Its download moved to a site that bundles other programs."}]
```

- A withdrawal is carried in every index signed from then on. It is never forgotten, and a withdrawn version is never
  signed again.
- Fenrir shows "This recipe was withdrawn from the library", with the reason, on every server made from it, and stops
  offering it for new servers.
- The server keeps running. Nothing is removed or stopped by itself.

Only Fenrir's maker withdraws a recipe. To ask for a withdrawal, open an issue and say why.

## Run the checks yourself

With Python 3.14 (the version the workflow uses):

```
python -m pip install -r tools/requirements.txt
python -I tools/check.py all recipes/<id>
python -I tools/check.py downloads recipes/<id>
```

`tools/` is a pinned copy of Fenrir's own checks; [tools/README.md](tools/README.md) says what is in it and where it
comes from.

## What is where

```
recipes/<id>/recipe.json      one recipe
recipes/<id>/README.md        its research notes: sources, the live run, quirks
recipes/<id>/icon.png         optional, 64 KB at most
examples/example-valheim/     a worked example of an entry; never signed
schema/recipe-1.json          the recipe schema, copied from Fenrir at each release
tools/                        the checks the workflow runs, and Fenrir's pinned copy of its own
revoked.json                  withdrawn versions
.github/                      the submission form and the checks' workflow
```

Ready-made mod setups (`setups/<id>/`) come later, through the same checks.

## Privacy

The library collects nothing. A submission is a public issue or pull request, made from your own GitHub account by
your own choice. Fenrir counts nothing and sends nothing of its own.

## Licence

The recipes, their notes and the withdrawals (`recipes/`, `examples/`, `revoked.json`) are dedicated to the public
domain under CC0-1.0 ([LICENSE](LICENSE)), so Fenrir's website and other tools may reuse the facts. Submitting a
recipe means you may share its facts that way.

`tools/` and `schema/` are Fenrir's own files. Their licence is in [tools/LICENSE](tools/LICENSE).
