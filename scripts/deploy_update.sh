#!/usr/bin/env bash
# Guarded in-place deployment for the single-instance systemd installation.
# Run as the shopbot service user from the deployed repository.
set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-$HOME/shorts-chill-shop-bot}"
SERVICE_NAME="${SERVICE_NAME:-shopbot}"
REMOTE="${DEPLOY_REMOTE:-origin}"
DEPLOY_REF="${DEPLOY_REF:-main}"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"
PIP="${PIP:-$REPO_DIR/.venv/bin/pip}"
BACKUP_OUTPUT=""
SYSTEMCTL="/usr/bin/systemctl"
ROOT_SYSTEMCTL() { sudo -n "$SYSTEMCTL" "$@"; }

log() { printf '[deploy] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

[[ -d "$REPO_DIR/.git" ]] || die "not a Git checkout: $REPO_DIR"
cd "$REPO_DIR"

CURRENT_BRANCH="$(git branch --show-current)"
[[ "$CURRENT_BRANCH" == "$DEPLOY_REF" ]] || die "checked-out branch '$CURRENT_BRANCH' differs from DEPLOY_REF '$DEPLOY_REF'"
[[ -x "$PYTHON" && -x "$PIP" ]] || die "venv executables not found; expected $PYTHON and $PIP"
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || die "working tree is not clean; refusing to overwrite local changes"
sudo -n "$SYSTEMCTL" is-active --quiet "$SERVICE_NAME" || die "service is not active or shopbot lacks passwordless systemctl permission"
[[ ! -e .git/MERGE_HEAD && ! -e .git/rebase-merge && ! -e .git/rebase-apply ]] || die "Git merge/rebase is in progress"

log "Fetching $REMOTE/$DEPLOY_REF"
git fetch --prune "$REMOTE" "+refs/heads/$DEPLOY_REF:refs/remotes/$REMOTE/$DEPLOY_REF"
TARGET="$(git rev-parse "$REMOTE/$DEPLOY_REF")"
PREVIOUS="$(git rev-parse HEAD)"
git merge-base --is-ancestor "$PREVIOUS" "$TARGET" || die "target is not a fast-forward; manual review required"
[[ "$TARGET" != "$PREVIOUS" ]] || { log "Already up to date ($PREVIOUS)"; exit 0; }

log "Creating and verifying a SQLite backup before code changes"
BACKUP_OUTPUT="$("$PYTHON" -m scripts.database_maintenance backup)"
printf '%s\n' "$BACKUP_OUTPUT"
grep -q '^BACKUP_OK:' <<<"$BACKUP_OUTPUT" || die "database backup did not report BACKUP_OK"

ROLLBACK_NEEDED=0
rollback_on_error() {
    local status=$?
    trap - ERR
    if (( ROLLBACK_NEEDED )); then
        log "Deployment failed (exit $status); rolling code back to $PREVIOUS"
        if git reset --hard "$PREVIOUS" && "$PIP" install -r requirements.txt && ROOT_SYSTEMCTL restart "$SERVICE_NAME" && ROOT_SYSTEMCTL is-active --quiet "$SERVICE_NAME"; then
            log "Code rollback succeeded. Database was left untouched; backup is available above."
        else
            log "CRITICAL: automatic rollback could not be verified. Inspect service and restore manually if required."
        fi
    fi
    exit "$status"
}
trap rollback_on_error ERR

ROLLBACK_NEEDED=1
log "Fast-forwarding code to $TARGET"
git merge --ff-only "$TARGET"
log "Installing pinned runtime dependencies"
"$PIP" install -r requirements.txt -r requirements-dev.txt
log "Compiling application modules"
"$PYTHON" -m compileall -q bot config curation database handlers keyboards services scripts
log "Running focused deployment safety tests"
"$PYTHON" -m pytest -q tests/test_database_maintenance.py tests/test_affiliate_orders.py tests/test_affiliate_link_analytics.py tests/test_sales_calendar.py tests/test_admin_panel.py
log "Restarting systemd service: $SERVICE_NAME"
ROOT_SYSTEMCTL restart "$SERVICE_NAME"
ROOT_SYSTEMCTL is-active --quiet "$SERVICE_NAME"
ROLLBACK_NEEDED=0
trap - ERR
log "DEPLOY_OK: $PREVIOUS -> $TARGET"
log "DATABASE_BACKUP: $(sed -n 's/^BACKUP_OK: //p' <<<"$BACKUP_OUTPUT" | tail -n 1)"
