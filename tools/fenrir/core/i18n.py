"""Languages (docs/v3-design/i18n.md): the string table, the engine's messages, and the formatter the pages share.

The table is fenrir/i18n/<lang>/<area>.json: English first and five languages beside it, one file per area, so lanes that
convert different pages never meet in one big file. A page shows t("servers.start") through static/js/i18n.js. The
engine says M("server.not_installed"): a str holding the English text that also knows its id and arguments. Every 2.x
path that compares, logs or prints the English keeps working, while the window, Fenrir Connect and Fenrir Link can show
the same message in their own language.

Messages are a subset of ICU MessageFormat, read the same way here and in i18n.js (tests/i18n_cases.json holds the cases
both must agree on):

    {name}                                          an argument, as plain text
    {n, plural, one {# player} other {# players}}   zero, one, two, few, many, other and =N; # is the number
    {kind, select, game {...} other {...}}
    {n, number}  {size, size}  {when, date}  {when, time}   typed arguments, formatted for the language

A plural or select may hold one more inside a branch, and no deeper. An apostrophe quotes a brace ('{') or, inside a
plural, a #; two apostrophes are one; any other apostrophe is itself, so "friend's" needs nothing. Messages are text,
never HTML, and a value is never read as a message: a player called "{n}" is shown as "{n}".

This module uses nothing but the standard library, so a build script or Fenrir Connect can use it without the engine."""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Context, Decimal, InvalidOperation
from pathlib import Path

# The six languages (PLAN-V3 §7 #13). tag is the variant Intl and CLDR know it by: Portuguese ships as Brazilian (decision
# 9). name is the language in its own words, which is how a language picker shows it whatever the page's language is.
LANGS = {
    "en": {"tag": "en", "dir": "ltr", "name": "English"},
    "ar": {"tag": "ar", "dir": "rtl", "name": "العربية"},
    "es": {"tag": "es", "dir": "ltr", "name": "Español"},
    "de": {"tag": "de", "dir": "ltr", "name": "Deutsch"},
    "fr": {"tag": "fr", "dir": "ltr", "name": "Français"},
    "pt": {"tag": "pt-BR", "dir": "ltr", "name": "Português (Brasil)"},
}
SOURCE = "en"
if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / "i18n"  # build.py ships it beside static
else:
    ROOT = Path(__file__).resolve().parents[1] / "i18n"

KINDS = ("plural", "select", "number", "date", "time", "size")
TYPED = ("number", "date", "time", "size")
CATEGORIES = ("zero", "one", "two", "few", "many", "other")
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SELECTOR = re.compile(r"=[0-9]+|[A-Za-z0-9_-]+")
SPACE = " \t\r\n"
NUMERIC = re.compile(r"-?[0-9]+(\.[0-9]+)?")
# Embeddings, overrides and isolates: a name holding them could make an address or a path read differently ("Trojan
# Source"), and one holding a pop could end the isolate a right-to-left sentence puts around it (i18n.md §5).
BIDI_CONTROLS = re.compile("[\u202a-\u202e\u2066-\u2069]")
FSI, PDI = "\u2068", "\u2069"

# The settings (config ui.*): the language ("" follows Windows), the 12- or 24-hour clock ("" is the region's own),
# Arabic's digits (Western by default, decision 9), a region for formats ("" follows Windows) and the first day of the
# week (0 is the region's own, else 1 Monday to 7 Sunday, as Intl numbers them).
CLOCKS = ("", "12", "24")
DIGITS = ("latn", "arab")
REGION = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z]{4})?(-([A-Za-z]{2}|[0-9]{3}))?")

# Numbers as Intl.NumberFormat writes them in the pages: the decimal mark, the group mark, and how many digits the first
# group needs before grouping starts (Spanish writes 1234 but 12.345). Arabic with Arabic-Indic digits has marks of its own.
NUMBER_MARKS = {"en": (".", ",", 1), "ar": (".", ",", 1), "es": (",", ".", 2), "de": (",", ".", 1), "fr": (",", "\u202f", 1),
                "pt": (",", ".", 1)}
