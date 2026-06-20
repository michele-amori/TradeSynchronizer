#!/usr/bin/env bash
# Build a thin macOS .app bundle that launches gui.py.
#
# No embedded Python, no code-signing, no notarisation — the bundle
# is just a shell wrapper that finds the project's virtualenv (or
# the system python3) and runs gui.py. Suitable for personal local
# use only.
#
# Output: ./TradeSynchronizer.app (overwrites any existing build).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="TradeSynchronizer"
APP_PATH="$PROJECT_ROOT/$APP_NAME.app"

echo "Building $APP_NAME.app at $APP_PATH"

# Wipe any previous build.
rm -rf "$APP_PATH"

# Create the bundle skeleton.
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

# ── Info.plist ──────────────────────────────────────────────────────── #
# LSRequiresNativeExecution=true is the critical bit on Apple Silicon:
# it tells launchd to NEVER start this app under Rosetta translation,
# regardless of any user "Open using Rosetta" checkbox in Get Info
# (the system disables that checkbox entirely when this key is true).
# Without it, an app like ours — universal-binary Python framework
# under a bash launcher — can end up running x86_64 + translated, and
# the engine subprocess then can't dlopen the arm64-only .so files
# that pip installed in .venv (cryptography / cffi / mitmproxy TLS).
# Symptom: ImportError "have arm64, need x86_64" the first time the
# engine does `from mitmproxy import http`.
cat > "$APP_PATH/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>local.tradesync</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>$APP_NAME</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSRequiresNativeExecution</key> <true/>
</dict>
</plist>
EOF

# ── Launcher script ─────────────────────────────────────────────────── #
# Note the trailing dollar-signs inside the heredoc are escaped (\$) so
# the project root is baked at build time but \$PY / \$PROJECT_ROOT are
# resolved at run time.
#
# Apple Silicon arch handling:
#
# On macOS Tahoe (26.x) Apple started actively warning the user
# about "Intel-Based Apps" when it observes a universal binary
# running its x86_64 slice — even when an arm64 slice is also
# available. In our setup that toast targets "Python", which is
# correct: the bash launcher below, when invoked by launchd via
# `open /Applications/<bundle>.app`, runs as x86_64 (i386 in
# `arch(1)` output) under Rosetta, and `exec "\$PY"` then inherits
# x86_64 — even though LSRequiresNativeExecution=true is set on
# this bundle. That key applies to a Mach-O CFBundleExecutable;
# for a shebang script like this one, launchd execs
# /usr/bin/env bash to interpret it and the arch choice falls back
# to launchd's session default, which on Tahoe is x86_64.
#
# An earlier attempt to wrap the Python invocation in `arch
# -arm64` was reverted (commit 6503cb0) because it had crashed
# with exit 126 — but that was a different shape:
# `exec arch -arm64 <shell-script>`. Here we invoke `arch -arm64
# <binary>`, which is the documented, supported shape. Verified
# on Tahoe (16h18m CEST 6-Jun): bash launcher reports arch i386,
# but the Python child correctly reports platform.machine() ==
# arm64 after the arch wrapper.
#
# The benefit chains: GUI Python in arm64 → tkinter / mitmproxy
# imports load arm64 dylibs → Tahoe stops flagging "Intel-Based
# Apps" → future macOS releases that drop Rosetta won't break us.
LAUNCHER="$APP_PATH/Contents/MacOS/$APP_NAME"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Auto-generated launcher — regenerate with build_app.sh.
PROJECT_ROOT="$PROJECT_ROOT"

if [ -x "\$PROJECT_ROOT/.venv/bin/python" ]; then
    PY="\$PROJECT_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="\$(command -v python3)"
else
    osascript -e 'display dialog "Python 3 not found. Install it or create a .venv inside the project." buttons {"OK"} default button "OK" with icon stop'
    exit 1
fi

