set -e

# default: current directory
TARGET="${@:1}"
if [ -z "$TARGET" ]; then
    TARGET="."
fi

# do the actual formatting
ruff format $TARGET
ruff check --select I --fix $TARGET # sort imports