ARABIC_MARKS = ("\u066b", "\u066c", 1)
ARABIC_DIGITS = str.maketrans("0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669")
# A minus in Arabic carries a mark that keeps it in front of the number inside right-to-left text, as Intl writes it.
MINUS = {("ar", "latn"): "\u200e-", ("ar", "arab"): "\u061c-"}
# Sizes as Intl writes megabytes and gigabytes in each language ("1,7 Go" in French).
UNITS = {"en": ("{0} MB", "{0} GB"), "ar": ("{0} \u0645.\u0628", "{0} \u063a.\u0628"), "es": ("{0} MB", "{0} GB"),
         "de": ("{0} MB", "{0}\u00a0GB"), "fr": ("{0}\u202fMo", "{0}\u202fGo"), "pt": ("{0} MB", "{0} GB")}
# The engine's own clock: HH:MM, or h:MM with the language's morning and evening marks. English and Arabic read 12-hour
# by default, the others 24-hour, as their regions do. There is no date library here: the engine sends epoch seconds and
# each app formats them; a date the engine writes itself is ISO (2026-10-13), which reads the same in every language.
DAY_HALVES = {"en": ("AM", "PM"), "ar": ("\u0635", "\u0645"), "es": ("a. m.", "p. m."), "de": ("AM", "PM"),
              "fr": ("AM", "PM"), "pt": ("AM", "PM")}
CLOCK_OF = {"en": "12", "ar": "12"}


class MessageError(ValueError):
    """A message that does not parse: what was expected, where, and the message itself."""


# --------------------------------------------------------------------------- parsing
class _Parser:
    def __init__(self, text):
        self.s, self.i = text, 0

    def error(self, why):
        return MessageError(f"Expected {why} at {self.i} in {self.s!r}")

    def peek(self):
        return self.s[self.i] if self.i < len(self.s) else ""

    def space(self):
        while self.i < len(self.s) and self.s[self.i] in SPACE:
            self.i += 1

    def name(self):
        m = NAME.match(self.s, self.i)
        if not m:
            raise self.error("a name (letters, digits and _)")
        self.i = m.end()
        return m.group(0)

    def message(self, depth, in_plural):
        s, parts, buf = self.s, [], []
        while self.i < len(s):
            c = s[self.i]
            if c == "'":
                nxt = s[self.i + 1] if self.i + 1 < len(s) else ""
                if nxt == "'":
                    buf.append("'")
                    self.i += 2
                    continue
                if nxt in ("{", "}") or (nxt == "#" and in_plural):  # a quoted stretch, up to the next lone apostrophe
                    self.i += 1
                    while True:
                        if self.i >= len(s):
                            raise self.error("an apostrophe to close the quote")
                        if s[self.i] == "'":
                            if s[self.i + 1:self.i + 2] == "'":
                                buf.append("'")
                                self.i += 2
                                continue
                            self.i += 1
                            break
                        buf.append(s[self.i])
                        self.i += 1
                    continue
                buf.append("'")
                self.i += 1
                continue
            if c == "{":
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(self.argument(depth, in_plural))
                continue
            if c == "}":
                break  # the end of a branch: the caller checks it is one
            if c == "#" and in_plural:
                if buf:
                    parts.append("".join(buf))
                    buf = []
                parts.append(("#",))
                self.i += 1
                continue
            buf.append(c)
            self.i += 1
        if buf:
            parts.append("".join(buf))
        return parts

    def argument(self, depth, in_plural):
        self.i += 1
        self.space()
        name = self.name()
        self.space()
        if self.peek() == "}":
            self.i += 1
            return ("arg", name)
        if self.peek() != ",":
            raise self.error("a , or } after the argument's name")
        self.i += 1
        self.space()
        kind = self.name()
        self.space()
        if kind not in KINDS:
            raise self.error(f"one of {', '.join(KINDS)}")
        if kind in TYPED:
            if self.peek() != "}":
                raise self.error(f"a }} after {kind}: Fenrir's messages have no styles")
            self.i += 1
            return ("typed", name, kind)
        if depth >= 2:
            raise self.error("no plural or select nested more than one level deep")
        if self.peek() != ",":
            raise self.error(f"a , after {kind}")
        self.i += 1
        branches = {}
        while True:
            self.space()
            if self.peek() == "}":
                self.i += 1
                break
            if self.i >= len(self.s):
                raise self.error(f"a }} to close the {kind}")
            m = SELECTOR.match(self.s, self.i)
            if not m:
                raise self.error("a branch name")
            key = m.group(0)
            if kind == "plural" and not key.startswith("=") and key not in CATEGORIES:
                raise self.error(f"a plural form ({', '.join(CATEGORIES)} or =N), not {key}")
            if kind == "select" and key.startswith("="):
                raise self.error(f"a select key, not {key}")
            if key in branches:
                raise self.error(f"{key} only once")
            self.i = m.end()
            self.space()
            if self.peek() != "{":
                raise self.error(f"a {{ after {key}")
            self.i += 1
            branches[key] = self.message(depth + 1, in_plural or kind == "plural")
            if self.peek() != "}":
                raise self.error("a } to close the branch")
            self.i += 1
        if "other" not in branches:
            raise self.error(f"an other branch in the {kind}")
        return (kind, name, branches)


