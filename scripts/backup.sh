#!/usr/bin/env bash
# scripts/backup.sh
# Automated PostgreSQL backup script for Incident Copilot
# Dumps active database, compresses with gzip, retains newest 7 copies,
# and optionally synchronizes to an off-server rclone remote.
#
# Usage:
#   ./scripts/backup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_DIR="${PROJECT_ROOT}/backups"
ENV_FILE="${PROJECT_ROOT}/.env"

# ── 1. Validate Environment & Read Credentials Without Echoing ─────────────
if [ ! -f "${ENV_FILE}" ]; then
    echo "ERROR: .env file not found at ${ENV_FILE}" >&2
    exit 1
fi

# Extract POSTGRES_USER and POSTGRES_DB silently from .env (ignoring comments/empty lines)
POSTGRES_USER="$(grep -E '^POSTGRES_USER=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"'"'"' || true)"
POSTGRES_DB="$(grep -E '^POSTGRES_DB=' "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"'"'"' || true)"

if [ -z "${POSTGRES_USER}" ] || [ -z "${POSTGRES_DB}" ]; then
    echo "ERROR: Failed to extract POSTGRES_USER or POSTGRES_DB from .env" >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"

# ── 2. Execute pg_dump Into Timestamped Gzip Archive ───────────────────────
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILENAME="incident-copilot-${TIMESTAMP}.sql.gz"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

echo "--> Initiating PostgreSQL backup at ${TIMESTAMP}..."
cd "${PROJECT_ROOT}"

# Run pg_dump through docker compose exec -T without exposing passwords on CLI
docker compose exec -T postgres pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --clean --if-exists | gzip > "${BACKUP_PATH}"

# Verify backup was created and is non-empty
if [ ! -s "${BACKUP_PATH}" ]; then
    echo "ERROR: Backup file ${BACKUP_PATH} was created but is empty." >&2
    rm -f "${BACKUP_PATH}"
    exit 1
fi

BACKUP_SIZE="$(du -h "${BACKUP_PATH}" | cut -f1)"
echo "--> Backup successfully generated: ${BACKUP_PATH} (${BACKUP_SIZE})"

# ── 3. Rotate Local Backups (Retain Newest 7 Files) ─────────────────────────
echo "--> Rotating local backups (retaining newest 7 archives)..."
# List all backup archives sorted by modification time descending, skip first 7, delete remaining
find "${BACKUP_DIR}" -maxdepth 1 -name "incident-copilot-*.sql.gz" -type f -printf '%T@ %p\n' \
    | sort -rn \
    | cut -d' ' -f2- \
    | tail -n +8 \
    | while IFS= read -r old_backup; do
        if [ -n "${old_backup}" ] && [ -f "${old_backup}" ]; then
            echo "    Pruning old backup: ${old_backup}"
            rm -f "${old_backup}"
        fi
    done

# ── 4. Optional Off-Server Sync via rclone ─────────────────────────────────
if [ -n "${RCLONE_REMOTE:-}" ]; then
    echo "--> Syncing backup archive to off-server remote: ${RCLONE_REMOTE}..."
    if command -v rclone >/dev/null 2>&1; then
        if rclone copy "${BACKUP_PATH}" "${RCLONE_REMOTE}/"; then
            echo "--> Off-server copy completed successfully."
        else
            echo "WARNING: rclone copy to ${RCLONE_REMOTE} failed with exit code $?." >&2
        fi
    else
        echo "WARNING: RCLONE_REMOTE is set but 'rclone' binary is not installed on host." >&2
    fi
else
    echo "======================================================================"
    echo " WARNING: RCLONE_REMOTE is not set! Backups exist ONLY on this server. "
    echo " Configure off-site storage to protect against host or disk failure.  "
    echo "======================================================================"
fi
