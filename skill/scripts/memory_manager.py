#!/usr/bin/env python3
"""
AI Memory Manager for Claude Code
Extracts, stores, merges, forgets, and loads persistent memory across sessions.
Pure stdlib — no third-party dependencies.
"""

import json
import os
import sys
import argparse
import hashlib
import re
import urllib.request
import urllib.error
import ssl
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
GLOBAL_MEMORY_FILE = DATA_DIR / "global_memory.json"
DEEP_MEMORY_FILE = DATA_DIR / "deep_memory.json"

MAX_MEMORY_BYTES = 500 * 1024  # 500KB
TARGET_MEMORY_BYTES = 450 * 1024  # 450KB after cleanup
MAX_TRANSCRIPT_CHARS = 80000  # limit transcript sent to API
SIMILARITY_TAG_THRESHOLD = 0.6
SIMILARITY_TITLE_THRESHOLD = 0.5


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def gen_id():
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    h = hashlib.md5(now_iso().encode() + os.urandom(4)).hexdigest()[:4]
    return f"mem_{ts}_{h}"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "entries": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_json_size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False).encode("utf-8"))


# ─── Similarity ──────────────────────────────────────────────

def tokenize(text: str) -> set:
    return set(re.findall(r"[\w一-鿿]+", text.lower()))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def tags_similarity(tags_a: list, tags_b: list) -> float:
    if not tags_a or not tags_b:
        return 0.0
    sa, sb = set(t.lower() for t in tags_a), set(t.lower() for t in tags_b)
    return len(sa & sb) / len(sa | sb)


def is_similar(entry_a: dict, entry_b: dict) -> bool:
    tag_sim = tags_similarity(entry_a.get("tags", []), entry_b.get("tags", []))
    if tag_sim >= SIMILARITY_TAG_THRESHOLD:
        return True
    title_sim = jaccard(
        tokenize(entry_a.get("title", "")),
        tokenize(entry_b.get("title", "")),
    )
    if title_sim >= SIMILARITY_TITLE_THRESHOLD:
        return True
    return False


def merge_entries(existing: dict, new_entry: dict) -> dict:
    existing["access_count"] = existing.get("access_count", 1) + 1
    existing["last_accessed"] = now_iso()

    ec = existing.get("content", "")
    nc = new_entry.get("content", "")
    if len(nc) > len(ec):
        existing["content"] = nc

    all_tags = list(set(existing.get("tags", []) + new_entry.get("tags", [])))
    existing["tags"] = all_tags

    imp_order = {"high": 3, "medium": 2, "low": 1}
    ei = imp_order.get(existing.get("importance", "low"), 1)
    ni = imp_order.get(new_entry.get("importance", "low"), 1)
    if ni > ei:
        existing["importance"] = new_entry["importance"]

    if existing.get("created_at", "") > new_entry.get("created_at", ""):
        existing["created_at"] = new_entry["created_at"]

    merged_from = existing.get("merged_from", [])
    mid = new_entry.get("id", "")
    if mid and mid not in merged_from:
        merged_from.append(mid)
    existing["merged_from"] = merged_from

    return existing


# ─── API ─────────────────────────────────────────────────────