_PARSED = {}


def parse(text):
    """A message as parts: text, ("arg", name), ("#",), ("typed", name, kind), or (plural|select, name, {key: parts})."""
    if not isinstance(text, str):
        raise MessageError("A message is text.")
    hit = _PARSED.get(text)
    if hit is not None:
        return hit
    p = _Parser(text)
    parts = p.message(0, False)
    if p.i < len(text):
        raise p.error("a { before this }")
    if len(_PARSED) > 4096:  # messages are a few thousand at most; a bound keeps a bug from growing it for ever
        _PARSED.clear()
    _PARSED[text] = parts
    return parts


def literal(text):
    """Plain text as a message that shows exactly that text: braces quoted, and an apostrophe doubled where it would
    start a quote. For tools that turn today's strings into messages (build/i18n_extract.py)."""
    out = []
    for i, c in enumerate(text):
        if c in "{}":
            out.append(f"'{c}'")
        elif c == "'" and (text[i + 1:i + 2] in ("'", "{", "}", "#") or i + 1 == len(text)):
            out.append("''")
        else:
            out.append(c)
    return "".join(out)


def placeholders(text):
    """The argument names a message uses, for checking that a translation keeps the English one's."""
    names = set()

    def walk(parts):
        for p in parts:
            if isinstance(p, tuple) and len(p) > 1:
                names.add(p[1])
                if p[0] in ("plural", "select"):
                    for branch in p[2].values():
                        walk(branch)
    walk(parse(text))
    return names


# --------------------------------------------------------------------------- plural rules
def _number_of(v):
    """A count or amount from an argument: numbers, and text that is plainly a number. True and False are not counts."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str) and NUMERIC.fullmatch(v):
        return float(v) if "." in v else int(v)
    return None


def _js_number(v):
    """A number as JavaScript's String() writes it, so {n} reads the same from the engine and from a page."""
    if isinstance(v, int):
        return str(v)
    if v != v:
        return "NaN"
    if v in (float("inf"), float("-inf")):
        return "Infinity" if v > 0 else "-Infinity"
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)
    if 1e-6 <= abs(v) < 1e21:
        return format(Decimal(r), "f")
    mantissa, _, exp = r.partition("e")
    return f"{mantissa}e{'+' if int(exp) > 0 else '-'}{abs(int(exp))}"


