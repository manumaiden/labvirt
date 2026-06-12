#!/bin/bash
# Installs vmbuilder as a symlink in ~/bin
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VMBUILDER_SRC="${SCRIPT_DIR}/vmbuilder.py"
BIN_DIR="${HOME}/bin"
SYMLINK="${BIN_DIR}/vmbuilder"

# ── Create ~/bin if missing ───────────────────────────────────────────────────
if [[ ! -d "$BIN_DIR" ]]; then
    mkdir -p "$BIN_DIR"
    echo "  Created: $BIN_DIR"
fi

# ── Create/update symlink ─────────────────────────────────────────────────────
ln -sf "$VMBUILDER_SRC" "$SYMLINK"
chmod +x "$VMBUILDER_SRC"
echo "  Symlink: $SYMLINK → $VMBUILDER_SRC"

# ── Ensure ~/bin is in PATH ───────────────────────────────────────────────────
PATH_LINE='export PATH="$HOME/bin:$PATH"'
ADDED_TO=""

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    # Not in current PATH — add to the right shell rc file
    for RC in "${HOME}/.bashrc" "${HOME}/.bash_profile" "${HOME}/.profile"; do
        if [[ -f "$RC" ]]; then
            if ! grep -qF 'HOME/bin' "$RC"; then
                echo "" >> "$RC"
                echo "# added by vmbuilder install" >> "$RC"
                echo "$PATH_LINE" >> "$RC"
                ADDED_TO="$RC"
            fi
            break
        fi
    done

    echo ""
    echo "  ~/bin was not in PATH."
    if [[ -n "$ADDED_TO" ]]; then
        echo "  Added to: $ADDED_TO"
        echo "  Run: source $ADDED_TO"
    else
        echo "  Add this line manually to your shell rc file:"
        echo "    $PATH_LINE"
    fi
else
    echo "  ~/bin already in PATH"
fi

# ── Copy default config to ~/.vmbuilder.conf if not already present ───────────
USER_CONF="${HOME}/.vmbuilder.conf"
if [[ ! -f "$USER_CONF" ]]; then
    cp "${SCRIPT_DIR}/configs/lab.conf" "$USER_CONF"
    echo "  Config:  $USER_CONF (copied from default)"
else
    echo "  Config:  $USER_CONF (already exists, not overwritten)"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  Installation complete."
echo "  Run: vmbuilder"
