"""HTTP for the games' sources: retries with pauses (some networks reset connections to Cloudflare-fronted sites now and
then: thunderstore.io and github.io both did on this PC), gzip handled, an overall deadline, downloads checked by size
or hash when the source gives one. Standard library only."""
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Fenrir (https://iksamxul.github.io/fenrir/; mod manager)"
PAUSES = (0.5, 1, 2, 3, 5, 8, 10)  # resets on some networks are random, not sustained: many quick tries beat a few long waits


class NetError(Exception):
    """Said to the host as it is."""


def _agent():
    """The app and its version (set_app), then where to read about it: the makers' sites see who asks."""
    from . import app
    a = app()
    return f"{a['name'].replace(' ', '')}/{a['version']} {UA}"


def request(url, data=None, headers=None, method=None):
    h = {"User-Agent": _agent(), "Accept-Encoding": "gzip"}
    h.update(headers or {})
    return urllib.request.Request(url, data=data, headers=h, method=method)


def _secure(url):
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost"))


def get(url, headers=None, limit=64 * 2 ** 20, timeout=30, deadline=120, tries=len(PAUSES) + 1, sleep=time.sleep):
    """The body of a GET, gunzipped. Connection resets and 5xx are tried again; 4xx are not."""
    if not _secure(url):
        raise NetError("That address is not a secure one.")
    last = None
    for attempt in range(tries):
        t0 = time.time()
        try:
            with urllib.request.urlopen(request(url, headers=headers), timeout=timeout) as r:
                body = b""
                while chunk := r.read(1 << 16):
                    body += chunk
                    if len(body) > limit:
                        raise NetError("The answer was far larger than expected.")
                    if time.time() - t0 > deadline:
                        raise NetError("The answer came in too slowly.")
                if r.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise NetError(f"{urllib.parse.urlsplit(url).hostname} answered {e.code}.") from None
            last = e
        except (OSError, EOFError) as e:  # resets, timeouts, a cut gzip stream
            last = e
        if attempt < tries - 1:
            sleep(PAUSES[min(attempt, len(PAUSES) - 1)])
    raise NetError(f"{urllib.parse.urlsplit(url).hostname} could not be reached ({getattr(last, 'reason', None) or last}).")


def final_url(url, tries=4, sleep=time.sleep):
    """Where a redirecting address ends up (GitHub's releases/latest → its tag's page), reading as little as possible."""
    if not _secure(url):
        raise NetError("That address is not a secure one.")
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request(url), timeout=30) as r:
                r.read(1)
                return r.geturl()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise NetError(f"{urllib.parse.urlsplit(url).hostname} answered {e.code}.") from None
            last = e
        except OSError as e:
            last = e
        if attempt < tries - 1:
            sleep(PAUSES[min(attempt, len(PAUSES) - 1)])
    raise NetError(f"{urllib.parse.urlsplit(url).hostname} could not be reached ({last}).")


def get_json(url, headers=None, **kw):
    try:
        return json.loads(get(url, headers={"Accept": "application/json", **(headers or {})}, **kw))
    except ValueError:
        raise NetError("The answer could not be read.") from None


def post_json(url, payload, headers=None, timeout=30, tries=3, sleep=time.sleep):
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(tries):
        try:
            req = request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise NetError(f"{urllib.parse.urlsplit(url).hostname} answered {e.code}.") from None
            last = e
        except (OSError, ValueError) as e:
            last = e
        if attempt < tries - 1:
            sleep(PAUSES[min(attempt, len(PAUSES) - 1)])
    raise NetError(f"{urllib.parse.urlsplit(url).hostname} could not be reached ({last}).")


DOWNLOAD_PAUSES = (1, 2, 3, 5, 8, 10, 15)


def download(url, dest, size=None, sha256=None, md5=None, headers=None, progress=None, timeout=60, tries=8, sleep=time.sleep, mirrors=()):
    """A file to `dest`, checked against the size and hash the source gave (when it gave them); a file that does not match
    is removed. Tried again from the start after a reset (sources here do not all honour ranges), going round the source's
    mirrors when it has some: some networks reset connections to one CDN host now and then (this PC's did)."""
    urls = [url, *mirrors]
    for u in urls:
        if not _secure(u):
            raise NetError("That download's address is not a secure one.")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(tries):
        url = urls[attempt % len(urls)]
        h256, h5, got, t0 = hashlib.sha256(), hashlib.md5(), 0, time.time()
        deadline = max(900.0, (size or 0) / (20 * 1024))
        try:
            with urllib.request.urlopen(request(url, headers=headers), timeout=timeout) as r, open(dest, "wb") as out:
                if not _secure(r.geturl()):
                    raise NetError("The download was sent to an address that is not a secure one.")
                total = size or int(r.headers.get("Content-Length") or 0) or None
                while chunk := r.read(1 << 20):
                    got += len(chunk)
                    if size and got > size:
                        raise NetError("The download is larger than its source says, so it was not used.")
                    h256.update(chunk)
                    h5.update(chunk)
                    out.write(chunk)
                    if progress and total:
                        progress(min(1.0, got / total))
                    if time.time() - t0 > deadline:
                        raise NetError("The download stalled.")
            if (size and got != size) or (sha256 and h256.hexdigest() != sha256.lower()) or (md5 and h5.hexdigest() != md5.lower()):
                raise NetError("The download does not match what its source says (size or checksum), so it was not used.")
            return dest
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:
                _unlink(dest)
                raise NetError(f"{urllib.parse.urlsplit(url).hostname} answered {e.code}.") from None
        except NetError:
            _unlink(dest)
            raise
        except OSError as e:
            last = e
        _unlink(dest)
        if attempt < tries - 1:
            sleep(DOWNLOAD_PAUSES[min(attempt, len(DOWNLOAD_PAUSES) - 1)])
    raise NetError(f"The download could not be finished ({getattr(last, 'reason', None) or last}). Try again in a moment.")


def _unlink(p):
    try:
        Path(p).unlink()
    except OSError:
        pass