def plural(lang, n):
    """CLDR's cardinal plural form of n, as Intl.PluralRules picks it in the pages: English and German one and other;
    Spanish, French and Portuguese add many for whole millions; French and Brazilian Portuguese say one for 0 and 1 (and
    for 1.5); Arabic has zero, one, two, few (3 to 10), many (11 to 99) and other, by the last two digits."""
    v = _number_of(n)
    if v is None or v != v or v in (float("inf"), float("-inf")):
        return "other"
    a = abs(v)
    text = _js_number(a)
    whole, _, frac = text.partition(".") if "e" not in text else (str(int(a)), "", "")
    i, decimals = int(whole), len(frac)
    millions = decimals == 0 and i != 0 and i % 1000000 == 0
    if lang == "ar":
        if a == 0:
            return "zero"
        if a == 1:
            return "one"
        if a == 2:
            return "two"
        if decimals == 0 and 3 <= i % 100 <= 10:
            return "few"
        if decimals == 0 and 11 <= i % 100 <= 99:
            return "many"
        return "other"
    if lang == "es":
        return "one" if a == 1 else "many" if millions else "other"
    if lang == "fr":
        return "one" if i in (0, 1) else "many" if millions else "other"
    if lang == "pt":
        return "one" if i in (0, 1) else "many" if millions else "other"
    return "one" if i == 1 and decimals == 0 else "other"  # en, de, and the pseudo-locales, which are English underneath


def categories(lang):
    """The plural forms a language has, as Intl.PluralRules lists them."""
    return {"ar": ("zero", "one", "two", "few", "many", "other"), "es": ("one", "many", "other"), "fr": ("one", "many", "other"),
            "pt": ("one", "many", "other")}.get(lang, ("one", "other"))


# --------------------------------------------------------------------------- formatting
class _Ctx:
    __slots__ = ("lang", "rtl", "clock", "digits", "tz")

    def __init__(self, lang, rtl, clock, digits, tz):
        self.lang, self.rtl, self.clock, self.digits, self.tz = lang, rtl, clock, digits, tz


def format_number(v, lang="en", digits="latn", min_frac=0, max_frac=3):
    """A number as Intl.NumberFormat writes it: the language's marks, up to three decimals, a half rounded away from zero."""
    n = _number_of(v)
    if n is None:
        return str(v)
    if n != n:
        return "NaN"
    arab = lang == "ar" and digits == "arab"
    dec, group, first = ARABIC_MARKS if arab else NUMBER_MARKS.get(lang, NUMBER_MARKS["en"])
    minus = MINUS.get((lang, "arab" if arab else "latn"), "-")
    if n in (float("inf"), float("-inf")):
        return ("" if n > 0 else minus) + "\u221e"
    try:
        q = Decimal(_js_number(abs(n))).quantize(Decimal(1).scaleb(-max_frac), rounding=ROUND_HALF_UP, context=Context(prec=80))
    except InvalidOperation:
        return str(n)
    whole, _, frac = format(q, "f").partition(".")
    frac = frac.rstrip("0").ljust(min_frac, "0")
    if len(whole) >= 3 + first:
        head = len(whole) % 3 or 3
        whole = whole[:head] + "".join(group + whole[k:k + 3] for k in range(head, len(whole), 3))
    out = whole + (dec + frac if frac else "")
    if arab:
        out = out.translate(ARABIC_DIGITS)
    return (minus + out) if n < 0 and q != 0 else out


def format_size(mb, lang="en", digits="latn"):
    """A size in megabytes as the pages say it: MB under a gigabyte (whole from 10 MB), GB with one decimal above."""
    n = _number_of(mb)
    n = 0 if n is None else n
    mbs, gbs = UNITS.get(lang, UNITS["en"])
    if n >= 1024:
        return gbs.replace("{0}", format_number(n / 1024, lang, digits, 1, 1))
    if n >= 10:
        return mbs.replace("{0}", format_number(n, lang, digits, 0, 0))
    k = 1 if n else 0
    return mbs.replace("{0}", format_number(n, lang, digits, k, k))


def _moment(ts, ctx):
    n = _number_of(ts)
    if n is None:
        return None
    try:
        return datetime.fromtimestamp(n, ctx.tz) if ctx.tz is not None else datetime.fromtimestamp(n)
    except (OverflowError, OSError, ValueError):
        return None


