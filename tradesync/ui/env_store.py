"""
env_store — in-memory representation of the project's three dotenv
files (.env shared + .env.live + .env.demo), with targeted per-file
writes so the two engines never disturb each other's config.

Extracted from ui/app.py to keep persistence logic separate from the
Tkinter presentation layer (and unit-testable without a display). The
GUI imports EnvStore from here; app.py re-exports it for backward
compatibility with existing imports.
"""

from __future__ import annotations

from pathlib import Path

from .env_constants import ENVIRONMENTS, PER_ENV_DEFAULTS, PER_ENV_KEYS


# Keys that EnvStore._build_shared emits by name into .env. Any other
# key found in .env at load time is preserved as an "extra" and
# re-emitted at the end of the file by the same builder, so manual
# edits don't vanish on the next Save. Kept in sync with the literal
# emit list in _build_shared.
_CANONICAL_SHARED_KEYS = frozenset({
    "TRADOVATE_APP_ID",
    "TRADOVATE_APP_VERSION",
    "TRADOVATE_IS_AUTOMATED",
    "PROXY_LISTEN_HOST",
    "AUTO_LAUNCH_TRADINGVIEW",
    "LOG_LEVEL",
    "LOG_FILE",
    "VERBOSE_TROUBLESHOOTING",
})

# Buckets used by snapshot/write to address one file at a time.
SHARED = "shared"
_FILE_BUCKETS = (SHARED, "live", "demo")


