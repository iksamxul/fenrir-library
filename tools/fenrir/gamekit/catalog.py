"""NOT Fenrir's gamekit/catalog.py: a stand-in made by fenrir/build/library_tools.py, holding only what
core/recipes/policy.py reads of the game kit's catalog, from tools/blocked.json. Fenrir's own catalog also holds
the game kit's mod recipes, which no check of the library needs."""
import json
import re
import unicodedata
from pathlib import Path

_LISTS = json.loads((Path(__file__).resolve().parents[2] / "blocked.json").read_text("utf-8"))
BLOCKED_STEAM = dict(_LISTS["steam"])
BLOCKED_EPIC = dict(_LISTS["epic"])
BLOCKED_NAMES = re.compile(_LISTS["names"])
RECIPES = {g["key"]: {"name": g["name"], "steam": list(g["steam"]), "policy": "noeac"} for g in _LISTS["noeac"]}


def squeeze(name):
    """A game's name as lower-case letters and digits only, marks like ™ and ® gone."""
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower())
