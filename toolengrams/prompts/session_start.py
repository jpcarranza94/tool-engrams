"""SessionStart formation guidance — injected at the start of every session."""

FORMATION_GUIDANCE = """\
[ToolEngrams: tool-bound memory]
You have ToolEngrams — memories bound to tool-call patterns that surface \
automatically when you call matching tools. Memory formation happens \
automatically in the background — you don't need to manage it.

If you want to manually save or manage memories:
  Save: engram remember "<body>" --trigger "<command prefix>" --type <feedback|reference>
  Example: engram remember "Use --force-with-lease" --trigger "git push --force" --trigger "git push -f" --type feedback
  Forget: engram forget "<name>"
  Browse: engram recall [query]

Use --trigger to specify exactly which command prefix the memory binds to. \
Use --type feedback for corrections (blocks the call), --type reference for info (context only).\
"""
