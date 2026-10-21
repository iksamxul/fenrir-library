"""The pages a built-in resolver reads (recipes.md section 3.11.2), made for the harness: each names downloads on the fake
download server, so a resolver recipe's install runs through Fenrir's own code and nothing comes from the internet. The
harness hands the driver's install two seams, page (a page's bytes, as gamekit.net.get's) and fetch (a file, as
gamekit.net.download's).

    fivem-page   FiveM's server download page, its __NEXT_DATA__ naming the Windows server's zip (the recipe's program
                 inside), and citizenfx/cfx-server-data's zip with the three basic resources 2.x's install takes

FAKES maps a resolver to fn(downloads, view) -> {page address: bytes}: fn adds its files to the fake download server."""
import io
import json
import zipfile

from . import play as P


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, text in sorted(files.items()):
            z.writestr(rel, text)
    return buf.getvalue()


def fivem(downloads, view):
    from ...games import fivem as fivem2x
    url = "https://downloads.cfx-services.net/harness/1/cfx-server_win_x64.zip"  # the host 2.x's page check allows
    downloads.add(url, P.archive({"url": url, "unpack": "zip"}, view)[0])
    top = "cfx-server-data-master/resources"
    downloads.add(fivem2x.SERVER_DATA, _zip({f"{top}/[managers]/mapmanager/fxmanifest.lua": "fx_version 'cerulean'\n",
                                             f"{top}/[managers]/spawnmanager/fxmanifest.lua": "fx_version 'cerulean'\n",
                                             f"{top}/[gameplay]/basic-gamemode/fxmanifest.lua": "fx_version 'cerulean'\n"}))
    data = {"props": {"pageProps": {"enhanced": {"windows": [{"downloadURL": url, "displayName": "cfx-server_win_x64.zip", "subtitle": "build 1"}]}}}}
    page = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'
    return {fivem2x.DOCS: page.encode("utf-8")}


FAKES = {"fivem-page": fivem}
