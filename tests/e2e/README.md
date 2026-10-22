# End-to-end mock project test

`run_e2e.py` starts the real FlowState server from this checkout and works on a made-up
project ("Lakeside Bakery ordering app") the way AI apps do, only through MCP tool calls:

- claude-code in two windows (two stdio server processes), cursor (stdio), grok (streamable
  HTTP with its own app key), plus a slow-timeout window, two HTTP claude-code sessions and
  a v2.3.0 server for the upgrade check;
- 13 scenarios (fresh install, a normal day with claims, conflicting decisions, review mode,
  self-approval and the `flowstate approve` step, receipts and timed-out writes, two windows
  and "since your last visit", undo, deletes / backups / safety copies / health check, run
  ledger, upgrade from v2.3.0, bad input, concurrency and the real-data guard, tool
  descriptions, and approvals on: the popup, the phone page and the terminal), each pass/fail
  with evidence;
- every tool and every action it offers is called at least once (the report says how many).

```bash
.venv/bin/python tests/e2e/run_e2e.py                 # about 4-5 minutes
.venv/bin/python tests/e2e/run_e2e.py --keep          # keep /tmp/fs-e2e-<pid> to inspect
.venv/bin/python tests/e2e/run_e2e.py --skip-upgrade  # no v2.3.0 worktree
```

Safety: every FlowState path (database, backups, config, token file and store, auth DB,
relay, project files, data-git, and HOME itself) points into a new `/tmp/fs-e2e-<pid>`
folder, deleted at the end unless `--keep`. Every server process loads
`tests/guard_site/sitecustomize.py`, which refuses (and records in `guard.log`) any touch of
the real FlowState folders; scenario 11 fails if anything was refused. `flowstate approve` /
`reject` run either in a new session with no terminal (to prove a piped "y" is refused) or
in a pseudo-terminal of their own, never on the terminal you run this from. Scenario 13's
phone check uses a fake relay enrollment and a key pair made in memory for the run; the server
processes get its public key (and a 4-second popup limit) only through `FS_TEST_PATCH`, which
`tests/guard_site` applies; FlowState itself has no such switch. The HTTP token is generated per run and never printed. The upgrade step uses
`git worktree add /tmp/fs-230 v2.3.0` and removes it again. Only servers the script started
are stopped, by their own process. The report's last line checks that the real FlowState
folders were not touched.

It needs `git`, the repo's `.venv` (the `mcp` and `httpx` packages FlowState already uses)
and the `v2.3.0` tag. Exit code 0 only if every check passes. The bugs its first run found
have regression tests in the normal suite (`tests/test_e2e_findings.py` and others).
