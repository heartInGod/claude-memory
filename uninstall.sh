#!/bin/bash
# AI Memory - Uninstall Script
# Removes hooks from settings.json and optionally deletes skill files
set -e

SKILL_DST="$HOME/.claude/skills/claude-memory"
SETTINGS="$HOME/.claude/settings.json"

echo "=== AI Memory Uninstaller ==="
echo ""

# 1. Remove hooks from settings.json
echo "[1/3] Removing hooks from $SETTINGS ..."
if [ -f "$SETTINGS" ]; then
    python3 - "$SETTINGS" << 'PYEOF'
import json
import sys

settings_path = sys.argv[1]

with open(settings_path, "r") as f:
    settings = json.load(f)

hooks = settings.get("hooks", {})
removed = 0

for event_type in ["SessionStart", "Stop"]:
    if event_type in hooks:
        original_len = len(hooks[event_type])
        hooks[event_type] = [
            h for h in hooks[event_type]
            if "claude-memory" not in json.dumps(h)
        ]
        removed += original_len - len(hooks[event_type])
        if not hooks[event_type]:
            del hooks[event_type]

if not hooks:
    settings.pop("hooks", None)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)

print(f"  Removed {removed} hook(s)")
PYEOF
else
    echo "  No settings.json found, skipping"
fi

# 2. Ask about data preservation
echo ""
echo "[2/3] Data files:"
if [ -f "$SKILL_DST/data/global_memory.json" ]; then
    G_COUNT=$(python3 -c "import json; d=json.load(open('$SKILL_DST/data/global_memory.json')); print(len(d.get('entries',[])))" 2>/dev/null || echo "?")
    echo "  global_memory.json: $G_COUNT entries"
fi
if [ -f "$SKILL_DST/data/deep_memory.json" ]; then
    D_COUNT=$(python3 -c "import json; d=json.load(open('$SKILL_DST/data/deep_memory.json')); print(len(d.get('entries',[])))" 2>/dev/null || echo "?")
    echo "  deep_memory.json: $D_COUNT entries"
fi

echo ""
read -p "Delete memory data as well? (y/N): " DELETE_DATA

# 3. Remove files
echo "[3/3] Removing skill files..."
if [ "$DELETE_DATA" = "y" ] || [ "$DELETE_DATA" = "Y" ]; then
    rm -rf "$SKILL_DST"
    echo "  Removed $SKILL_DST (including data)"
else
    # Keep data directory, remove everything else
    rm -f "$SKILL_DST/SKILL.md"
    rm -rf "$SKILL_DST/scripts"
    echo "  Removed skill files, preserved data in $SKILL_DST/data/"
fi

echo ""
echo "=== Uninstall complete ==="
echo "Restart Claude Code to apply changes."
