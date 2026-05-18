#!/usr/bin/env bash
set -euo pipefail

cd "$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"

# Get already-registered submodule paths
existing=$(git config --file .gitmodules --get-regexp path 2>/dev/null | awk '{print $2}' || true)

for dir in */; do
    dir="${dir%/}"

    # Skip if not a git repo
    [ -d "$dir/.git" ] || continue

    # Skip if already a submodule
    if echo "$existing" | grep -qx "$dir"; then
        echo "skip: $dir (already a submodule)"
        continue
    fi

    # Get the remote URL
    url=$(git -C "$dir" remote get-url origin 2>/dev/null || true)
    if [ -z "$url" ]; then
        echo "skip: $dir (no origin remote)"
        continue
    fi

    echo "adding: $dir -> $url"
    git submodule add "$url" "$dir"
done

echo "done"
