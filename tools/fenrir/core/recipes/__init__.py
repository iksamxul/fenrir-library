"""Recipes (3.0, docs/v3-design/recipes.md): every server kind as data, one JSON document each, checked against the schema
in fenrir/recipes/recipe.schema.json and against Fenrir's own rules before anything runs.

    schema.py   the JSON Schema checker: the subset of keywords the recipe schema uses, in the standard library only
    policy.py   the rules a schema cannot say: anti-cheat, where recipes come from, placeholders by place, secrets, paths,
                programs, downloads, Docker, console lines and the regular expression guard
    values.py   placeholders filled, settings read and checked, conditional arguments, ports, and the path guard at run time
    load.py     recipe files read strictly, hashed and checked; the built-in recipes; one view per platform

Nothing here starts a process, opens a port or writes a server's files: the drivers (core/recipes/drivers) do, through
these checks."""


class Problem(tuple):
    """One thing wrong with a recipe: where (a JSON pointer such as /run/args/3), what to say, and a short code the tests
    and the trust prompt can tell apart (anticheat, secret, path...)."""
    __slots__ = ()

    def __new__(cls, where, say, code="schema"):
        return super().__new__(cls, (str(where or "/"), str(say), str(code)))

    where = property(lambda self: self[0])
    say = property(lambda self: self[1])
    code = property(lambda self: self[2])

    def __str__(self):
        return f"{self.where}: {self.say}" if self.where not in ("", "/") else self.say


class RecipeError(ValueError):
    """A recipe Fenrir will not use, with every problem found. str() is the first few, said plainly."""

    def __init__(self, problems, intro="This recipe cannot be used"):
        self.problems = [p if isinstance(p, Problem) else Problem("/", str(p)) for p in (problems if isinstance(problems, (list, tuple)) else [problems])]
        shown = "; ".join(str(p) for p in self.problems[:3])
        more = f" (and {len(self.problems) - 3} more)" if len(self.problems) > 3 else ""
        super().__init__(f"{intro}: {shown}{more}." if shown else f"{intro}.")

    def codes(self):
        return {p.code for p in self.problems}