cd "\$PROJECT_ROOT"
# On Apple Silicon force the GUI Python into the arm64 slice. The
# bash launcher itself is running under Rosetta (\`arch\` returns
# "i386") because launchd starts shebang scripts in its session
# default x86_64; the universal-binary Python child would inherit
# that without this wrapper and end up flagged by Tahoe's
# "Intel-Based Apps" warning. \`arch -arm64\` is robust here
# because the target is a Mach-O binary (Python), not another
# shell script — the earlier exit-126 issue (commit 6503cb0) only
# affected the script-wrapping-script shape.
if [ "\$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ]; then
    exec /usr/bin/arch -arm64 "\$PY" "\$PROJECT_ROOT/gui.py"
fi
exec "\$PY" "\$PROJECT_ROOT/gui.py"
EOF
chmod +x "$LAUNCHER"

# ── Optional icon ───────────────────────────────────────────────────── #
# If you drop an AppIcon.icns at the project root, the build will pick
# it up automatically.
if [ -f "$PROJECT_ROOT/AppIcon.icns" ]; then
    cp "$PROJECT_ROOT/AppIcon.icns" "$APP_PATH/Contents/Resources/AppIcon.icns"
    # Insert CFBundleIconFile before the closing </dict>. Done with
    # awk because sed -i is non-portable across macOS/Linux.
    awk '
        /<\/dict>/ && !done {
            print "    <key>CFBundleIconFile</key><string>AppIcon</string>"
            done=1
        }
        { print }
    ' "$APP_PATH/Contents/Info.plist" > "$APP_PATH/Contents/Info.plist.tmp"
    mv "$APP_PATH/Contents/Info.plist.tmp" "$APP_PATH/Contents/Info.plist"
fi

# ── Ad-hoc codesign ─────────────────────────────────────────────────── #
# Apply an ad-hoc signature so Gatekeeper recognises the bundle as
# locally-signed rather than "no usable signature". Without this,
# `spctl -a` outright rejects the bundle on macOS Sequoia+, and on
# macOS Tahoe (26) the user can be blocked from opening it even
# through right-click → Open without first granting an override in
# System Settings → Privacy & Security.
#
# Ad-hoc means the signature is self-attested ("- " as the identity
# argument): no Apple Developer cert involved, no notarization, no
# fee, no entitlements. It's enough to flip the bundle from
# "unsigned" to "adhoc-signed", which on a local install is the
# practical sweet spot — Gatekeeper still classes it as developer-
# unknown, but the user's manual approval sticks across launches
# instead of being re-prompted each time.
#
# `--deep` re-signs every nested bundle and binary too (we don't
# have any, but it's a no-op when there isn't one and a correctness
# fix the day we add a helper tool). `--force` overwrites any
# previous signature (idempotent rebuilds). `--timestamp=none` is
# explicit: ad-hoc signatures don't talk to Apple's TSA server, and
# leaving the default `--timestamp` set causes a network call that
# can fail silently behind some corporate proxies.
#
# Strip extended attributes from the freshly-built bundle FIRST.
# When the project lives in an iCloud-synced folder (e.g. ~/Documents),
# the file provider stamps com.apple.FinderInfo / com.apple.provenance /
# com.apple.fileprovider.* onto the files. codesign rejects those with
# "resource fork, Finder information, or similar detritus not allowed"
# and the bundle ends up unsigned. `xattr -cr` clears them so the sign
# below succeeds; harmless when there are no such attributes.
xattr -cr "$APP_PATH" 2>/dev/null || true
if codesign --sign - --deep --force --timestamp=none "$APP_PATH" 2>/dev/null; then
    echo "✓ Ad-hoc codesigned"
else
    echo "⚠ codesign --sign - failed; bundle remains unsigned." \
         "Gatekeeper may complain on first launch."
fi

# ── Strip quarantine, if any ───────────────────────────────────────── #
# If the bundle was previously downloaded / unzipped, macOS may have
# stamped it with com.apple.quarantine, which forces a Gatekeeper
# challenge on every launch even AFTER ad-hoc signing. Remove it
# now so the just-built bundle is immediately usable.
xattr -dr com.apple.quarantine "$APP_PATH" 2>/dev/null || true

echo "✓ Built $APP_PATH"
echo
echo "Double-click TradeSynchronizer.app, or drag it to /Applications."
echo "On first launch macOS may ask to confirm — right-click → Open."
