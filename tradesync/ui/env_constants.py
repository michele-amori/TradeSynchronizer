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

# Keys whose values differ between LIVE and DEMO. Everything else in
# the settings file is shared across both engines.
#
# Note: TRADOVATE_CID and TRADOVATE_SEC USED to be per-env but are
# now app-level (loaded from tradesync/_app_credentials.py, which
# is gitignored). They no longer appear in .env.live / .env.demo
# nor in the GUI form.
PER_ENV_KEYS = frozenset({
    "TRADOVATE_USERNAME",
    "TRADOVATE_PASSWORD",
    "TRADOVATE_ACCOUNT_ID",
    "PROXY_LISTEN_PORT",
})

# Per-env defaults for fields where the LIVE and DEMO defaults must
# differ (two processes can't bind the same port).
PER_ENV_DEFAULTS: dict[str, dict[str, str]] = {
    "PROXY_LISTEN_PORT": {"live": "8080", "demo": "8081"},
}
