"""NOT Fenrir's app.py: a stand-in made by fenrir/build/library_tools.py with only app.py's own blocks for the two
helpers the process driver starts as `python app.py ...` (core/games/consolectl.py): --ctrl-c sends Ctrl+C to a
server's console so it saves, and --console-type types a line into it."""
import sys

if "--ctrl-c" in sys.argv:  # this process sends Ctrl+C to a game server's console so it saves (core/games/consolectl.py)
    from core.games.consolectl import main as _ctrl_c
    sys.exit(_ctrl_c(sys.argv))

if "--console-type" in sys.argv:  # this process types a line into the console of the host's own server (core/games/consolectl.py)
    from core.games.consolectl import main_type as _console_type
    sys.exit(_console_type(sys.argv))

sys.exit("This stand-in for Fenrir's app.py runs only --ctrl-c and --console-type.")
