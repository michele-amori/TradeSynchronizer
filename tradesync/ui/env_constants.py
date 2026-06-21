"""
env_constants — the small set of constants shared between the dotenv
store (env_store.py) and the GUI form (app.py).

These describe how settings are split across the three dotenv files:
which keys are per-environment vs shared, and the per-env defaults that
must differ between LIVE and DEMO (two engine subprocesses can't bind
the same port). Kept in their own module so both EnvStore and the GUI
can import them without either depending on the other.
"""

from __future__ import annotations


ENVIRONMENTS = ("live", "demo")

# Keys whose values differ between LIVE and DEMO AND are still managed
# by the GUI form. Everything else in the env files is either shared
# across both engines or preserved verbatim as an "extra".
#
# Note 1: TRADOVATE_CID and TRADOVATE_SEC USED to be per-env but are
# now app-level (loaded from tradesync/_app_credentials.py, which
# is gitignored). They no longer appear in .env.live / .env.demo
# nor in the GUI form.
#
# Note 2: TRADOVATE_USERNAME / TRADOVATE_PASSWORD / TRADOVATE_ACCOUNT_ID
# USED to be here too, edited from the per-env tabs. They were removed
# from the GUI: the user now writes them by hand into .env.live /
# .env.demo. Crucially they are NOT listed here anymore, so EnvStore
# treats them as preserved "extras" (extras_per_env) instead of
# canonical keys — a GUI Save re-emits them verbatim rather than
# overwriting the hand-written values. The engine reads them straight
# from the environment in config.py, unchanged.
PER_ENV_KEYS = frozenset({
    "PROXY_LISTEN_PORT",
})

# Per-env defaults for fields where the LIVE and DEMO defaults must
# differ (two processes can't bind the same port).
PER_ENV_DEFAULTS: dict[str, dict[str, str]] = {
    "PROXY_LISTEN_PORT": {"live": "8080", "demo": "8081"},
}
