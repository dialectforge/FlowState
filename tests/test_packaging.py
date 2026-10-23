"""A plain, non-editable install (2.4.1). 2.4.0's wheel left out database/schema.sql, so a
`pip install mcp-server` (not -e) answered every call with flowstate_unavailable. These tests
check that the package carries everything FlowState reads from disk, and build a real wheel,
install it into a temp venv and use it there, away from the repository.

The wheel test runs offline: it builds with the hatchling already installed (or pip's own
build isolation if pip can get hatchling, e.g. from its cache) and installs the wheel with
--no-deps --no-index; the venv borrows mcp, pydantic and their dependencies from the test
environment through a .pth file. If a wheel or a venv can't be made here, it is skipped."""

import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import venv
from pathlib import Path

import pytest

from conftest import GUARD_SITE, REPO, SERVER_DIR

PKG = SERVER_DIR / "flowstate"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_package_schema_matches_the_repo_schema():
    """flowstate/schema.sql (what the code reads) and database/schema.sql (what setup and the
    docs point at) must be the same file."""
    assert _text(PKG / "schema.sql") == _text(REPO / "database" / "schema.sql")


def test_code_reads_the_schema_from_the_package():
    from flowstate import database
    assert database.schema_path() == database.PACKAGE_SCHEMA_PATH == (PKG / "schema.sql").resolve()


def test_schema_falls_back_to_the_repo_copy(monkeypatch, tmp_path):
    from flowstate import database
    monkeypatch.setattr(database, "PACKAGE_SCHEMA_PATH", tmp_path / "missing.sql")
    assert database.schema_path() == database.REPO_SCHEMA_PATH
    db = tmp_path / "fallback.db"
    database.Database(db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM projects").fetchone() == (0,)


def test_missing_schema_leaves_no_empty_database(monkeypatch, tmp_path):
    from flowstate import database
    monkeypatch.setattr(database, "PACKAGE_SCHEMA_PATH", tmp_path / "missing.sql")
    monkeypatch.setattr(database, "REPO_SCHEMA_PATH", tmp_path / "missing-too.sql")
    db = tmp_path / "never.db"
    with pytest.raises(FileNotFoundError):
        database.Database(db)
    assert not db.exists()


def test_an_empty_database_file_from_2_4_0_is_set_up(tmp_path):
    """2.4.0 without its schema file left a 0-byte database behind; 2.4.1 sets it up."""
    from flowstate import server as S
    db = tmp_path / "left-by-2.4.0.db"
    db.write_bytes(b"")
    S.init_backend(db)
    try:
        assert S.backend.error is None, S.backend.error
    finally:
        S.backend.tracker = None
    from flowstate import migrations
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == migrations.latest_version()


def test_every_data_file_is_inside_the_package():
    """Nothing the package reads at run time lives outside mcp-server/flowstate."""
    for name in ("schema.sql", "release_signers"):
        assert (PKG / name).is_file(), name
    assert len(list((PKG / "migrations").glob("[0-9][0-9][0-9]_*.sql"))) >= 22


# ------------------------------------------------------------------ the real wheel
CHECK = textwrap.dedent(r'''
    import asyncio, json, os, sqlite3, subprocess, sys
    from pathlib import Path

    import flowstate
    from flowstate import database, migrations

    here = Path(flowstate.__file__).resolve().parent
    prefix = Path(sys.prefix).resolve()
    assert prefix in here.parents, f"flowstate came from {here}, not the temp venv {prefix}"
    assert not (here.parent / "pyproject.toml").exists(), "this is the source tree, not an install"
    assert database.schema_path() == database.PACKAGE_SCHEMA_PATH, database.schema_path()
    assert database.schema_path().is_file()
    assert (here / "release_signers").is_file()
    assert [v for v, _n, _p in migrations.available()] == list(range(1, migrations.latest_version() + 1))

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    db = Path(os.environ["FLOWSTATE_DB"])
    assert not db.exists()

    def data(res):
        assert not res.isError, res.content[0].text
        return json.loads(res.content[0].text)

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "flowstate"],
                                       env=dict(os.environ), cwd=os.getcwd())
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                assert init.serverInfo.version == flowstate.__version__, init.serverInfo.version
                first = await s.call_tool("start_here", {})
                assert not first.isError, first.content[0].text
                assert "flowstate_unavailable" not in first.content[0].text
                data(await s.call_tool("manage_projects", {"action": "create", "name": "Wheel",
                                                           "description": "wheel install test"}))
                one = data(await s.call_tool("log_work", {"project": "Wheel", "kind": "decision",
                                                          "summary": "Keep the notes in plain text files"}))
                old = one["record"]["id"]
                two = data(await s.call_tool("log_work", {"project": "Wheel", "kind": "decision",
                                                          "summary": "Keep the notes in SQLite",
                                                          "replaces": old}))
                assert two["replaces"] == old
                brief = await s.call_tool("start_here", {"project": "Wheel"})
                text = brief.content[0].text
                assert not brief.isError and text.startswith("FlowState brief: Wheel"), text[:300]
                assert "Keep the notes in SQLite" in text and f"replaces #{old}" in text, text
    asyncio.run(go())

    with sqlite3.connect(db) as conn:
        versions = [v for (v,) in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, migrations.latest_version() + 1)), versions
        assert conn.execute("SELECT count(*) FROM projects WHERE name = 'Wheel'").fetchone() == (1,)
    r = subprocess.run([sys.executable, "-m", "flowstate", "migrate", "--db", str(db)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "up to date" in r.stdout, r.stdout + r.stderr
    print("WHEEL-OK", flowstate.__version__)
''')


def _skip(what: str, r: subprocess.CompletedProcess):
    pytest.skip(f"{what} isn't possible here: {(r.stderr or r.stdout).strip()[-300:]}")


def _build_wheel(out: Path) -> Path:
    env = dict(os.environ, PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    env.pop("PYTHONPATH", None)
    try:
        import hatchling  # noqa: F401
        extra = ["--no-build-isolation", "--no-index"]
    except ImportError:
        extra = []   # pip's build isolation: needs hatchling from pip's cache or the network
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", *extra, "-w", str(out),
                        str(SERVER_DIR)], capture_output=True, text=True, timeout=600, env=env,
                       cwd=str(out))
    wheels = list(out.glob("flowstate-*.whl"))
    if r.returncode != 0 or len(wheels) != 1:
        _skip("building a wheel", r)
    return wheels[0]


