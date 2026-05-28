#!/usr/bin/env python3
"""
AI Memory Manager for Claude Code
Tree-structured memory: entries organized by hierarchical path (e.g. "flink/deploy/jar-mapping").
Pure stdlib — no third-party dependencies.
"""

import json
import os
import sys
import argparse
import hashlib
import re
import shutil
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
GLOBAL_MEMORY_FILE = DATA_DIR / "global_memory.json"
DEEP_MEMORY_FILE = DATA_DIR / "deep_memory.json"

MAX_MEMORY_BYTES = 500 * 1024
TARGET_MEMORY_BYTES = 450 * 1024
MAX_TRANSCRIPT_CHARS = 80000


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gen_id():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    h = hashlib.md5(now_iso().encode() + os.urandom(4)).hexdigest()[:4]
    return f"mem_{ts}_{h}"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {"version": 2, "entries": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_json_size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


# ─── Similarity (path-based) ────────────────────────────────

def tokenize(text: str) -> set:
    return set(re.findall(r"[\w一-鿿]+", text.lower()))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def entry_path(entry: dict) -> str:
    return entry.get("path", "") or entry.get("title", "")


def is_similar(entry_a: dict, entry_b: dict) -> bool:
    pa = entry_path(entry_a)
    pb = entry_path(entry_b)
    if pa and pb and "/" in pa and "/" in pb:
        if pa == pb:
            return True
        parts_a = pa.split("/")
        parts_b = pb.split("/")
        common = sum(1 for a, b in zip(parts_a, parts_b) if a == b)
        return common >= 2
    # v1 fallback for entries without path
    ta = entry_a.get("tags", [])
    tb = entry_b.get("tags", [])
    if ta and tb:
        sa = set(t.lower() for t in ta)
        sb = set(t.lower() for t in tb)
        if sa and sb and len(sa & sb) / len(sa | sb) >= 0.6:
            return True
    title_sim = jaccard(tokenize(pa), tokenize(pb))
    return title_sim >= 0.5


def merge_entries(existing: dict, new_entry: dict) -> dict:
    existing["access_count"] = existing.get("access_count", 1) + 1
    existing["last_accessed"] = now_iso()

    ec = existing.get("content", "")
    nc = new_entry.get("content", "")
    if len(nc) > len(ec):
        existing["content"] = nc

    imp_order = {"high": 3, "medium": 2, "low": 1}
    ei = imp_order.get(existing.get("importance", "low"), 1)
    ni = imp_order.get(new_entry.get("importance", "low"), 1)
    if ni > ei:
        existing["importance"] = new_entry["importance"]

    if existing.get("created_at", "") > new_entry.get("created_at", ""):
        existing["created_at"] = new_entry["created_at"]

    return existing


# ─── API ─────────────────────────────────────────────────────

def call_sonnet(prompt: str, system: str = "", max_tokens: int = 4096) -> str:
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    model = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy", "")
    custom_headers_str = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")

    url = f"{base_url}/v1/messages"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    if custom_headers_str:
        for part in custom_headers_str.split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                headers[k.strip()] = v.strip()

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    if proxy:
        handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        opener = urllib.request.build_opener(handler, urllib.request.HTTPSHandler(context=ctx))
    else:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

    try:
        with opener.open(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"[claude-memory] API error {e.code}: {err_body}", file=sys.stderr)
    except Exception as e:
        print(f"[claude-memory] API call failed: {e}", file=sys.stderr)
    return ""


# ─── Extract ─────────────────────────────────────────────────

def read_transcript(path: str) -> str:
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                role = obj.get("type", "")
                if role not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content_raw = msg.get("content", "")
                if isinstance(content_raw, list):
                    text_parts = []
                    for block in content_raw:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            text_parts.append(block)
                    content = "\n".join(text_parts)
                elif isinstance(content_raw, str):
                    content = content_raw
                else:
                    continue
                if content.strip():
                    lines.append(f"[{role}]: {content[:2000]}")
            except json.JSONDecodeError:
                continue

    full = "\n".join(lines)
    if len(full) > MAX_TRANSCRIPT_CHARS:
        full = full[-MAX_TRANSCRIPT_CHARS:]
    return full


EXTRACT_PROMPT = """You are analyzing a Claude Code session transcript. Extract the most important and reusable knowledge.

Output ONLY a JSON array. Each object must have:
- "path": hierarchical path, 2~5 levels, lowercase english, "/" separated, each level 1~2 words
  Examples: "flink/deploy/jar-mapping", "github/ssh", "canoe/volume", "python/async/timeout"
- "content": core knowledge, concise but complete enough to be useful later
- "importance": "high", "medium", or "low"

Rules:
- Only extract genuinely useful, non-obvious information
- Skip trivial exchanges, greetings, and routine operations
- Merge related info into single entries rather than many tiny ones
- Content should be self-contained
- If nothing worth saving, return an empty array: []
- Prefer merging into existing paths when the knowledge overlaps

{existing_paths_section}

Transcript:
---
{transcript}
---"""


def cmd_extract(args):
    transcript_text = read_transcript(args.transcript)
    if not transcript_text.strip():
        print("[claude-memory] Empty transcript, nothing to extract.", file=sys.stderr)
        return

    global_mem = load_json(GLOBAL_MEMORY_FILE)
    existing_paths = sorted(set(
        entry_path(e) for e in global_mem.get("entries", []) if entry_path(e)
    ))

    if existing_paths:
        paths_text = "Existing paths in memory (merge into these if the knowledge overlaps, or create new paths):\n"
        paths_text += "\n".join(f"- {p}" for p in existing_paths[:100])
    else:
        paths_text = ""

    prompt = EXTRACT_PROMPT.format(
        transcript=transcript_text,
        existing_paths_section=paths_text,
    )
    raw = call_sonnet(prompt, system="You extract structured knowledge from conversations. Output only valid JSON.")

    if not raw.strip():
        print("[claude-memory] No response from API.", file=sys.stderr)
        return

    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            new_entries_raw = json.loads(match.group())
        else:
            print("[claude-memory] Could not parse JSON from response.", file=sys.stderr)
            return
    except json.JSONDecodeError as e:
        print(f"[claude-memory] JSON parse error: {e}", file=sys.stderr)
        return

    if not new_entries_raw:
        print("[claude-memory] No entries extracted.", file=sys.stderr)
        return

    deep_mem = load_json(DEEP_MEMORY_FILE)
    now = now_iso()
    session_id = getattr(args, "session_id", "") or ""

    added = 0
    merged_global = 0
    reactivated = 0

    for raw_entry in new_entries_raw:
        new_entry = {
            "id": gen_id(),
            "path": raw_entry.get("path", "misc/unknown"),
            "content": raw_entry.get("content", ""),
            "created_at": now,
            "last_accessed": now,
            "access_count": 1,
            "importance": raw_entry.get("importance", "medium"),
        }

        # exact path match first
        merged = False
        for existing in global_mem["entries"]:
            if entry_path(existing) == new_entry["path"]:
                merge_entries(existing, new_entry)
                merged = True
                merged_global += 1
                break

        # prefix match fallback
        if not merged:
            for existing in global_mem["entries"]:
                if is_similar(existing, new_entry):
                    merge_entries(existing, new_entry)
                    merged = True
                    merged_global += 1
                    break

        # check deep_memory for reactivation
        to_reactivate = []
        for i, deep_entry in enumerate(deep_mem["entries"]):
            if is_similar(deep_entry, new_entry):
                to_reactivate.append(i)

        for idx in reversed(to_reactivate):
            reactivated_entry = deep_mem["entries"].pop(idx)
            reactivated_entry["forgotten_at"] = None
            reactivated_entry["last_accessed"] = now
            reactivated_entry["access_count"] = reactivated_entry.get("access_count", 1) + 1
            if not merged:
                merge_entries(reactivated_entry, new_entry)
                global_mem["entries"].append(reactivated_entry)
                merged = True
            else:
                global_mem["entries"].append(reactivated_entry)
            reactivated += 1

        if not merged:
            global_mem["entries"].append(new_entry)
            added += 1

    forgotten = do_forget(global_mem, deep_mem)

    save_json(GLOBAL_MEMORY_FILE, global_mem)
    save_json(DEEP_MEMORY_FILE, deep_mem)

    size_kb = get_json_size(global_mem) / 1024
    print(
        f"[claude-memory] Extract done: +{added} new, ~{merged_global} merged, "
        f"↑{reactivated} reactivated, -{forgotten} forgotten. "
        f"Global: {len(global_mem['entries'])} entries ({size_kb:.1f}KB)",
        file=sys.stderr,
    )


# ─── Forget ──────────────────────────────────────────────────

def compute_score(entry: dict) -> float:
    access_count = entry.get("access_count", 1)
    last_accessed = entry.get("last_accessed", "")
    try:
        la = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
        days_ago = (datetime.now(timezone.utc) - la).total_seconds() / 86400
    except (ValueError, TypeError):
        days_ago = 365
    recency = max(0, 1.0 - days_ago / 180)
    return access_count * 0.6 + recency * 0.4


def do_forget(global_mem: dict, deep_mem: dict) -> int:
    current_size = get_json_size(global_mem)
    if current_size <= MAX_MEMORY_BYTES:
        return 0

    entries_with_score = [(compute_score(e), i, e) for i, e in enumerate(global_mem["entries"])]
    entries_with_score.sort(key=lambda x: x[0])

    forgotten = 0
    while get_json_size(global_mem) > TARGET_MEMORY_BYTES and entries_with_score:
        score, idx, entry = entries_with_score.pop(0)
        entry["forgotten_at"] = now_iso()
        deep_mem["entries"].append(entry)
        global_mem["entries"] = [e for e in global_mem["entries"] if e["id"] != entry["id"]]
        forgotten += 1

    return forgotten


def cmd_forget(args):
    global_mem = load_json(GLOBAL_MEMORY_FILE)
    deep_mem = load_json(DEEP_MEMORY_FILE)
    forgotten = do_forget(global_mem, deep_mem)
    save_json(GLOBAL_MEMORY_FILE, global_mem)
    save_json(DEEP_MEMORY_FILE, deep_mem)
    print(f"[claude-memory] Forget done: {forgotten} entries moved to deep_memory.", file=sys.stderr)


# ─── Load ────────────────────────────────────────────────────

def cmd_load(args):
    global_mem = load_json(GLOBAL_MEMORY_FILE)
    entries = global_mem.get("entries", [])
    if not entries:
        return

    imp_order = {"high": 3, "medium": 2, "low": 1}
    entries.sort(
        key=lambda e: (imp_order.get(e.get("importance", "low"), 1), e.get("access_count", 1)),
        reverse=True,
    )

    now = now_iso()
    for entry in entries:
        entry["last_accessed"] = now
        entry["access_count"] = entry.get("access_count", 1) + 1

    save_json(GLOBAL_MEMORY_FILE, global_mem)

    # Build tree: top_level -> mid_level -> [entries]
    tree = defaultdict(lambda: defaultdict(list))
    for entry in entries:
        path = entry_path(entry) or "misc/unknown"
        parts = path.split("/")
        top = parts[0]
        mid = "/".join(parts[1:-1]) if len(parts) >= 3 else ""
        tree[top][mid].append(entry)

    lines = ["<claude-memory-context>"]
    lines.append(f"AI Memory: {len(entries)} entries loaded")
    lines.append("")

    for top in sorted(tree.keys()):
        lines.append(f"## {top}/")
        mids = tree[top]
        for mid in sorted(mids.keys()):
            if mid:
                lines.append(f"### {mid}/")
            for entry in mids[mid]:
                path = entry_path(entry) or "?"
                leaf = path.split("/")[-1]
                content = entry.get("content", "")
                importance = entry.get("importance", "medium")
                access = entry.get("access_count", 1)
                preview = content[:200].replace("\n", " ")
                lines.append(f"- **{leaf}** [{importance}] ({access}x): {preview}")
        lines.append("")

    lines.append("</claude-memory-context>")
    print("\n".join(lines))


# ─── Recall ──────────────────────────────────────────────────

def cmd_recall(args):
    query = args.query.lower()
    query_tokens = tokenize(query)

    deep_mem = load_json(DEEP_MEMORY_FILE)
    global_mem = load_json(GLOBAL_MEMORY_FILE)

    results = []
    for source_name, mem in [("deep_memory", deep_mem), ("global_memory", global_mem)]:
        for entry in mem.get("entries", []):
            path = entry_path(entry)
            path_tokens = set(path.lower().replace("/", " ").replace("-", " ").split())
            content_tokens = tokenize(entry.get("content", ""))
            all_tokens = path_tokens | content_tokens
            overlap = len(query_tokens & all_tokens)
            if overlap > 0:
                results.append((overlap, source_name, entry))

    results.sort(key=lambda x: x[0], reverse=True)

    if not results:
        print(f"No memories found matching '{args.query}'")
        return

    print(f"Found {len(results)} matching memories for '{args.query}':\n")
    for score, source, entry in results[:20]:
        status = "ACTIVE" if source == "global_memory" else "FORGOTTEN"
        path = entry_path(entry) or "untitled"
        forgotten_info = ""
        if entry.get("forgotten_at"):
            forgotten_info = f" (forgotten: {entry['forgotten_at'][:10]})"
        print(f"[{status}] {path}{forgotten_info}")
        print(f"  Access: {entry.get('access_count', 0)} | ID: {entry.get('id', '?')}")
        print(f"  {entry.get('content', '')[:200]}")
        print()


# ─── Consolidate ─────────────────────────────────────────────

CONSOLIDATE_PATH_PROMPT = """Reorganize these memory entries into a tree structure.

Assign each entry a hierarchical path: 2~5 levels, lowercase english, "/" separated, each level 1~2 words.

Rules:
1. Group entries about the same topic under the SAME path — they will be merged later
2. Be VERY aggressive about merging: if entries overlap significantly, give them the SAME path
3. Path examples: "flink/deploy/jar-mapping", "github/ssh", "canoe/troubleshoot", "feishu/auth"
4. Keep paths short and descriptive. Target ~40-60 unique paths total.
5. DO NOT create near-duplicate paths like "flink/debug" AND "flink/diagnosis" — pick ONE
6. All Flink troubleshooting/debugging/diagnosis entries → one path like "flink/diagnosis"
7. All auth/token/oauth entries for same service → one path like "feishu/auth"
8. All CDN/image-proxy entries → one path like "talkie/cdn"

Output ONLY a JSON array: [{{"id": "...", "path": "..."}}]

Entries:
{entries_json}"""


NORMALIZE_PATHS_PROMPT = """These paths were assigned to memory entries across multiple batches. Some are redundant duplicates of the same concept.

Merge similar/redundant paths. Rules:
1. If two paths describe the same topic, map the less common one to the more common one
2. Example merges: "flink/debugging" + "flink/troubleshooting" -> "flink/troubleshooting"
3. Example merges: "flink/deploy/jar-mapping" + "flink/deploy/jar-structure" -> "flink/deploy/jar"
4. Keep the shorter or more descriptive path as canonical
5. Only merge paths that truly overlap — don't merge unrelated paths

Output ONLY a JSON object mapping old_path -> new_path (only include paths that need to change).
If no merges needed, output: {{}}

Paths with entry counts:
{paths_json}"""


CONSOLIDATE_MERGE_PROMPT = """Merge these memory entries into ONE concise entry. They all describe the same topic.

Rules:
1. Combine all unique information — no information loss
2. Remove duplicate descriptions
3. Keep it concise but complete
4. Output ONLY the merged content text (not JSON)

Entries to merge:
{entries_content}"""


def cmd_consolidate(args):
    dry_run = getattr(args, "dry_run", False)
    global_mem = load_json(GLOBAL_MEMORY_FILE)
    entries = global_mem.get("entries", [])

    if not entries:
        print("[claude-memory] No entries to consolidate.", file=sys.stderr)
        return

    print(f"[claude-memory] Consolidating {len(entries)} entries...", file=sys.stderr)

    # Phase 1: Path assignment via Sonnet
    compact_entries = []
    for e in entries:
        compact_entries.append({
            "id": e.get("id", ""),
            "title": e.get("title", "") or e.get("path", ""),
            "tags": e.get("tags", [])[:5],
        })

    all_assignments = []
    batch_size = 60
    assigned_paths_so_far = []
    for i in range(0, len(compact_entries), batch_size):
        batch = compact_entries[i:i + batch_size]
        batch_num = i // batch_size + 1
        print(f"[claude-memory] Path assignment batch {batch_num}...", file=sys.stderr)

        # Include previously assigned paths for cross-batch consistency
        context_suffix = ""
        if assigned_paths_so_far:
            unique_paths = sorted(set(assigned_paths_so_far))
            context_suffix = f"\n\nPaths already assigned in previous batches (reuse these when applicable):\n{json.dumps(unique_paths)}"

        prompt = CONSOLIDATE_PATH_PROMPT.format(
            entries_json=json.dumps(batch, ensure_ascii=False, indent=1)
        ) + context_suffix
        raw = call_sonnet(prompt, system="Output only valid JSON array.", max_tokens=8192)

        try:
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                assignments = json.loads(match.group())
                all_assignments.extend(assignments)
                assigned_paths_so_far.extend(a.get("path", "") for a in assignments)
            else:
                print(f"[claude-memory] Failed to parse batch {batch_num}. Response preview:", file=sys.stderr)
                print(raw[:500], file=sys.stderr)
                return
        except json.JSONDecodeError as e:
            print(f"[claude-memory] JSON parse error batch {batch_num}: {e}", file=sys.stderr)
            return

    # Build id → path mapping
    id_to_path = {a["id"]: a["path"] for a in all_assignments if "id" in a and "path" in a}

    # Phase 1.5: Normalize paths across batches
    path_counts = defaultdict(int)
    for p in id_to_path.values():
        path_counts[p] += 1

    if len(path_counts) > 1:
        print(f"[claude-memory] Normalizing {len(path_counts)} paths...", file=sys.stderr)
        paths_info = [{"path": p, "count": c} for p, c in sorted(path_counts.items())]
        norm_prompt = NORMALIZE_PATHS_PROMPT.format(
            paths_json=json.dumps(paths_info, ensure_ascii=False, indent=1)
        )
        norm_raw = call_sonnet(norm_prompt, system="Output only valid JSON object.", max_tokens=4096)

        try:
            match = re.search(r"\{.*\}", norm_raw, re.DOTALL)
            if match:
                path_remap = json.loads(match.group())
                if path_remap:
                    for eid in id_to_path:
                        old_path = id_to_path[eid]
                        if old_path in path_remap:
                            id_to_path[eid] = path_remap[old_path]
                    remapped = len(path_remap)
                    new_unique = len(set(id_to_path.values()))
                    print(f"[claude-memory] Normalized: {remapped} paths remapped, {new_unique} unique paths remain.", file=sys.stderr)
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"[claude-memory] Path normalization parse error (skipping): {e}", file=sys.stderr)

    # Group entries by assigned path
    path_groups = defaultdict(list)
    for entry in entries:
        eid = entry.get("id", "")
        path = id_to_path.get(eid, "misc/unassigned")
        path_groups[path].append(entry)

    # Print preview
    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"Result: {len(entries)} entries -> {len(path_groups)} paths", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    for path in sorted(path_groups.keys()):
        group = path_groups[path]
        print(f"\n  {path} ({len(group)} entries):", file=sys.stderr)
        for e in group:
            label = e.get("title", "") or e.get("path", "?")
            print(f"    - [{e.get('id', '')}] {label[:60]}", file=sys.stderr)

    if dry_run:
        print(f"\n[claude-memory] Dry run complete. No changes written.", file=sys.stderr)
        return

    # Phase 2: Merge content for multi-entry groups
    print(f"\n[claude-memory] Merging content for multi-entry groups...", file=sys.stderr)

    new_entries = []
    for path, group in sorted(path_groups.items()):
        if len(group) == 1:
            entry = group[0]
            new_entries.append({
                "id": entry.get("id", gen_id()),
                "path": path,
                "content": entry.get("content", ""),
                "access_count": entry.get("access_count", 1),
                "last_accessed": entry.get("last_accessed", now_iso()),
                "importance": entry.get("importance", "medium"),
                "created_at": entry.get("created_at", now_iso()),
            })
        else:
            # Call Sonnet to merge content
            entries_text = ""
            for j, e in enumerate(group):
                label = e.get("title", "") or e.get("path", "?")
                entries_text += f"\n--- Entry {j + 1}: {label} ---\n{e.get('content', '')}\n"

            prompt = CONSOLIDATE_MERGE_PROMPT.format(entries_content=entries_text)
            merged_content = call_sonnet(
                prompt,
                system="Merge knowledge entries. Output only the merged content text.",
            )

            if not merged_content.strip():
                merged_content = max((e.get("content", "") for e in group), key=len)

            base = max(group, key=lambda e: e.get("access_count", 1))
            base_id = base.get("id", gen_id())

            imp = "high" if any(e.get("importance") == "high" for e in group) else \
                  "medium" if any(e.get("importance") == "medium" for e in group) else "low"

            new_entries.append({
                "id": base_id,
                "path": path,
                "content": merged_content.strip(),
                "access_count": max(e.get("access_count", 1) for e in group),
                "last_accessed": max(e.get("last_accessed", "") for e in group),
                "importance": imp,
                "created_at": min(e.get("created_at", now_iso()) for e in group),
            })
            print(f"  Merged {len(group)} entries -> {path}", file=sys.stderr)

    # Backup and save
    backup_path = GLOBAL_MEMORY_FILE.with_suffix(".json.bak")
    shutil.copy2(GLOBAL_MEMORY_FILE, backup_path)
    print(f"[claude-memory] Backup saved to {backup_path}", file=sys.stderr)

    global_mem["version"] = 2
    global_mem["entries"] = new_entries
    save_json(GLOBAL_MEMORY_FILE, global_mem)

    size_kb = get_json_size(global_mem) / 1024
    print(
        f"\n[claude-memory] Consolidation complete: {len(entries)} -> {len(new_entries)} entries ({size_kb:.1f}KB)",
        file=sys.stderr,
    )


