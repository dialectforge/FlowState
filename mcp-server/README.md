# flowstatemcp

Local-first shared project memory for AI apps. One SQLite file on your computer holds the
todos, changes, decisions, problems, and lessons your AI apps share. The import package is
`flowstate`. The PyPI name is `flowstatemcp`, because `flowstate` is a different project.

- Site: https://flowstatemcp.com
- Hosted (no install): https://flowstatemcp.com/hosted
- Source: https://github.com/dialectforge/FlowState

## Install

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/) for the
one-command form.

```bash
uvx flowstatemcp
```

That starts the stdio MCP server. These do the same thing:

```bash
pipx run flowstatemcp
pip install flowstatemcp
flowstatemcp
flowstate
```

`pip install flowstate` does not install this project.

The git clone is the fallback when you want `setup_flowstate.py` to write app configs, schedule
backups, and install the FlowState skill. See the repository
[README](https://github.com/dialectforge/FlowState/blob/main/README.md).

## Run

```bash
flowstatemcp                                 # stdio (what desktop apps start)
flowstate                                    # the same server
python -m flowstate --transport http --host 127.0.0.1 --port 8766
flowstate --help
```

The database is `FLOWSTATE_DB` if set, otherwise the per-OS default in the repository AGENTS.md.

A plain `pip install flowstatemcp` (or of a built wheel) includes the base schema
(`flowstate/schema.sql`), the migrations, and `release_signers`. `flowstate update` only works
in a git clone set up by `setup_flowstate.py`. Like every FlowState 2.4.0 or later, it checks
GitHub once a day for a newer release (nothing about you or your projects is sent);
`flowstate update check off` or `FLOWSTATE_UPDATE_CHECK=0` turns that off.

Search by meaning is optional; see "Honest limits" in the repository README.