def _time(ts, ctx):
    d = _moment(ts, ctx)
    if d is None:
        return _text(ts, ctx)
    if (ctx.clock or CLOCK_OF.get(ctx.lang, "24")) == "12":
        text = f"{d.hour % 12 or 12}:{d.minute:02d} {DAY_HALVES.get(ctx.lang, DAY_HALVES['en'])[d.hour >= 12]}"
    else:
        text = f"{d.hour:02d}:{d.minute:02d}"
    return text.translate(ARABIC_DIGITS) if ctx.lang == "ar" and ctx.digits == "arab" else text


def _date(ts, ctx):
    d = _moment(ts, ctx)
    if d is None:
        return _text(ts, ctx)
    text = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"
    return text.translate(ARABIC_DIGITS) if ctx.lang == "ar" and ctx.digits == "arab" else text


def _plain_text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return BIDI_CONTROLS.sub("", str(v))
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _js_number(v)
    return str(v)


def _text(v, ctx):
    """A value as text inside a sentence: its direction controls taken out, and, in a right-to-left language, isolated
    so a name or an address in another script keeps its own order without reordering the words around it."""
    if isinstance(v, (str, list, tuple)):
        s = ", ".join(_plain_text(x) for x in v) if isinstance(v, (list, tuple)) else _plain_text(v)
        return FSI + s + PDI if ctx.rtl else s
    return _plain_text(v)


def _arg(args, name, ctx):
    v = args.get(name)
    return "{" + name + "}" if v is None else _text(v, ctx)


def _branch(branches, v, lang):
    n = _number_of(v)
    if n is not None:
        for key, parts in branches.items():
            if key.startswith("=") and float(key[1:]) == n:
                return parts
        return branches.get(plural(lang, n), branches["other"])
    return branches["other"]


def _render(parts, args, ctx, out, count):
    for p in parts:
        if isinstance(p, str):
            out.append(p)
            continue
        kind = p[0]
        if kind == "arg":
            out.append(_arg(args, p[1], ctx))
        elif kind == "#":
            v = args.get(count)
            n = _number_of(v)
            out.append(format_number(n, ctx.lang, ctx.digits) if n is not None else _arg(args, count, ctx))
        elif kind == "typed":
            v, typ = args.get(p[1]), p[2]
            if v is None:
                out.append("{" + p[1] + "}")
            elif _number_of(v) is None:
                out.append(_text(v, ctx))
            elif typ == "number":
                out.append(format_number(v, ctx.lang, ctx.digits))
            elif typ == "size":
                out.append(format_size(v, ctx.lang, ctx.digits))
            elif typ == "time":
                out.append(_time(v, ctx))
            else:
                out.append(_date(v, ctx))
        elif kind == "plural":
            _render(_branch(p[2], args.get(p[1]), ctx.lang), args, ctx, out, p[1])
        else:  # select
            v = args.get(p[1])
            _render(p[2].get(_plain_text(v), p[2]["other"]), args, ctx, out, count)


def format_message(text, args=None, lang="en", *, clock="", digits="latn", tz=None, rtl=None):
    """One message filled with its arguments for a language. clock is "12" or "24" ("" the language's own), digits
    "arab" for Arabic-Indic digits in Arabic, tz the time zone for times and dates (this PC's when None), rtl whether
    to isolate text arguments (the language's direction when None). Raises MessageError when it does not parse."""
    ctx = _Ctx(lang, (LANGS.get(lang, {}).get("dir") == "rtl") if rtl is None else bool(rtl), clock if clock in CLOCKS else "",
               digits if digits in DIGITS else "latn", tz)
    out = []
    _render(parse(text), args or {}, ctx, out, None)
    return "".join(out)


