#!/bin/bash

# A script to back up a local Django application.
# The backup is compressed with gzip by default.
# Set the script to exit immediately if a command exits with a non-zero status.
set -e

# --- Configuration ---

# The directory where backups will be stored.
BACKUP_DIR="backups"

# --- Main Script ---

# Navigate to the script's directory. This is crucial for finding manage.py.
# cd "$(dirname "$0")"

echo "--- Starting Django Application Backup ---"
echo "  - Backup Target: Local python3 manage.py"
echo "  - Output Directory: ${BACKUP_DIR}/"

# Create the backup directory if it doesn't exist.
echo ""
echo "Ensuring backup directory '${BACKUP_DIR}' exists..."
mkdir -p "${BACKUP_DIR}"

# Generate a timestamp for the backup file.
TIMESTAMP=$(date +%F_%H-%M-%S) # Format: YYYY-MM-DD_HH-MM-SS
APP_BACKUP_FILE="${BACKUP_DIR}/${TIMESTAMP}.json.gz"

echo ""
echo "Backing up Django application data..."

# Execute the dumpdata command locally.
# The output is piped directly to gzip for compression.
# Note: Your Django settings must be configured to connect to the database.
python3 manage.py dumpdata --natural-foreign --natural-primary --exclude contenttypes --exclude auth.permission | gzip > "${APP_BACKUP_FILE}"

echo ""
echo "--- ✅ Django Backup Complete! ---"
echo "   File created at: ${APP_BACKUP_FILE}"