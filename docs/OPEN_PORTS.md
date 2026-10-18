# Open follow-ons

Work found during a change that did not ship with it. Add an entry when you defer something;
remove it when it ships.

## From the v2.1 tool-surface redesign (branch `redesign/tool-surface`)

- **User-gated hard delete** for projects, components and records. Today retiring is by
  status (`archived`, `deprecated`, `cancelled`) and duplicates go through
  `manage_memory(action="cleanup")` and the review queue. Glama's completeness check counts
  the missing delete; decide whether FlowState wants one at all (and how it fits undo and
  v1.1 compatibility).
- **Category filter when listing learnings.** `get_learnings(category=...)` has no listed
  equivalent: `search` lists learnings but cannot filter by category. Add
  `category` to `search` if anyone needs it.
- **Dead modules.** `mcp-server/flowstate/server_v14.py` and `split_tools.py` are imported
  by nothing. Remove them in a cleanup change.
- **Re-check the Glama score** after the next release. The 2026-10-17 score was taken from an
  older build (descriptions such as "Manage problems." that are not on main).
- **Older names in error messages.** A few error texts still name the internal operation
  (e.g. `rules(add) needs project`). They are correct but use the v2.0 names.
