#!/bin/bash
# Claude Memory - Upgrade Script
# Updates skill files and migrates data to latest format (v2 tree-structured paths)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$SCRIPT_DIR/skill"
SKILL_DST="$HOME/.claude/skills/claude-memory"

echo "=== Claude Memory Upgrade ==="
echo ""

# 1. Check prerequisites
echo "[1/4] Checking prerequisites..."
if [ ! -d "$SKILL_DST" ]; then
    echo "ERROR: claude-memory not installed. Run install.sh first."
    exit 1
fi
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 is required."
    exit 1
fi

# 2. Update skill files (preserve data/)
echo "[2/4] Updating skill files..."
cp "$SKILL_SRC/SKILL.md" "$SKILL_DST/"
cp "$SKILL_SRC/scripts/memory_manager.py" "$SKILL_DST/scripts/"
cp "$SKILL_SRC/scripts/session_start.sh" "$SKILL_DST/scripts/"
cp "$SKILL_SRC/scripts/session_stop.sh" "$SKILL_DST/scripts/"
chmod +x "$SKILL_DST/scripts/"*.sh
echo "  Scripts updated"

# 3. Migrate global_memory to v2 tree structure (if needed)
echo "[3/4] Checking data format..."
GLOBAL_MEM="$SKILL_DST/data/global_memory.json"

if [ -f "$GLOBAL_MEM" ]; then
    VERSION=$(python3 -c "import json; d=json.load(open('$GLOBAL_MEM')); print(d.get('version', 1))" 2>/dev/null || echo "1")
    ENTRY_COUNT=$(python3 -c "import json; d=json.load(open('$GLOBAL_MEM')); print(len(d.get('entries',[])))" 2>/dev/null || echo "0")

    if [ "$VERSION" = "1" ] && [ "$ENTRY_COUNT" -gt "0" ]; then
        echo "  global_memory.json is v1 format ($ENTRY_COUNT entries), migrating to v2..."
        echo "  This calls Sonnet API to consolidate entries into tree-structured paths."
        echo "  Backup will be saved as global_memory.json.bak"
        echo ""
        python3 "$SKILL_DST/scripts/memory_manager.py" consolidate
        echo ""
        echo "  Migration complete."
    elif [ "$VERSION" = "2" ]; then
        echo "  global_memory.json already v2 ($ENTRY_COUNT entries), no migration needed."
    else
        echo "  global_memory.json is empty, skipping migration."
    fi
else
    echo "  No global_memory.json found, creating empty v2 file."
    echo '{"version": 2, "entries": []}' > "$GLOBAL_MEM"
fi

# 4. Reset deep_memory (regenerated from new forgetting cycle)
echo "[4/4] Resetting deep_memory..."
DEEP_MEM="$SKILL_DST/data/deep_memory.json"
echo '{"version": 2, "entries": []}' > "$DEEP_MEM"
echo "  deep_memory.json reset"

# Done
echo ""
echo "=== Upgrade complete! ==="
python3 "$SKILL_DST/scripts/memory_manager.py" stats
echo ""
echo "Restart Claude Code to apply changes."