def _borrowed_site_dirs() -> list:
    import mcp
    import pydantic
    return sorted({str(Path(m.__file__).resolve().parents[1]) for m in (mcp, pydantic)})


# .pth files from a borrowed site dir that the temp venv must still read. On Windows, mcp
# depends on pywin32 (mcp/os/win32/utilities.py imports pywintypes), and pywin32 is only
# importable through its own pywin32.pth: it adds win32, win32/lib and Pythonwin to sys.path and
# runs pywin32_bootstrap, which lets Windows find the DLLs in pywin32_system32. A real
# `pip install` of FlowState gets pywin32 and that .pth the normal way; only this test, which
# borrows site-packages through plain path lines, has to repeat it. (Other .pth files are left
# out on purpose: an editable FlowState's would put the source tree back on the path.)
BORROWED_PTH = ("pywin32.pth",)


def _deps_pth_lines(site_dirs) -> list:
    """The lines of the venv's zz_flowstate_test_deps.pth: each borrowed site dir, then what its
    pywin32.pth adds (folders that exist, made absolute; its `import` lines as they are), the
    way site.addpackage reads it, plus the pywin32_system32 DLL folder for Windows."""
    lines, imports = [], []
    for d in site_dirs:
        d = Path(d)
        lines.append(str(d))
        for name in BORROWED_PTH:
            pth = d / name
            if not pth.is_file():
                continue
            for raw in pth.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("import ", "import\t")):
                    imports.append(line)
                    continue
                sub = d / line.replace("\\", "/")
                if sub.is_dir() and str(sub) not in lines:
                    lines.append(str(sub))
        dlls = d / "pywin32_system32"
        if os.name == "nt" and dlls.is_dir():
            imports.append(f"import os; os.add_dll_directory({str(dlls)!r})")
    # import lines last, so every folder above is on sys.path when they run
    return lines + [i for n, i in enumerate(imports) if i not in imports[:n]]


