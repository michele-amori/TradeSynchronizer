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
</dict>
</plist>
EOF

# ── Launcher script ─────────────────────────────────────────────────── #
# Note the trailing dollar-signs inside the heredoc are escaped (\$) so
# the project root is baked at build time but \$PY / \$PROJECT_ROOT are
# resolved at run time.
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

echo "✓ Built $APP_PATH"
echo
echo "Double-click TradeSynchronizer.app, or drag it to /Applications."
echo "On first launch macOS may ask to confirm — right-click → Open."
