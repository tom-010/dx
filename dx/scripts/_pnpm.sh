#!/usr/bin/env bash
# Sourced by scripts that need pnpm: it is installed via nvm, which only loads in interactive shells.
if ! command -v pnpm >/dev/null 2>&1; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
fi
