---
name: engram-recall
description: "Browse and search ToolEngrams memories. Use when user asks 'what do you remember about X', 'show my memories', or you need to check what's stored."
---

# ToolEngrams: Recall

Browse, search, and inspect memories in the tool-bound memory store. Memories normally surface automatically via hooks — use this skill for explicit browsing when the user asks about stored knowledge.

## When to Use

- User asks "what do you remember about X?"
- User asks "show my memories" or "list memories"
- You need to check if a fact is already stored before saving a duplicate
- User wants to audit or review what's in the memory store

## Commands

### List all active memories
```bash
engram recall
```

### Search by keyword (FTS)
```bash
engram recall "<query>"
```

### Show full detail for one memory (including triggers and recent surfaces)
```bash
engram recall --id <memory_id>
```

### Show summary counts by kind/scope/trigger kind
```bash
engram recall --stats
```

### Limit results
```bash
engram recall "<query>" --limit 5
```

## Output

JSON with matching memories. Each memory includes: id, name, kind, scope, surface_count, useful_count, pinned status. `--id` also shows triggers and recent surface history.
