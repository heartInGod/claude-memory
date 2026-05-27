#!/bin/bash
# AI Memory - Stop Hook
# Extracts knowledge from session transcript before exit
set -e

HOOK_INPUT=$(cat)
TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | jq -r '.transcript_path // ""')
SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""')

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
    exit 0
fi

# Run extraction in background to avoid blocking session exit
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py extract \
    --transcript "$TRANSCRIPT_PATH" \
    --session-id "$SESSION_ID" 2>/tmp/claude-memory-extract.log &

exit 0