def test_borrowed_pywin32_pth_is_repeated_in_the_temp_venv(tmp_path):
    """Windows CI: mcp imports pywintypes, which the temp venv only finds if the borrowed
    pywin32.pth is repeated (a fake pywin32 layout here, so it runs on every OS)."""
    site_dir = tmp_path / "site-packages"
    for sub in ("win32/lib", "Pythonwin", "pywin32_system32"):
        (site_dir / sub).mkdir(parents=True)
    (site_dir / "pywin32.pth").write_text(
        "# .pth file for the PyWin32 extensions\nwin32\nwin32\\lib\nPythonwin\nmissing_dir\n"
        "# hackery\nimport fs_fake_bootstrap\n", encoding="utf-8")
    (site_dir / "win32" / "lib" / "fs_fake_pywintypes.py").write_text("OK = 1\n", encoding="utf-8")
    (site_dir / "win32" / "lib" / "fs_fake_bootstrap.py").write_text("RAN = 1\n", encoding="utf-8")
    (site_dir / "Pythonwin" / "fs_fake_win32ui.py").write_text("OK = 1\n", encoding="utf-8")
    lines = _deps_pth_lines([site_dir])
    assert lines[:4] == [str(site_dir), str(site_dir / "win32"), str(site_dir / "win32" / "lib"),
                         str(site_dir / "Pythonwin")]
    assert "import fs_fake_bootstrap" in lines and not any("missing_dir" in l for l in lines)
    assert lines[-1].startswith("import ")
    if os.name == "nt":
        assert f"import os; os.add_dll_directory({str(site_dir / 'pywin32_system32')!r})" in lines
    pth_dir = tmp_path / "venv-site"
    pth_dir.mkdir()
    (pth_dir / "zz_flowstate_test_deps.pth").write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import site, sys; site.addsitedir(sys.argv[1]); "
                        "import fs_fake_pywintypes, fs_fake_win32ui; "
                        "assert 'fs_fake_bootstrap' in sys.modules; print('PTH-OK')", str(pth_dir)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "PTH-OK" in r.stdout, r.stdout + r.stderr


def test_wheel_installs_and_works_without_the_repository(tmp_path):
    import flowstate
    work = tmp_path / "wheel-test"
    (work / "dist").mkdir(parents=True)
    wheel = _build_wheel(work / "dist")
    assert wheel.name == f"flowstate-{flowstate.__version__}-py3-none-any.whl"
    import zipfile
    names = set(zipfile.ZipFile(wheel).namelist())
    assert "flowstate/schema.sql" in names and "flowstate/release_signers" in names
    assert "flowstate/migrations/001_auto_sessions.sql" in names

    env_dir = work / "venv"
    try:
        venv.EnvBuilder(with_pip=True, clear=True).create(env_dir)
    except Exception as e:  # noqa: BLE001 - no ensurepip etc.
        pytest.skip(f"making a venv isn't possible here: {e}")
    py = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    clean = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
    r = subprocess.run([str(py), "-m", "pip", "install", "--no-deps", "--no-index", "-q", str(wheel)],
                       capture_output=True, text=True, timeout=600, env=dict(clean, PIP_NO_INPUT="1"))
    if r.returncode != 0:
        _skip("installing the wheel", r)
    purelib = subprocess.run([str(py), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                             capture_output=True, text=True, env=clean).stdout.strip()
    # mcp, pydantic and their dependencies from this environment, after the venv's own
    # site-packages (plain path lines: .pth files in there, e.g. an editable FlowState, are not
    # read, except pywin32's on Windows; see _deps_pth_lines)
    (Path(purelib) / "zz_flowstate_test_deps.pth").write_text("\n".join(_deps_pth_lines(_borrowed_site_dirs()))
                                                              + "\n", encoding="utf-8")

    run_dir = work / "run"
    data = run_dir / "data"
    data.mkdir(parents=True)
    (run_dir / "check.py").write_text(CHECK, encoding="utf-8")
    env = {k: v for k, v in clean.items() if not k.startswith("FLOWSTATE_")}
    env.update({"PYTHONPATH": str(GUARD_SITE),   # the real-data guard only; no source folder
                "FLOWSTATE_UPDATE_CHECK": "0", "FLOWSTATE_HOME": str(data),
                "FLOWSTATE_DB": str(data / "flowstate.db"),
                "FLOWSTATE_BACKUP_DIR": str(data / "backups"), "FLOWSTATE_CONFIG_DIR": str(data / "config"),
                "FLOWSTATE_AUTH_DB": str(data / "config" / "remote-auth.db"),
                "FLOWSTATE_RELAY_DIR": str(data / "relay"),
                "FLOWSTATE_TOKEN_STORE_FILE": str(data / "config" / "http-token"),
                "FLOWSTATE_REVIEW_PORT": "0",
                "FLOWSTATE_GUARD_DIRS": os.environ["FLOWSTATE_GUARD_DIRS"],
                "FLOWSTATE_GUARD_LOG": os.environ["FLOWSTATE_GUARD_LOG"]})
    r = subprocess.run([str(py), "check.py"], cwd=str(run_dir), env=env, capture_output=True, text=True,
                       timeout=600)
    assert r.returncode == 0 and "WHEEL-OK" in r.stdout, (r.stdout + r.stderr)[-3000:]
    shutil.rmtree(work, ignore_errors=True)
