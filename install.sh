#!/bin/bash
# AI Memory - Install Script
# Installs the claude-memory skill to ~/.claude/skills/ and configures hooks in settings.json
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$SCRIPT_DIR/skill"
SKILL_DST="$HOME/.claude/skills/claude-memory"
SETTINGS="$HOME/.claude/settings.json"

echo "=== AI Memory Installer ==="
echo ""

# 1. Check dependencies
echo "[1/5] Checking dependencies..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required but not found."
    exit 1
fi
if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not found. Install with: sudo apt-get install jq"
    exit 1
fi
echo "  python3: $(python3 --version 2>&1)"
echo "  jq: $(jq --version 2>&1)"

# 2. Copy skill files
echo "[2/5] Installing skill to $SKILL_DST ..."
mkdir -p "$SKILL_DST/scripts"
cp "$SKILL_SRC/SKILL.md" "$SKILL_DST/"
cp "$SKILL_SRC/scripts/memory_manager.py" "$SKILL_DST/scripts/"
cp "$SKILL_SRC/scripts/session_start.sh" "$SKILL_DST/scripts/"
cp "$SKILL_SRC/scripts/session_stop.sh" "$SKILL_DST/scripts/"
chmod +x "$SKILL_DST/scripts/"*.sh

# 3. Initialize data files (preserve existing data on reinstall)
echo "[3/5] Initializing data directory..."
mkdir -p "$SKILL_DST/data"
if [ ! -f "$SKILL_DST/data/global_memory.json" ]; then
    echo '{"version": 1, "entries": []}' > "$SKILL_DST/data/global_memory.json"
    echo "  Created global_memory.json"
else
    echo "  global_memory.json already exists, preserving data"
fi
if [ ! -f "$SKILL_DST/data/deep_memory.json" ]; then
    echo '{"version": 1, "entries": []}' > "$SKILL_DST/data/deep_memory.json"
    echo "  Created deep_memory.json"
else
    echo "  deep_memory.json already exists, preserving data"
fi

# 4. Configure hooks in settings.json
echo "[4/5] Configuring hooks in $SETTINGS ..."
if [ ! -f "$SETTINGS" ]; then
    echo '{}' > "$SETTINGS"
fi

python3 - "$SETTINGS" << 'PYEOF'
import json
import sys

settings_path = sys.argv[1]

try:
    with open(settings_path, "r") as f:
        settings = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    settings = {}

hooks = settings.setdefault("hooks", {})

session_start_cmd = "bash ~/.claude/skills/claude-memory/scripts/session_start.sh"
ss_hooks = hooks.setdefault("SessionStart", [])
if not any(session_start_cmd in json.dumps(h) for h in ss_hooks):
    ss_hooks.append({
        "hooks": [{
            "type": "command",
            "command": session_start_cmd,
            "timeout": 30
        }]
    })
    print("  Added SessionStart hook")
else:
    print("  SessionStart hook already configured")

stop_cmd = "bash ~/.claude/skills/claude-memory/scripts/session_stop.sh"
stop_hooks = hooks.setdefault("Stop", [])
if not any(stop_cmd in json.dumps(h) for h in stop_hooks):
    stop_hooks.append({
        "hooks": [{
            "type": "command",
            "command": stop_cmd,
            "timeout": 120
        }]
    })
    print("  Added Stop hook")
else:
    print("  Stop hook already configured")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2, ensure_ascii=False)
PYEOF

# 5. Verify
echo "[5/5] Verifying installation..."
python3 "$SKILL_DST/scripts/memory_manager.py" stats
echo ""
echo "=== Installation complete! ==="
echo ""
echo "Hooks configured:"
echo "  SessionStart → loads memory context into new sessions"
echo "  Stop         → extracts knowledge when sessions end"
echo ""
echo "Data files:"
echo "  $SKILL_DST/data/global_memory.json  (active memory)"
echo "  $SKILL_DST/data/deep_memory.json    (forgotten archive)"
echo ""
echo "Restart Claude Code to activate the hooks."
