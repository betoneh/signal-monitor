#!/bin/bash
# Signal Monitor Watcher
# Polls the GitHub repo for trigger.json or a pending kb_ingest job.

set -euo pipefail

REPO_DIR="/Users/maxumbra/clawd/projects/signal-monitor-standalone/repos/signal-monitor"
STANDALONE_DIR="/Users/maxumbra/clawd/projects/signal-monitor-standalone"
TRIGGER_FILE="$REPO_DIR/trigger.json"
SETTINGS_FILE="$REPO_DIR/settings.json"
PROCESSOR="$STANDALONE_DIR/scripts/process_kb_ingest.py"
MODEL_PROCESSOR="$STANDALONE_DIR/scripts/process_model_onboarding.py"
LOG="$STANDALONE_DIR/data/watcher.log"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

read_kb_field() {
    local section="$1"
    local field="$2"
    python3 - "$SETTINGS_FILE" "$section" "$field" <<'PY'
import json
import sys
from pathlib import Path

settings = Path(sys.argv[1])
section = sys.argv[2]
field = sys.argv[3]
data = json.loads(settings.read_text()) if settings.exists() else {}
kb = data.get(section, {})
value = kb.get(field, "")
if isinstance(value, str):
    print(value)
else:
    print(value if value is not None else "")
PY
}

clear_kb_ingest() {
    python3 - "$SETTINGS_FILE" <<'PY'
import json
import sys
from pathlib import Path

settings = Path(sys.argv[1])
data = json.loads(settings.read_text()) if settings.exists() else {}
model = data.get("kb_ingest", {}).get("model") or data.get("model") or "anthropic/claude-opus-4-6"
data["kb_ingest"] = {"url": "", "deep_dives_count": 0, "model": model}
settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY
}

clear_kb_existing_dd() {
    python3 - "$SETTINGS_FILE" <<'PY'
import json
import sys
from pathlib import Path

settings = Path(sys.argv[1])
data = json.loads(settings.read_text()) if settings.exists() else {}
model = data.get("kb_existing_dd", {}).get("model") or data.get("model") or "anthropic/claude-opus-4-6"
data["kb_existing_dd"] = {"entry": "", "deep_dives_count": 0, "model": model}
settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
PY
}

push_repo_change() {
    local message="$1"
    cd "$REPO_DIR"
    git add settings.json trigger.json 2>/dev/null || true
    git commit -m "$message" --quiet 2>/dev/null || true
    git push --quiet 2>/dev/null || true
}

cd "$REPO_DIR"
git checkout -- . 2>/dev/null || true
git clean -fd --quiet 2>/dev/null || true
git pull --rebase --quiet 2>/dev/null || true

KB_URL=""
KB_DD="0"
KB_MODEL=""
KB_EXISTING_ENTRY=""
KB_EXISTING_DD="0"
KB_EXISTING_MODEL=""
MODEL_REQUEST=""
if [ -f "$SETTINGS_FILE" ]; then
    KB_URL="$(read_kb_field kb_ingest url)"
    KB_DD="$(read_kb_field kb_ingest deep_dives_count)"
    KB_MODEL="$(read_kb_field kb_ingest model)"
    KB_EXISTING_ENTRY="$(read_kb_field kb_existing_dd entry)"
    KB_EXISTING_DD="$(read_kb_field kb_existing_dd deep_dives_count)"
    KB_EXISTING_MODEL="$(read_kb_field kb_existing_dd model)"
    MODEL_REQUEST="$(read_kb_field model_onboarding request)"
fi

if [ -n "$MODEL_REQUEST" ]; then
    log "Model onboarding found: $MODEL_REQUEST"
    if /opt/homebrew/bin/python3.13 "$MODEL_PROCESSOR" --request "$MODEL_REQUEST" >> "$LOG" 2>&1; then
        push_repo_change "Process model_onboarding"
        log "Model onboarding processed."
    else
        log "Model onboarding failed; leaving request pending."
    fi
fi

if [ -n "$KB_URL" ]; then
    log "KB ingest found: $KB_URL (dd=$KB_DD, model=$KB_MODEL)"
    if /opt/homebrew/bin/python3.13 "$PROCESSOR" --url "$KB_URL" --deep-dives "${KB_DD:-0}" --model "${KB_MODEL:-anthropic/claude-opus-4-6}" >> "$LOG" 2>&1; then
        clear_kb_ingest
        push_repo_change "Clear kb_ingest"
        log "KB ingest completed."
    else
        log "KB ingest failed; leaving kb_ingest pending."
    fi
fi

if [ -n "$KB_EXISTING_ENTRY" ]; then
    log "KB existing_dd found: $KB_EXISTING_ENTRY (dd=$KB_EXISTING_DD, model=$KB_EXISTING_MODEL)"
    if /opt/homebrew/bin/python3.13 "$PROCESSOR" --kb-entry "$KB_EXISTING_ENTRY" --deep-dives "${KB_EXISTING_DD:-0}" --model "${KB_EXISTING_MODEL:-anthropic/claude-opus-4-6}" >> "$LOG" 2>&1; then
        clear_kb_existing_dd
        push_repo_change "Clear kb_existing_dd"
        log "KB existing_dd completed."
    else
        log "KB existing_dd failed; leaving request pending."
    fi
fi

if [ ! -f "$TRIGGER_FILE" ]; then
    log "No trigger found, exiting."
    exit 0
fi

log "Trigger found: $(cat "$TRIGGER_FILE")"

rm -f "$TRIGGER_FILE"
push_repo_change "Consumed trigger.json"

log "Launching standalone pipeline..."

FEED_FLAG=""
HOURS=24
MAX_TWEETS=180
if [ -f "$SETTINGS_FILE" ]; then
    WATCHER_FEED=$(python3 -c "import json; s=json.load(open('$SETTINGS_FILE')); print(s.get('runs',{}).get('watcher',{}).get('feed','Following'))" 2>/dev/null || echo "Following")
    HOURS=$(python3 -c "import json; s=json.load(open('$SETTINGS_FILE')); print(s.get('runs',{}).get('watcher',{}).get('hours',24))" 2>/dev/null || echo 24)
    MAX_TWEETS=$(python3 -c "import json; s=json.load(open('$SETTINGS_FILE')); print(s.get('runs',{}).get('watcher',{}).get('max',180))" 2>/dev/null || echo 180)
    if [ "$WATCHER_FEED" = "Following" ]; then
        FEED_FLAG="--following"
    fi
    log "Feed: $WATCHER_FEED, Hours: $HOURS, Max: $MAX_TWEETS"
else
    FEED_FLAG="--following"
    log "No settings.json found, using defaults (Following, 24h, 180 tweets)"
fi

export SM_FEED_FLAG="$FEED_FLAG"
export SM_HOURS="$HOURS"
export SM_MAX="$MAX_TWEETS"

/bin/bash "$STANDALONE_DIR/scripts/run_pipeline.sh" watcher

log "Watcher done."
