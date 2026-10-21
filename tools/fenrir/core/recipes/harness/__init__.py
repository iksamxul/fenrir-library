"""The fake-server harness (recipes.md section 3.11): a recipe's whole life on any PC, in seconds, with no game, no Steam
and no network beyond 127.0.0.1. The tests, Check before sharing in Fenrir and the library's GitHub Action all run it.

    fake_server.py     the program that stands in for the real one: arguments, files, lines, ports, players, stops
    wire.py            what its ports answer: Steam's query, RCON, Minecraft's status, HTTP, a plain accept
    fake_steamcmd.py   SteamCMD for core/games/steamcmd.py's runner seam
    fake_downloads.py  a local server for download recipes, their https hosts mapped to it at fetch time
    fake_docker.py     a docker command first on PATH, its containers being the fake server
    play.py            what the fake plays for one run, and what a fake install lays down
    regexes.py         every pattern of a recipe timed on lines made to be slow
    run.py             the life it runs, step by step, against a driver, and the report

    python -m core.recipes.harness <recipe.json>

Only the program is fake: the driver that runs it is Fenrir's own (run.py names the few calls it makes)."""
