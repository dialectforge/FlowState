# Open follow-ons

Work found during a change that did not ship with it. Add an entry when you defer something;
remove it when it ships.

## From the v2.1 tool-surface redesign (branch `redesign/tool-surface`)

- **Category filter when listing learnings.** `get_learnings(category=...)` has no listed
  equivalent: `search` lists learnings but cannot filter by category. Add
  `category` to `search` if anyone needs it.
- **Dead modules.** `mcp-server/flowstate/server_v14.py` and `split_tools.py` are imported
  by nothing. Remove them in a cleanup change.
- **Re-check the Glama score** after the next release. The 2026-10-17 score was taken from an
  older build (descriptions such as "Manage problems." that are not on main).
- **Older names in error messages.** A few error texts still name the internal operation
  (e.g. `rules(add) needs project`). They are correct but use the v2.0 names.

## From user-approved deletes (task 2, same branch)

Closed: the "user-gated hard delete" follow-on. Deletes ship as user-approved requests
(`deletes.py`, see `docs/TOOL_CONSOLIDATION.md`).

- **Erase for good.** An approved delete is kept in `record_history` (that is how undo works)
  and in backups, so the text stays in the database file. If someone needs a real erasure
  (privacy), add a user-only purge of a delete's copies (CLI or review page, never a tool).
- **Attachment file on delete.** Approving an attachment's delete removes the row and keeps
  the bundle copy of the file (so undo is complete). The older `delete_file=true` is ignored
  in the request flow; add a user-side "also delete the file" if wanted.
- **Unknown project name on reads.** A read naming a project that matches nothing (for
  example `manage_history(action="list", project=<a deleted project>)`) answers
  `project_required` instead of `project_not_found`. Wording bug in server.py's read path.
- **Mixed server versions.** A v2.0 server on the same database can undo an approved delete
  (fine) but would also allow undoing that undo, re-deleting without approval, and can't undo
  a project delete. Once deletes are used, run only v2.1+ servers against the database.
- **Plural wording.** Delete request summaries say "1 attempts", "1 links".

