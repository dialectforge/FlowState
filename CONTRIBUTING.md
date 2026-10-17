# Contributing to FlowState

Thanks for helping. Issues and pull requests are welcome, and so are AI-assisted ones:
this repo is written to be worked on by AI agents as well as people.

## Set up

Python 3.11 or newer. From the repo root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
python -m pip install -e "mcp-server[dev]"
```

## Run the tests

```bash
python -m pytest tests -q
```

The tests use temporary databases and fake config folders only. They run on Windows,
macOS and Linux with Python 3.11 to 3.13 in GitHub Actions on every push and pull request.

## The details

The full guide for working on the code (layout, migrations, identity, tool labels,
changelog) is in [AGENTS.md](AGENTS.md), under "Working on this repo". It is kept there
so the two files don't drift apart.

## Never commit

Real databases (`*.db`), tokens or keys, `http-token.txt`, `.env` files, or logs.
Don't point tests or experiments at a real FlowState database or a real app config.
