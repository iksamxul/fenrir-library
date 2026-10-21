"""Games beyond Minecraft: the host's side. The library scan, the mod engine and the sources live in fenrir/gamekit, which
Fenrir Connect shares; this package keeps what only the host does:

    webapi.py    the Games page's routes
    servers.py   dedicated servers for modded games (SteamCMD installs, a console of their own, Ctrl+C to save and stop)
    fivem.py     GTA V's modded multiplayer: a FiveM server with the host's own key
    steamcmd.py, a2s.py, upnp.py, consolectl.py   Valve's installer, Steam's server query, router ports, Ctrl+C
"""
