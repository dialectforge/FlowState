"""The review page's port note follows FLOWSTATE_HOME (2.4.4). Temp folders only."""

from flowstate import paths, waiting


def test_review_page_state_file_follows_flowstate_home(tmp_path, monkeypatch):
    db = tmp_path / "x" / "flowstate.db"
    monkeypatch.setenv("FLOWSTATE_HOME", str(tmp_path / "home"))
    assert waiting._state_file(db).parent == tmp_path / "home" / "review-page"
    monkeypatch.delenv("FLOWSTATE_HOME")
    assert waiting._state_file(db).parent == paths.data_root() / "review-page"