# --------------------------------------------------------------------------- the table
class Catalog:
    """Every language's strings, read from its area files when first needed. A language's own table holds only what has
    been translated; merged() is what a page loads, the English with the language's strings over it, so a key that is
    not translated yet shows in English, never as its key. Files that cannot be used are listed in problems, which the
    tests keep empty, instead of stopping the engine: a broken translation must never take a host's servers down."""

    LANG_DIR = re.compile(r"[a-z]{2,3}(-[a-z0-9]{1,8})*")

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._tables = {}
        self._seen = {}
        self.problems = []

    def files(self, lang):
        d = self.root / lang
        if not self.LANG_DIR.fullmatch(lang or "") or not d.is_dir():
            return []
        return sorted(p for p in d.glob("*.json") if not p.name.endswith(".notes.json"))

    @staticmethod
    def area_of(path):
        """msg.json and msg-games.json are both the msg area: a big area can be split so lanes never meet in one file."""
        return Path(path).name[:-len(".json")].split("-")[0]

    def _load(self, lang):
        table, problems = {}, []
        for f in self.files(lang):
            area, where = self.area_of(f), f"{lang}/{f.name}"
            try:
                data = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError) as e:
                problems.append(f"{where}: {e}")
                continue
            if not isinstance(data, dict):
                problems.append(f"{where}: not an object of keys and messages")
                continue
            for key, text in data.items():
                if not isinstance(text, str):
                    problems.append(f"{where}: {key} is not text")
                    continue
                if not key.startswith(area + "."):
                    problems.append(f"{where}: {key} does not start with {area}.")
                if key in table:
                    problems.append(f"{where}: {key} is in another file too")
                table[key] = text
        return table, problems

    def table(self, lang):
        with self._lock:
            if lang not in self._tables:
                table, problems = self._load(lang)
                self._tables[lang] = table
                self.problems = [p for p in self.problems if not p.startswith(lang + "/")] + problems
            return self._tables[lang]

    def get(self, lang, key):
        """One message's text in a language, else in English, else None."""
        text = self.table(lang).get(key) if lang != SOURCE else None
        return text if text is not None else self.table(SOURCE).get(key)

    def merged(self, lang):
        self._fresh(SOURCE)
        en = self.table(SOURCE)
        if lang == SOURCE:
            return dict(en)
        self._fresh(lang)
        own = self.table(lang)
        return {k: own[k] if k in own and _parses(own[k]) else v for k, v in en.items()}  # a page never gets one that does not

    def _fresh(self, lang):
        """Running from source, an edited area file is read again at the next page load, so a string can change without
        restarting the engine. A built app's files never change, so it never looks."""
        if getattr(sys, "frozen", False):
            return
        try:
            seen = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.files(lang))
        except OSError:
            return
        with self._lock:
            if self._seen.get(lang) != seen:
                if lang in self._seen:
                    self._tables.pop(lang, None)
                self._seen[lang] = seen

    def reload(self):
        with self._lock:
            self._tables.clear()
            self._seen.clear()
            self.problems = []


CATALOG = Catalog(ROOT)


def _parses(text):
    try:
        parse(text)
        return True
    except MessageError:
        return False


def say(key, args=None, lang="en", **opts):
    """Any key of the table in a language: its translation, else its English, else the key. A translation that does not
    parse falls back to the English, so a bad file never shows a broken sentence or stops what was being said."""
    if lang != SOURCE and lang in LANGS:
        own = CATALOG.table(lang).get(key)
        if own is not None:
            try:
                return format_message(own, args, lang, **opts)
            except MessageError:
                pass
    src = CATALOG.table(SOURCE).get(key)
    if src is None:
        return key
    try:
        return format_message(src, args, SOURCE, **opts)
    except MessageError:
        return src


def render(mid, args=None, lang="en", **opts):
    """An engine message (an id of en/msg*.json, without the msg. in front) in a reader's language: a push to a phone in
    the device's language, a Discord post in the guild's, a toast in the host's (i18n.md §3.3)."""
    return say("msg." + mid, args, lang, **opts)


def relative(ts, lang="en", now=None):
    """"in 10 minutes" or "3 hours ago" for notifications, from the table's time.in.* and time.ago.* (pages use Intl's)."""
    d = (_number_of(ts) or 0) - (time.time() if now is None else now)
    a = abs(d)
    if a < 60:
        return say("time.just_now", None, lang)
    unit, n = ("minutes", int(a / 60 + 0.5)) if a < 3600 else ("hours", int(a / 3600 + 0.5)) if a < 86400 else ("days", int(a / 86400 + 0.5))
    return say(f"time.{'in' if d > 0 else 'ago'}.{unit}", {"n": n}, lang)


