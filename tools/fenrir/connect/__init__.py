"""Fenrir Connect: the friend-side app. Standard library only, no dependency on the host's config or server code."""

VERSION = "2.9.0"  # Fenrir, the host app: Settings → Apps, About, support bundles. Every release moves it on
CONNECT_VERSION = "2.9.0"  # Fenrir Connect, the friends' app: only when its own files change, or every friend downloads it again
PRODUCT = "Fenrir"  # the host app's name on every surface (titles, About, shortcuts, notifications); the exe stays Fenrir.exe
CONNECT_PRODUCT = "Fenrir Connect"  # the friends' app's name; the exe stays FenrirConnect.exe so self-updates keep working
CONNECT_FILES = "73de37d921eafe87"  # the fingerprint of those files at CONNECT_VERSION (tests/test_versions.py says when to change both)
