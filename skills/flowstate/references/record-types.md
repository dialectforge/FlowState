# FlowState record types: one example each

| kind | example call |
|---|---|
| decision | `log_work(project="Atlas VPN", kind="decision", summary="Use TCP fallback when UDP is blocked", details="Rejected: QUIC (no stable library yet)")` |
| decision that replaces one | `log_work(project="Atlas VPN", kind="decision", summary="Use QUIC fallback", replaces=41)` |
| change | `log_work(project="Home Hub", kind="change", summary="Moved the context layer behind one read contract", component="Hub context layer")` |
| change on a shared component | `log_work(project="ChatApp", kind="change", summary="Frame header now 16 bytes", component="Wire Protocol")` (alerts every project linked to it) |
| problem | `log_work(project="Inkwell", kind="problem", summary="Export crashes on empty chapters", priority="urgent")` |
| learning | `log_work(project="FlowState v2", kind="learning", summary="Claude Desktop ignores cwd; set PYTHONPATH", category="gotcha")` |
| todo | `log_work(project="Cattle Dog", kind="todo", summary="Add offline mode", priority="normal")` |
| where it came from | add `source="user_stated"` (the user said it), `"ai_inferred"` or `"from_code"` |
| not sure | add `suggested=true` to any of the above: it waits for the user's OK |
| a hard rule | `rules(action="add", project="FlowState v2", rule="Never push; local commits only")` |
| stopping mid-task | `handoff(action="leave", project="Inkwell", where_i_was="Export rewrite half done; 3/5 tests pass", next_step="Fix the unicode test")` |

Links:

| link | example |
|---|---|
| a project uses a shared component | `link(action="add", project="Atlas VPN", link_type="uses", target_project="Atlas OS", target_component="Wire Protocol")` |
| a project depends on another | `link(action="add", project="Home Hub", link_type="depends_on", target_project="Helper AI")` |
| two components are the same thing | `link(action="add", project="FlowState", link_type="same_as", component="Helper client", target_project="Helper AI", target_component="Helper core")` |
| a GitHub issue on a todo | `link(action="add", project="FlowState v2", item_type="todo", item_id=381, url="https://github.com/owner/repo/issues/12")` |
| a subtask / a blocker | `todo(action="add", project=..., title=..., parent_todo_id=12)` / `todo(action="block", project=..., todo_id=13, blocked_by=[12])` |
