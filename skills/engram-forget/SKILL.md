---
name: engram-forget
description: "Soft-demote or archive a ToolEngrams memory. Use when user says 'forget X', 'don't remember X', or a memory is no longer relevant."
---

# ToolEngrams: Forget

Demote or remove a memory from the tool-bound memory store. Default is soft demote (memory still exists but scores very low); `--delete` fully archives it.

## When to Use

- User says "forget X", "don't remember X", "ignore that memory"
- A memory is outdated or no longer relevant
- User explicitly asks to remove a fact or rule

## Commands

### Soft demote (default — keeps memory, tanks its score)
```bash
engram forget "<memory name>"
```

### Hard archive (excluded from all retrieval)
```bash
engram forget --delete "<memory name>"
```

### Soft-demote all memories matching a topic
```bash
engram forget --topic "<keyword>"
```

### Restore a previously forgotten/archived memory
```bash
engram forget --restore "<memory name>"
```

## Name Lookup

Name matching is fuzzy: exact match → FTS search → LIKE substring. You don't need the exact name — a distinctive keyword from the memory name usually works.

## Output

JSON with action taken, memory ID, and name.