# ─── Reactivate ──────────────────────────────────────────────

def cmd_reactivate(args):
    entry_id = args.id
    deep_mem = load_json(DEEP_MEMORY_FILE)
    global_mem = load_json(GLOBAL_MEMORY_FILE)

    target = None
    for i, entry in enumerate(deep_mem["entries"]):
        if entry["id"] == entry_id:
            target = deep_mem["entries"].pop(i)
            break

    if not target:
        print(f"Entry {entry_id} not found in deep_memory.", file=sys.stderr)
        return

    target["forgotten_at"] = None
    target["last_accessed"] = now_iso()
    target["access_count"] = target.get("access_count", 1) + 1
    global_mem["entries"].append(target)

    save_json(GLOBAL_MEMORY_FILE, global_mem)
    save_json(DEEP_MEMORY_FILE, deep_mem)
    print(f"[claude-memory] Reactivated: {entry_path(target) or entry_id}")


# ─── Stats ───────────────────────────────────────────────────

def cmd_stats(args):
    global_mem = load_json(GLOBAL_MEMORY_FILE)
    deep_mem = load_json(DEEP_MEMORY_FILE)

    g_entries = global_mem.get("entries", [])
    d_entries = deep_mem.get("entries", [])
    g_size = get_json_size(global_mem) / 1024
    d_size = get_json_size(deep_mem) / 1024

    print("=== AI Memory Stats ===")
    print(f"Global Memory: {len(g_entries)} entries ({g_size:.1f}KB / {MAX_MEMORY_BYTES // 1024}KB)")
    print(f"Deep Memory:   {len(d_entries)} entries ({d_size:.1f}KB)")
    print()

    if g_entries:
        tops = defaultdict(int)
        for e in g_entries:
            top = entry_path(e).split("/")[0]
            tops[top] += 1
        print("Tree roots:", ", ".join(f"{k}:{v}" for k, v in sorted(tops.items(), key=lambda x: -x[1])))

        top = sorted(g_entries, key=lambda e: e.get("access_count", 0), reverse=True)[:5]
        print("\nMost accessed:")
        for e in top:
            print(f"  [{e.get('access_count', 0)}x] {entry_path(e)}")

        bottom = sorted(g_entries, key=lambda e: compute_score(e))[:5]
        print("\nAt risk of forgetting:")
        for e in bottom:
            score = compute_score(e)
            print(f"  [score:{score:.2f}] {entry_path(e)}")


# ─── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI Memory Manager")
    sub = parser.add_subparsers(dest="command")

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--transcript", required=True)
    p_extract.add_argument("--session-id", default="")

    sub.add_parser("load")
    sub.add_parser("forget")
    sub.add_parser("stats")

    p_recall = sub.add_parser("recall")
    p_recall.add_argument("--query", required=True)

    p_react = sub.add_parser("reactivate")
    p_react.add_argument("--id", required=True)

    p_consolidate = sub.add_parser("consolidate")
    p_consolidate.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    cmd_map = {
        "extract": cmd_extract,
        "load": cmd_load,
        "forget": cmd_forget,
        "stats": cmd_stats,
        "recall": cmd_recall,
        "reactivate": cmd_reactivate,
        "consolidate": cmd_consolidate,
    }

    if args.command in cmd_map:
        cmd_map[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