# --------------------------------------------------------------------------- the engine's messages
def _plain(v):
    if v is None or isinstance(v, bool) or isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else str(v)  # JSON has no NaN: a page would refuse the answer
    if isinstance(v, str):
        return str(v)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return str(v)


def plain_args(args):
    """Arguments as JSON carries them: text, numbers, true or false, null, and lists of those; anything else as its text."""
    return {str(k): _plain(v) for k, v in (args or {}).items()}


class Msg(str):
    """English text that remembers the message it was made from (M() makes them). Being a str, it still equals the
    English, json writes it as that text, and every 2.x test and code path that compares English text keeps working.
    .id and .args let a reader in another language see the same message in theirs. Joining it to more text gives a
    plain str: the result is no longer that message."""

    def __new__(cls, text="", mid="", args=None):
        self = super().__new__(cls, text)
        self.id = mid
        self.args = dict(args or {})
        return self

    def __reduce__(self):  # a copy or a pickle keeps the id: str's own would rebuild it from the text alone
        return (Msg, (str(self), self.id, self.args))

    def ref(self):
        """{id, args}, as answers, timeline lines and events carry it beside the English (i18n.md §3.3, roles.md §3.4)."""
        return {"id": self.id, "args": plain_args(self.args)}

    def render(self, lang, **opts):
        return render(self.id, self.args, lang, **opts) if self.id else str(self)


def M(mid, /, **args):
    """The engine's message by id (a key of en/msg*.json without its msg.), as English text that knows its id and
    arguments: raise ServerError(M("server.not_installed")) or return {"ok": False, "error": M(...)}. The id is always
    written out, never built, so tests/test_i18n_messages.py can check that each one exists. An id with no English shows
    as itself rather than failing whatever was being reported."""
    src = CATALOG.table(SOURCE).get("msg." + mid)
    if src is None:
        return Msg(mid, mid, args)
    try:
        return Msg(format_message(src, args, SOURCE), mid, args)
    except MessageError:
        return Msg(src, mid, args)


def text_for(value, lang):
    """What a reader in lang sees for a message, a message's {id, args}, or plain text (shown as it is)."""
    if isinstance(value, Msg) and value.id:
        return value.render(lang)
    if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"]:
        args = value.get("args")
        return render(value["id"], args if isinstance(args, dict) else {}, lang)
    return "" if value is None else str(value)


def text_of(err):
    """An exception's message, keeping it a Msg when it was raised with one (str(err) would make it plain text)."""
    a = getattr(err, "args", ())
    return a[0] if len(a) == 1 and isinstance(a[0], Msg) else str(err)


def with_msg(body):
    """A 2.x answer with {id, args} beside its English error when the error is a message (i18n.md §3.3). Older apps read
    error as before and never see the new key."""
    if isinstance(body, dict) and isinstance(body.get("error"), Msg) and body["error"].id and "msg" not in body:
        return {**body, "msg": body["error"].ref()}
    return body


# --------------------------------------------------------------------------- the host's language
_OS = {}


def _windows_languages():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    count, size = wintypes.ULONG(0), wintypes.ULONG(0)
    if not k32.GetUserPreferredUILanguages(8, ctypes.byref(count), None, ctypes.byref(size)) or not size.value:  # 8: names, en-US
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if not k32.GetUserPreferredUILanguages(8, ctypes.byref(count), buf, ctypes.byref(size)):
        return []
    return [x for x in buf[:size.value].split("\0") if x]


def _posix_tag(value):
    tag = value.split(".")[0].split("@")[0].replace("_", "-")
    return "" if tag in ("", "C", "POSIX") else tag


