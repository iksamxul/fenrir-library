"""The fake download server (recipes.md section 3.11.2): a local HTTP server for download recipes, serving the archives
the harness makes. A recipe keeps its https addresses (the schema refuses http, the policy refuses addresses by number);
only at fetch time is each address's host mapped to this server, and the download itself is Fenrir's own
(gamekit.net.download, which takes plain http to 127.0.0.1 only), with its size and sha256 checks:

    https://dl.gitea.com/gitea/1.0.0/gitea.exe   ->   http://127.0.0.1:<port>/dl.gitea.com/gitea/1.0.0/gitea.exe

The archive's sha256 is the harness's to know, so the recipe it runs carries that hash in place of the real one."""
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeDownloads:
    """Files by their https address, served on 127.0.0.1 while it runs. hits lists the paths asked for."""

    def __init__(self):
        self.files, self.hits = {}, []
        self.httpd = None

    @staticmethod
    def key(url):
        u = urllib.parse.urlsplit(url)
        return f"/{u.hostname}{u.path}"

    def add(self, url, data):
        self.files[self.key(url)] = bytes(data)

    def start(self):
        files, hits = self.files, self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the name http.server calls
                path = urllib.parse.urlsplit(self.path).path
                hits.append(path)
                data = files.get(path)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="fake-downloads").start()
        return self

    def local(self, url):
        """The address on this server that stands for one https address."""
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{self.key(url)}"

    def fetch(self, url, dest, size=None, sha256=None, progress=None, **kw):
        """What the driver's download seam takes: Fenrir's own download of the mapped address, checked as it always is."""
        from gamekit import net
        if urllib.parse.urlsplit(url).scheme != "https":
            raise net.NetError("That download's address is not a secure one.")  # the harness keeps the recipe's rule
        return net.download(self.local(url), dest, size=size, sha256=sha256, progress=(lambda f: progress(f * 100, "Downloading")) if progress else None,
                            tries=1, **kw) or dest

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
