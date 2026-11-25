#!/bin/bash
# Kept for compatibility. The installer now lives in macos/install-login-item.sh.
#   ./install.sh ~/Projects        install (same as: macos/install-login-item.sh install ~/Projects)
#   ./install.sh --uninstall       remove
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "${1:-}" = "--uninstall" ]; then
  exec "$DIR/macos/install-login-item.sh" uninstall
fi
exec "$DIR/macos/install-login-item.sh" install "$@"