class EnvStore:
    """
    In-memory representation of the project's THREE dotenv files:

        .env        — shared settings (proxy host, replication
                      policy, logging, app metadata)
        .env.live   — LIVE-only credentials, port, IBKR watchlist
        .env.demo   — DEMO-only credentials, port, IBKR watchlist

    Layout in memory:

        self.shared    = {"PROXY_LISTEN_HOST": "127.0.0.1", ...}
        self.per_env   = {"live": {"TRADOVATE_USERNAME": "foo", ...},
                          "demo": {"TRADOVATE_USERNAME": "",    ...}}

    The GUI can save just the files that actually changed (targeted
    write) so the two engines never end up touching each other's
    config — modifying DEMO while LIVE is running cannot disturb
    LIVE's file on disk.

    Migration: if .env doesn't exist yet but .env.live or .env.demo
    contains shared keys (legacy from the previous design where
    they were duplicated in both env files), load() still picks the
    shared values up — they migrate to .env on the next Save.
    """

    def __init__(self, project_root: Path):
        self.shared_path: Path = project_root / ".env"
        self.env_paths: dict[str, Path] = {
            env: project_root / f".env.{env}" for env in ENVIRONMENTS
        }
        self.shared:  dict[str, str]            = {}
        self.per_env: dict[str, dict[str, str]] = {e: {} for e in ENVIRONMENTS}
        # Extra keys found in the files at load time that the canonical
        # builders (_build_shared / _build_env) don't emit by name.
        # We preserve them across a load → write round-trip so the user
        # can drop ad-hoc settings into a .env file (e.g. TRADOVATE_CID,
        # TRADOVATE_DEVICE_ID — anything not in the GUI form) without
        # them silently vanishing on the next Save. Indexed by file
        # bucket so per-env extras stay scoped to the right engine.
        self.extras_shared: dict[str, str] = {}
        self.extras_per_env: dict[str, dict[str, str]] = {
            e: {} for e in ENVIRONMENTS
        }

    # ── parsing helper ────────────────────────────────────────────── #

    @staticmethod
    def _parse(path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        if not path.exists():
            return out
        for line in path.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            out[k.strip()] = v.strip()
        return out

    # ── load ──────────────────────────────────────────────────────── #

    def load(self) -> None:
        """
        Read all three files into memory. Shared keys come from .env
        (authoritative); per-env keys come from .env.live / .env.demo.

        Unknown keys are preserved verbatim in `extras_*`, NOT migrated:
        if the user wrote TRADOVATE_CID into .env.live (legitimate use —
        it's an override for that engine's app credentials), it stays
        in extras_per_env["live"] and the next write rebuilds the file
        with that line re-emitted at the bottom. Previous behaviour
        silently migrated unknown env-file keys into .shared via
        setdefault, then _build_shared dropped any key not in its
        hardcoded list — so the value vanished after a single
        load → write round-trip. That bug was responsible for the
        engine flipping into shadow mode after the GUI auto-fixed a
        port collision (the patch wrote .env.live and silently lost
        TRADOVATE_CID / TRADOVATE_SEC / TRADOVATE_DEVICE_ID).
        """
        self.shared = {}
        self.per_env = {e: {} for e in ENVIRONMENTS}
        self.extras_shared = {}
        self.extras_per_env = {e: {} for e in ENVIRONMENTS}

        # 1. Shared file: split into canonical-shared vs extras.
        for k, v in self._parse(self.shared_path).items():
            if k == "TRADOVATE_ENVIRONMENT":
                continue
            if k in PER_ENV_KEYS:
                continue   # shared file shouldn't have per-env keys
            if k in _CANONICAL_SHARED_KEYS:
                self.shared[k] = v
            else:
                self.extras_shared[k] = v

        # 2. Env-specific files: split into canonical-per-env, legacy
        #    stray shared (migrated), and unknown extras (preserved).
        for env in ENVIRONMENTS:
            for k, v in self._parse(self.env_paths[env]).items():
                if k == "TRADOVATE_ENVIRONMENT":
                    continue
                if k in PER_ENV_KEYS:
                    self.per_env[env][k] = v
                elif k in _CANONICAL_SHARED_KEYS:
                    # Legacy: an old two-file deployment may have had
                    # canonical shared keys duplicated into env files.
                    # setdefault preserves the .env value if both exist.
                    self.shared.setdefault(k, v)
                else:
                    # Truly unknown key (e.g. TRADOVATE_CID overrides
                    # for an env-specific Tradovate app registration).
                    # Keep it scoped to that engine, do NOT migrate.
                    self.extras_per_env[env][k] = v

    # ── value access ──────────────────────────────────────────────── #

    def get_env(self, env: str, key: str) -> str:
        if key in PER_ENV_KEYS:
            return self.per_env[env].get(key, "")
        return self.shared.get(key, "")

    def set_env(self, env: str, key: str, value: str) -> None:
        if key in PER_ENV_KEYS:
            self.per_env[env][key] = value
        else:
            self.shared[key] = value

    # ── snapshot (per-file, used for dirty tracking) ──────────────── #

    def snapshot_per_file(self) -> dict[str, tuple]:
        snap: dict[str, tuple] = {
            SHARED: tuple(sorted(self.shared.items())),
        }
        for env in ENVIRONMENTS:
            snap[env] = tuple(sorted(self.per_env[env].items()))
        return snap

    # Convenience: combined snapshot, equivalent to old .snapshot().
    def snapshot(self) -> tuple:
        s = self.snapshot_per_file()
        return tuple(s[bucket] for bucket in _FILE_BUCKETS)

    # ── write (targeted) ──────────────────────────────────────────── #

    def write(self, only: set[str] | None = None) -> list[Path]:
        """
        Write the dotenv files. If `only` is None, write all three;
        otherwise write only the named buckets ('shared', 'live',
        'demo'). Returns the list of paths actually written.

        Targeted writes are the heart of environment independence:
        if the user modified only the Demo tab and clicks Save, only
        .env.demo gets touched — .env.live's mtime stays unchanged,
        so a running LIVE engine can't possibly notice anything.
        """
        if only is None:
            only = set(_FILE_BUCKETS)
        written: list[Path] = []
        if SHARED in only:
            self.shared_path.write_text("\n".join(self._build_shared()))
            written.append(self.shared_path)
        for env in ENVIRONMENTS:
            if env in only:
                self.env_paths[env].write_text(
                    "\n".join(self._build_env(env))
                )
                written.append(self.env_paths[env])
        return written

    def _build_shared(self) -> list[str]:
        s = self.shared
        lines = [
            "# TradeSynchronizer — settings shared by every engine.",
            "# Auto-managed by the GUI's \"General\" tab — feel free to edit by hand.",
            "# Per-environment data (credentials, ports, IBKR watch lists) lives",
            "# in .env.live and .env.demo.",
            "",
            "# ── Tradovate application metadata ──────────────────────────────── #",
            f"TRADOVATE_APP_ID={s.get('TRADOVATE_APP_ID', 'TradeSynchronizer')}",
            f"TRADOVATE_APP_VERSION={s.get('TRADOVATE_APP_VERSION', '1.0')}",
            f"TRADOVATE_IS_AUTOMATED={s.get('TRADOVATE_IS_AUTOMATED', 'false')}",
            "",
            "# ── Proxy listen host (ports are per-engine) ────────────────────── #",
            f"PROXY_LISTEN_HOST={s.get('PROXY_LISTEN_HOST', '127.0.0.1')}",
            "",
            "# ── TradingView Desktop ─────────────────────────────────────────── #",
            f"AUTO_LAUNCH_TRADINGVIEW={s.get('AUTO_LAUNCH_TRADINGVIEW', 'true')}",
            "",
            "# ── Logging ─────────────────────────────────────────────────────── #",
            f"LOG_LEVEL={s.get('LOG_LEVEL', 'INFO')}",
            f"LOG_FILE={s.get('LOG_FILE', '~/Library/Logs/TradeSynchronizer/tradesync.log')}",
            f"VERBOSE_TROUBLESHOOTING={s.get('VERBOSE_TROUBLESHOOTING', 'true')}",
        ]
        # Preserve any extra keys the user added manually (e.g. flags
        # we haven't formalised yet, or experimental overrides).
        if self.extras_shared:
            lines.append("")
            lines.append("# ── Extras preserved from previous load ─────────────────────────── #")
            for k in sorted(self.extras_shared):
                lines.append(f"{k}={self.extras_shared[k]}")
        lines.append("")
        return lines

    def _build_env(self, env: str) -> list[str]:
        p = self.per_env[env]
        default_port = PER_ENV_DEFAULTS["PROXY_LISTEN_PORT"][env]
        lines = [
            f"# TradeSynchronizer — {env.upper()} engine private settings.",
            f"# Auto-managed by the GUI's \"{env.capitalize()}\" tab — feel free to edit by hand.",
            "# Shared settings (proxy host, replication, logging) live in .env.",
            "",
            "# ── Tradovate (LEADER account) credentials ─────────────────────── #",
            "# TRADOVATE_USERNAME / TRADOVATE_PASSWORD / TRADOVATE_ACCOUNT_ID are",
            "# written here BY HAND — the GUI no longer manages them. The engine",
            "# reads them from this file at startup. ACCOUNT_ID may be the numeric",
            "# id OR the account name you see in Tradovate (e.g. DEMO3701228); the",
            "# engine resolves the name to the internal id via /account/list.",
            "# They are preserved verbatim across GUI Saves (see _build_env docs).",
            "",
            "# ── Proxy listen port ──────────────────────────────────────────── #",
            f"PROXY_LISTEN_PORT={p.get('PROXY_LISTEN_PORT', default_port)}",
        ]
        # Preserve any extra keys the user added manually to this env
        # file. This now INCLUDES the hand-written Tradovate credentials
        # (TRADOVATE_USERNAME / TRADOVATE_PASSWORD / TRADOVATE_ACCOUNT_ID)
        # since they were dropped from PER_ENV_KEYS and the canonical
        # emit list above — they round-trip through extras_per_env and
        # are re-emitted here unchanged, so a GUI Save never clobbers the
        # values the user typed by hand. Also covers per-env overrides
        # like TRADOVATE_CID/SEC/APP_ID/APP_VERSION/DEVICE_ID. Re-emitted
        # by sorted name for stable diffs.
        extras = self.extras_per_env.get(env, {})
        if extras:
            lines.append("")
            lines.append("# ── Per-env Tradovate / extras preserved ────────────────────────── #")
            for k in sorted(extras):
                lines.append(f"{k}={extras[k]}")
        lines.append("")
        return lines
