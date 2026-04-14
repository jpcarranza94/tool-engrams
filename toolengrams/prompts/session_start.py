"""SessionStart formation guidance — injected at the start of every session."""

FORMATION_GUIDANCE = """\
[ToolEngrams: tool-bound memory]
You have ToolEngrams — memories bound to tool-call patterns that surface \
automatically when you call matching tools. Memory formation happens \
automatically in the background — you don't need to manage it.

If you want to manually save or manage memories:
  Save: engram remember "<body with `backticked commands`>" --type <feedback|reference>
  Forget: engram forget "<name>"
  Browse: engram recall [query]\
"""
