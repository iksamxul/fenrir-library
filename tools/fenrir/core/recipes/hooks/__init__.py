"""The built-in recipes' hooks (recipes.md section 4.1, item 6): the Python that adds what their data cannot say, for
Fenrir's own recipes only (policy keeps hooks built-in, and drivers.for_recipe checks the source again).

    fivem.py           FiveM's download page, its basic resources and the host's own, and its server.cfg
    skyrimtogether.py  Skyrim Together's server copied from its kit, refreshed when the kit's version changed
    minecraft          the world in ServerManager, adapted (core/recipes/drivers/minecraft.py, REC-4)

Importing this package registers them: their driver classes (drivers.register), FiveM's writer (writers.register) and
its download page (process.RESOLVERS)."""
from . import fivem, skyrimtogether  # noqa: F401 - each registers itself on import
from ..drivers import minecraft  # noqa: F401,E402 - the minecraft hook's driver registers itself too
