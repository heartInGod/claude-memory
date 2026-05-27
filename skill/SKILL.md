---
name: claude-memory
description: "Persistent AI memory system. Auto-extracts knowledge from sessions, manages memory lifecycle with forgetting/reactivation. Use when: viewing memory stats, searching memories, recalling forgotten knowledge, or manually managing memory entries."
metadata:
---

# AI Memory Skill

Persistent memory system that automatically learns from Claude Code sessions.

## Automatic Behavior (via hooks)

- **Session Start**: Loads all active memories as context
- **Session End**: Extracts key knowledge/skills from the conversation, merges with existing memory, reactivates forgotten entries if relevant

## Manual Commands

Run these via bash when you need direct memory management:

### View stats
```bash
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py stats
```

### Search memories (includes forgotten ones)
```bash
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py recall --query "keyword"
```

### Force memory cleanup
```bash
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py forget
```

### Reactivate a forgotten memory
```bash
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py reactivate --id "mem_xxx"
```

## Memory Lifecycle

```
New Session → Extract Knowledge → Merge/Deduplicate → Store
                                       ↓
                              Similar in deep_memory? → Reactivate
                                       ↓
                              Size > 500KB? → Forget lowest-score entries
                                       ↓
                              deep_memory.json (archived with full metadata)
```

## Scoring Formula

`score = access_count × 0.6 + recency × 0.4`

- Frequently accessed memories persist longer
- Recent memories get a recency boost (decays over 180 days)
- When size exceeds 500KB, lowest-score entries are forgotten

## Data Files

- `~/.claude/skills/claude-memory/data/global_memory.json` — Active memories
- `~/.claude/skills/claude-memory/data/deep_memory.json` — Forgotten archive (searchable, reactivatable)
