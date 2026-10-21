"""Who is acting (3.0, docs/v3-design/roles.md §3.4): the host, a friend in their role, a phone, an AI app, a schedule, the
Discord bot, the command line, another node, or Fenrir itself.

Whatever runs does so as one actor, held in a context variable. The router sets it for each request, each of the engine's
own loops sets its own once (the keeper, the timed loop, a game night, an update), and a task takes the actor of whoever
started it to its thread (core/util.py TASKS). The timeline (core/activity.py) writes it on every line, so who did
something is a fact of the line, never a guess from its wording ("Phone: ..."). The server being worked on travels the
same way (about()), so lines and tasks also say which server they are about.

An actor never comes from a request's body or headers, only from its credential (roles.md §3.3), and the kinds only the
engine runs as (schedule, system, node) are made by engine code alone. An id is never a secret: a token's id, a friend's
fid, a Discord user's id or a schedule's name, never a key, since the timeline reaches the window, the phone, the support
bundle and, in 3.0, friends who may read their own lines."""
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass

KINDS = ("host", "friend", "phone", "ai", "schedule", "bot", "cli", "node", "system")
ROLES = ("player", "helper", "moderator", "cohost")  # named bundles of capabilities; what each allows is core/authz.py's


@dataclass(frozen=True)
class Actor:
    kind: str  # one of KINDS
    id: str  # a token's id, a friend's fid, a Discord user's id, a schedule's name: never a key
    name: str  # as people read it: "Sam", "Sam's phone", "Claude Code", "Friday's game night", "Discord: kim"
    role: str | None = None  # one of ROLES, for a friend and the devices made from their key
    via: str | None = None  # the way in: local, tunnel, tailnet, lan, discord or mcp

    def __post_init__(self):
        # a misspelt kind or role would make an actor no capability check knows: refused where it is made, not found later
        if self.kind not in KINDS:
            raise ValueError(f"No such kind of actor: {self.kind!r}")
        if self.role is not None and self.role not in ROLES:
            raise ValueError(f"No such role: {self.role!r}")
        if not (isinstance(self.id, str) and self.id and isinstance(self.name, str) and self.name):
            raise ValueError("An actor needs an id and a name")

    def public(self):
        """What a timeline line, a task and an event say about it. Not `via`: how someone reached Fenrir is the router's
        business, not something to keep for weeks or show to friends."""
        return {"kind": self.kind, "id": self.id[:64], "name": self.name[:80], "role": self.role}

    @classmethod
    def from_public(cls, d):
        """The actor a record wrote with public(), or None for a line from before 3.0 or anything that is not an actor."""
        if not isinstance(d, dict):
            return None
        try:
            return cls(str(d.get("kind") or ""), str(d.get("id") or ""), str(d.get("name") or ""), d.get("role") or None)
        except (TypeError, ValueError):
            return None


SYSTEM = Actor("system", "fenrir", "Fenrir")  # what nobody in particular asked for: a plain thread, a process that ended
HOST = Actor("host", "host", "Host")  # the host, from the window, the tray or this PC's command line
SCHEDULE = Actor("schedule", "scheduler", "Fenrir's schedule")  # the timed loop (core/scheduler.py): daily restarts, timed backups
KEEPER = Actor("schedule", "keeper", "Fenrir's care")  # game servers looking after themselves (core/games/keeper.py)
UPDATE = Actor("system", "update", "Fenrir's update")  # Fenrir updating itself and the friends' apps it hands out


def night(sid, title=None):
    """A game night as an actor: what it starts and stops is its doing (core/sessions.py). Its id says it is a night, so a
    filter by actor never mixes one up with another schedule."""
    return Actor("schedule", f"night:{sid}", str(title or "").strip()[:80] or "Game night")


CURRENT = contextvars.ContextVar("fenrir_actor", default=SYSTEM)
SERVER = contextvars.ContextVar("fenrir_server", default=None)


def current_actor():
    """Who is acting here and now."""
    return CURRENT.get()


def current_server():
    """The id of the server being worked on here and now, or None."""
    return SERVER.get()


@contextmanager
def acting(actor):
    """Everything inside runs as `actor`: the timeline lines it writes, the tasks it starts and what those do in turn. It
    nests, and the outer actor is back afterwards, also after an error. As a decorator, every call runs as `actor`."""
    if not isinstance(actor, Actor):
        raise TypeError("acting() takes an Actor")
    token = CURRENT.set(actor)
    try:
        yield actor
    finally:
        CURRENT.reset(token)


def _server_id(server):
    sid = str(server).strip()[:64] if server else ""
    return sid or None


@contextmanager
def about(server):
    """Everything inside is about one server, by its id in the server list ("valheim", "custom-1a2b3c4d"): its timeline
    lines and its tasks say so. None: about no server in particular, also inside another server's work."""
    token = SERVER.set(_server_id(server))
    try:
        yield
    finally:
        SERVER.reset(token)


def carry(fn):
    """fn, ready to run on another thread as whoever is acting now, about the same server. A plain thread starts as
    Fenrir, so work handed to one would lose who asked for it. Each call runs in a copy of its own, so one wrapper may run
    on several threads at once."""
    ctx = contextvars.copy_context()

    def run(*args, **kwargs):
        return ctx.copy().run(fn, *args, **kwargs)
    return run
