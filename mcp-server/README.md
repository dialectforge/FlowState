# FlowState MCP server

The Python package behind FlowState: a local MCP server that gives AI apps a shared project
memory in a SQLite file. Setup, features and app configs are in the repository's
[README.md](../README.md) and [AGENTS.md](../AGENTS.md); the easiest install is
`python setup_flowstate.py` from the repository root.

## Install (Python 3.11+)

```bash
pip install -e .            # the server
pip install -e ".[dev]"     # plus pytest, pytest-asyncio, httpx for the tests
```

Search by meaning is optional; see "Honest limits" in the main README.

## Run

```bash
python -m flowstate                                   # stdio (what desktop apps start)
python -m flowstate --transport http --host 127.0.0.1 --port 8766   # HTTP, needs a bearer token
python -m flowstate --help                            # all server options
python -m flowstate <command> -h                      # maintenance commands: backup, restore, review-page, ...
```

The `flowstate` command does the same as `python -m flowstate`. The database is `FLOWSTATE_DB`
if set, else the per-OS default listed in AGENTS.md.
