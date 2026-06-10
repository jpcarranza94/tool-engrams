"""SessionStart formation guidance — injected at the start of every session."""

FORMATION_GUIDANCE = """\
[ToolEngrams: tool-bound memory]
You have ToolEngrams — memories bound to tool-call patterns that surface \
automatically when you call matching tools. Memory formation happens \
automatically in the background — you don't need to manage it.

If you want to manually save or manage memories:
  Save: engram remember "<body>" --trigger "<token sequence>" --kind <block|hint>
  Example: engram remember "Use --force-with-lease" --trigger "git push --force" --trigger "git push -f" --kind block
  Forget: engram forget "<name>"
  Browse: engram recall [query]

Use --trigger to specify the required token sequence (subseq match, gaps allowed). \
Use --kind block for rules to enforce at PreToolUse (denies the call; rare). \
Use --kind hint for info injected as context alongside matching calls and on \
matching tool failures (default; non-blocking).\
"""