def call_sonnet(prompt: str, system: str = "") -> str:
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    model = os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy", "")
    custom_headers_str = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")

    url = f"{base_url}/v1/messages"
    body = {
        "model": model,
        "max_tokens": 4096,
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
        with opener.open(req, timeout=120) as resp:
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
                # Claude Code transcript format: "type" is "user"/"assistant", "message" contains the content
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


EXTRACT_PROMPT = """You are analyzing a Claude Code session transcript. Extract the most important and reusable knowledge from this conversation.

For each piece of knowledge, output a JSON object. Output ONLY a JSON array, no other text.

Each object must have:
- "category": one of "knowledge", "skill", "pattern", "solution", "reference"
- "title": short descriptive title (Chinese or English, match the conversation language)
- "content": the core knowledge/skill, concise but complete enough to be useful later
- "importance": "high", "medium", or "low"
- "tags": list of lowercase keyword tags for matching

Categories:
- knowledge: facts, configurations, credentials, system behaviors learned
- skill: methods, techniques, workflows the user demonstrated or learned
- pattern: recurring patterns in code, debugging, or operations
- solution: specific problem→solution pairs
- reference: pointers to external resources, docs, URLs, tools

Rules:
- Only extract genuinely useful, non-obvious information
- Skip trivial exchanges, greetings, and routine operations
- Merge related info into single entries rather than many tiny ones
- Content should be self-contained — understandable without the original conversation
- If nothing worth saving, return an empty array: []
- Output MUST be valid JSON array

Transcript:
---
{transcript}
---"""


def cmd_extract(args):
    transcript_text = read_transcript(args.transcript)
    if not transcript_text.strip():
        print("[claude-memory] Empty transcript, nothing to extract.", file=sys.stderr)
        return

    prompt = EXTRACT_PROMPT.format(transcript=transcript_text)
    raw = call_sonnet(prompt, system="You extract structured knowledge from conversations. Output only valid JSON.")

    if not raw.strip():
        print("[claude-memory] No response from API.", file=sys.stderr)
        return

    # parse JSON array from response
    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            new_entries_raw = json.loads(match.group())
        else:
            print(f"[claude-memory] Could not parse JSON from response.", file=sys.stderr)
            return
    except json.JSONDecodeError as e:
        print(f"[claude-memory] JSON parse error: {e}", file=sys.stderr)
        return

    if not new_entries_raw:
        print("[claude-memory] No entries extracted.", file=sys.stderr)
        return

    global_mem = load_json(GLOBAL_MEMORY_FILE)
    deep_mem = load_json(DEEP_MEMORY_FILE)

    now = now_iso()
    session_id = getattr(args, "session_id", "") or ""

    added = 0
    merged_global = 0
    reactivated = 0

    for raw_entry in new_entries_raw:
        new_entry = {
            "id": gen_id(),
            "category": raw_entry.get("category", "knowledge"),
            "title": raw_entry.get("title", ""),
            "content": raw_entry.get("content", ""),
            "source_session": session_id,
            "created_at": now,
            "last_accessed": now,
            "access_count": 1,
            "importance": raw_entry.get("importance", "medium"),
            "tags": raw_entry.get("tags", []),
            "merged_from": [],
            "forgotten_at": None,
        }

        # 1) try merge with global_memory
        merged = False
        for existing in global_mem["entries"]:
            if is_similar(existing, new_entry):
                merge_entries(existing, new_entry)
                merged = True
                merged_global += 1
                break

        # 2) check deep_memory for reactivation
        to_reactivate = []
        for i, deep_entry in enumerate(deep_mem["entries"]):
            if is_similar(deep_entry, new_entry):
                to_reactivate.append(i)

        for idx in reversed(to_reactivate):
            reactivated_entry = deep_mem["entries"].pop(idx)
            reactivated_entry["forgotten_at"] = None
            reactivated_entry["last_accessed"] = now
            reactivated_entry["access_count"] = reactivated_entry.get("access_count", 1) + 1

            # merge with the new entry content
            if not merged:
                merge_entries(reactivated_entry, new_entry)
                global_mem["entries"].append(reactivated_entry)
                merged = True
            else:
                # already merged into global, just reactivate the deep entry
                global_mem["entries"].append(reactivated_entry)
            reactivated += 1

        if not merged:
            global_mem["entries"].append(new_entry)
            added += 1

    # 3) run forget if needed
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

    recency = max(0, 1.0 - days_ago / 180)  # decays to 0 over 180 days
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
    entries.sort(key=lambda e: (imp_order.get(e.get("importance", "low"), 1), e.get("access_count", 1)), reverse=True)

    now = now_iso()
    for entry in entries:
        entry["last_accessed"] = now
        entry["access_count"] = entry.get("access_count", 1) + 1

    save_json(GLOBAL_MEMORY_FILE, global_mem)

    # output as readable context
    lines = ["<claude-memory-context>"]
    lines.append(f"AI Memory: {len(entries)} entries loaded")
    lines.append("")

    for entry in entries:
        cat = entry.get("category", "knowledge")
        title = entry.get("title", "untitled")
        content = entry.get("content", "")
        importance = entry.get("importance", "medium")
        tags = ", ".join(entry.get("tags", []))
        access = entry.get("access_count", 1)

        lines.append(f"## [{cat}/{importance}] {title} (used:{access}x)")
        lines.append(content)
        if tags:
            lines.append(f"Tags: {tags}")
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
    for entry in deep_mem["entries"]:
        title_tokens = tokenize(entry.get("title", ""))
        content_tokens = tokenize(entry.get("content", ""))
        tag_tokens = set(t.lower() for t in entry.get("tags", []))
        all_tokens = title_tokens | content_tokens | tag_tokens

        overlap = len(query_tokens & all_tokens)
        if overlap > 0:
            results.append((overlap, "deep_memory", entry))

    for entry in global_mem["entries"]:
        title_tokens = tokenize(entry.get("title", ""))
        content_tokens = tokenize(entry.get("content", ""))
        tag_tokens = set(t.lower() for t in entry.get("tags", []))
        all_tokens = title_tokens | content_tokens | tag_tokens

        overlap = len(query_tokens & all_tokens)
        if overlap > 0:
            results.append((overlap, "global_memory", entry))

    results.sort(key=lambda x: x[0], reverse=True)

    if not results:
        print(f"No memories found matching '{args.query}'")
        return

    print(f"Found {len(results)} matching memories for '{args.query}':\n")
    for score, source, entry in results[:20]:
        status = "ACTIVE" if source == "global_memory" else "FORGOTTEN"
        forgotten_info = ""
        if entry.get("forgotten_at"):
            forgotten_info = f" (forgotten: {entry['forgotten_at'][:10]})"
        print(f"[{status}] {entry.get('title', 'untitled')}{forgotten_info}")
        print(f"  Category: {entry.get('category', '?')} | Access: {entry.get('access_count', 0)} | ID: {entry.get('id', '?')}")
        print(f"  {entry.get('content', '')[:200]}")
        print()


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
    print(f"[claude-memory] Reactivated: {target.get('title', entry_id)}")


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
        cats = {}
        for e in g_entries:
            c = e.get("category", "unknown")
            cats[c] = cats.get(c, 0) + 1
        print("Categories:", ", ".join(f"{k}:{v}" for k, v in sorted(cats.items())))

        top = sorted(g_entries, key=lambda e: e.get("access_count", 0), reverse=True)[:5]
        print("\nMost accessed:")
        for e in top:
            print(f"  [{e.get('access_count', 0)}x] {e.get('title', 'untitled')}")

        bottom = sorted(g_entries, key=lambda e: compute_score(e))[:5]
        print("\nAt risk of forgetting:")
        for e in bottom:
            score = compute_score(e)
            print(f"  [score:{score:.2f}] {e.get('title', 'untitled')}")


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

    args = parser.parse_args()
    cmd_map = {
        "extract": cmd_extract,
        "load": cmd_load,
        "forget": cmd_forget,
        "stats": cmd_stats,
        "recall": cmd_recall,
        "reactivate": cmd_reactivate,
    }

    if args.command in cmd_map:
        cmd_map[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
