"""
TradeSynchronizer GUI launcher.

Used by the macOS .app bundle and by manual `python3 gui.py`.
Real code lives in `tradesync.ui.app` — this file exists so the
bundle has a single, stable entry point to call.

On macOS, Tkinter inherits the menu-bar app name from the embedded
Python framework's Info.plist (which says "Python"). We override
it via NSBundle BEFORE importing tkinter so the menu bar reads
"TradeSynchronizer" instead. Requires pyobjc-framework-Cocoa on
macOS; on other platforms the import is silently skipped.
"""

import sys


def _set_macos_app_name(name: str = "TradeSynchronizer") -> None:
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
    except ImportError:
        # pyobjc not installed — menu bar will say "Python" but the
        # app still works. Not fatal; `pip install -r requirements.txt`
        # to fix.
        return

    bundle = NSBundle.mainBundle()
    if bundle is None:
        return
    info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
    if info is None:
        return
    # NSBundle's infoDictionary is technically immutable but the
    # underlying NSDictionary accepts setItem assignment in practice;
    # this is the standard PyObjC trick used by py2app, rumps, etc.
    info["CFBundleName"] = name
    info["CFBundleDisplayName"] = name


_set_macos_app_name()


from tradesync.ui.app import main  # noqa: E402  (after the rename above)


if __name__ == "__main__":
    main()
