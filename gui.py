"""
TradeSynchronizer GUI launcher.

Used by the macOS .app bundle and by manual `python3 gui.py`.
Real code lives in `tradesync.ui.app` — this file exists so the
bundle has a single, stable entry point to call.
"""

from tradesync.ui.app import main

if __name__ == "__main__":
    main()
