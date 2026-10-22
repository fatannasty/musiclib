#!/bin/bash
set -u

REPO_DIR="/Users/fatannasty/Documents/Daily/musiclib"
LOG_FILE="$HOME/Library/Logs/musiclib-autosync.log"
BRANCH="main"
LOCK_FILE="/tmp/musiclib-autosync.lock"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

if [ -e "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$pid" 2>/dev/null; then
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$REPO_DIR" || exit 1

if [ -z "$(git status --porcelain)" ]; then
    exit 0
fi

git add -A
if git diff --cached --quiet; then
    exit 0
fi

git commit -m "auto-sync: changes from $(hostname -s) at $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE" 2>&1

if ! git pull --rebase --autostash origin "$BRANCH" >> "$LOG_FILE" 2>&1; then
    log "pull/rebase failed; manual resolution needed"
    exit 1
fi

if git push origin "$BRANCH" >> "$LOG_FILE" 2>&1; then
    log "pushed to origin/$BRANCH"
else
    log "push failed (offline or auth issue); will retry next cycle"
fi