def posix_languages(environ):
    """The languages a Linux system asks for (LANGUAGE's list, then LC_ALL, LC_MESSAGES and LANG): Fenrir Server's."""
    found = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        for value in environ.get(var, "").split(":"):
            tag = _posix_tag(value)
            if tag and tag not in found:
                found.append(tag)
    return found


def posix_region(environ):
    """A Linux system's format for dates and numbers."""
    return next((t for t in (_posix_tag(environ.get(v, "")) for v in ("LC_ALL", "LC_TIME", "LANG")) if t), "")


def os_languages():
    """The languages the system shows its own screens in, the first choice first (Windows: Settings, Time & language),
    for example ["en-US", "ar-SA"]; on Linux (Fenrir Server) the locale variables. Read once, as Windows' own apps do."""
    if "langs" not in _OS:
        found = []
        try:
            found = _windows_languages() if os.name == "nt" else posix_languages(os.environ)
        except (OSError, AttributeError, ValueError):
            found = []
        _OS["langs"] = found
    return list(_OS["langs"])


def os_region():
    """The system's format for dates and numbers (Windows: Settings, Time & language, Region), for example "en-AE"."""
    if "region" not in _OS:
        found = ""
        try:
            if os.name == "nt":
                import ctypes
                buf = ctypes.create_unicode_buffer(85)  # LOCALE_NAME_MAX_LENGTH
                if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
                    found = buf.value
            else:
                found = posix_region(os.environ)
        except (OSError, AttributeError, ValueError):
            found = ""
        _OS["region"] = found if REGION.fullmatch(found or "") else ""
    return _OS["region"]


def resolve(pref="", os_langs=None):
    """The language to show: the one chosen; else the first of the system's languages that Fenrir has (Follow Windows);
    else English (i18n.md §4). A tag like pt-PT or es-419 counts as its language."""
    base = pref.strip().lower().replace("_", "-").split("-")[0] if isinstance(pref, str) else ""
    if base in LANGS:
        return base
    for tag in os_languages() if os_langs is None else os_langs:
        base = str(tag).strip().lower().replace("_", "-").split("-")[0]
        if base in LANGS:
            return base
    return SOURCE


def prefs(ui=None):
    """The host's language and format settings (config ui.*), each checked, so a value that makes no sense is the default."""
    if ui is None:
        try:
            from .config import CFG
            ui = CFG.ui()
        except Exception:  # noqa: BLE001 - outside the engine (a build script, a test of this module alone) there are no settings
            ui = {}
    ui = ui if isinstance(ui, dict) else {}

    def pick(key, allowed, default):
        v = ui.get(key)
        return v if isinstance(v, str) and v in allowed else default
    region, week = ui.get("region"), ui.get("weekStart")
    return {"lang": pick("lang", LANGS, ""), "clock": pick("clock", CLOCKS, ""), "digits": pick("digits", DIGITS, "latn"),
            "region": region if isinstance(region, str) and REGION.fullmatch(region) else "",
            "weekStart": week if isinstance(week, int) and not isinstance(week, bool) and 1 <= week <= 7 else 0}


def host_lang():
    """The host's language: what the engine renders in itself (toasts in the shell, Discord posts, Ask Fenrir's answers)."""
    return resolve(prefs()["lang"])


def boot(lang=None):
    """What the window's page starts with: the language, the settings, the system's languages and region, and the whole
    table. core/api.py puts it into index.html where it says __FENRIR_I18N__, so the first paint is already in the host's
    language and direction, with no request to wait for and no flash of English."""
    p = prefs()
    lang = lang if lang in LANGS else resolve(p["lang"])
    return {"lang": lang, "prefs": p, "os": {"langs": os_languages(), "region": os_region()}, "strings": CATALOG.merged(lang)}


def _script_safe(data):
    # No "<" survives (JSON reads < back as "<"), so nothing inside can close the script element early.
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def boot_json(lang=None):
    """boot() as text that is safe inside <script type="application/json">."""
    return _script_safe(boot(lang))


def table_json(lang):
    """One language's whole table, for a page that changes language without reloading (GET /i18n/<lang>.json)."""
    return _script_safe(CATALOG.merged(lang))
