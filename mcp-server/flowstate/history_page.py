"""The review page's "Project history" views (task 73), read-only:

  ?view=history&project=ID   the project's whole path in time order (project_history.timeline):
                             filters by type, app, date range, a search box, oldest or newest
                             first, 50 entries a page
  ?view=todos&project=ID     the project's todos with who created each and its status now
  ?view=record&project=ID&type=T&id=N
                             one record: its fields now, its whole edit history (before and
                             after), and every entry about it (a todo's claims too)

Rendered by review_page.Page on the page `flowstate review-page` opens and on the open-link
page (same session cookie as the rest of the page). Never on the tunnel page (Page.settings
"off"), never through an MCP tool or the relay. No forms here change anything: no Undo, no
approve; those stay in the review queue and "Who did what".
"""

from typing import Optional
from urllib.parse import urlencode

from . import project_history as H
from . import who

VIEWS = ("history", "todos", "record")
NOT_HERE = "Project history is only on the review page on this computer."
_STATUS_WORDS = {"open": "Open", "done": "Done", "cancelled": "Cancelled", "deleted": "Deleted",
                 "all": "All"}


def _int(v) -> Optional[int]:
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


def _who_line(e, x: dict) -> str:
    """by APP · model M · on MACHINE · signed in: HOW (no model / machine when not recorded)."""
    bits = [f"by {e(x.get('who') or 'not recorded')}"]
    if x.get("model"):
        bits.append(f"model {e(x['model'])}")
    if x.get("machine"):
        bits.append(f"on {e(x['machine'])}")
    bits.append(f"sign-in: {e(x.get('identity') or 'not recorded')}")
    return " · ".join(bits)


def _state(e, state: str) -> str:
    if state in ("deleted", "erased"):
        return f" <span class='state st-gone'>{e(state)}</span>"
    return ""


class Views:
    """The three views for one review Page (its base path, escaping, header, projects)."""

    def __init__(self, page, e):
        self.p, self.e = page, e

    def url(self, view: str, **kw) -> str:
        args = {"view": view, **{k: v for k, v in kw.items() if v not in (None, "")}}
        return self.e(f"{self.p.base}/?{urlencode(args)}")

    def _rec_link(self, pid: int, rtype, rid, label: str, state: str) -> str:
        e = self.e
        if rtype is None or rid is None:
            return ""
        text = e(label if state == "erased" else (label[:160] + ("..." if len(label) > 160 else "")))
        name = f"{e(rtype)} #{e(rid)}"
        if rtype in ("session", "review_mode_change", "project") or not str(rid).isdigit():
            return f"<b>{name}</b> {text}{_state(e, state)}"
        return (f"<a href='{self.url('record', project=pid, type=rtype, id=rid)}'>{name}</a> "
                f"{text}{_state(e, state)}")

    def _project_bar(self, view: str, pid: Optional[int], projects: list) -> str:
        e = self.e
        opts = "".join(f'<option value="{p["id"]}"{" selected" if p["id"] == pid else ""}>{e(p["name"])}</option>'
                       for p in projects)
        tabs = ""
        if pid is not None:
            tabs = (f"<p class='tabs'><a href='{self.url('history', project=pid)}'>Timeline</a>"
                    f"{' (this page)' if view == 'history' else ''} · "
                    f"<a href='{self.url('todos', project=pid)}'>Todos</a>"
                    f"{' (this page)' if view == 'todos' else ''}</p>")
        back = "todos" if view == "todos" else "history"
        return (f"<form method='get' class='row'><input type='hidden' name='view' value='{back}'>"
                f"<select name='project'><option value=''>Pick a project</option>{opts}</select>"
                f"<button>Show</button></form>{tabs}")

    def _pages(self, view: str, res: dict, keep: dict) -> str:
        if res["pages"] <= 1:
            return ""
        nav = []
        if res["page"] > 1:
            nav.append(f"<a href='{self.url(view, **keep, page=res['page'] - 1)}'>Previous</a>")
        nav.append(f"page {res['page']} of {res['pages']}")
        if res["page"] < res["pages"]:
            nav.append(f"<a href='{self.url(view, **keep, page=res['page'] + 1)}'>Next</a>")
        return f"<p class='pager'>{' · '.join(nav)}</p>"

    def _entries(self, pid: int, rows: list) -> str:
        e = self.e
        out = ["<ol class='tl'>"]
        for r in rows:
            rec = self._rec_link(pid, r["record_type"], r["record_id"], r["label"] or "", r["state"])
            other = ""
            if r.get("other"):
                o = r["other"]
                other = f"<div class='rel'>with {self._rec_link(pid, o['record_type'], o['record_id'], o['label'] or '', o['state'])}</div>"
            out.append(f"<li><div class='when'>{e(r['at'])} · <span class='tag'>{e(H.TYPE_WORDS.get(r['type'], r['type'] or ''))}</span></div>"
                       f"<div class='what'>{e(r['what'])}</div><div class='rec'>{rec}</div>{other}"
                       f"<div class='who'>{_who_line(e, r)}</div></li>")
        out.append("</ol>")
        return "".join(out)

    def _faded(self, res: dict) -> str:
        f = res.get("faded")
        if not f:
            return ""
        return (f"<p class='fade'>Older detail has faded. FlowState keeps its activity log and the "
                f"link between a change and its session for {f['days']} days, so entries from "
                f"before {self.e(f['before'])} may not say which app or session made them, and "
                f"records from before version 2.4.0 never did (shown as \"not recorded\"). The "
                f"edit history itself (every created, edited, deleted and undone record, before "
                f"and after) is kept.</p>")

    # ------------------------------------------------------------------ views
    def render(self, csrf: str, flash: str, q: dict) -> str:
        e, p = self.e, self.p
        q = {k: (v or "").strip() for k, v in (q or {}).items()}
        view = q.get("view") if q.get("view") in VIEWS else "history"
        out = p._head(flash, view, csrf)
        if p.settings == "off":   # the tunnel page: not here (create_app never routes it either)
            out.append(f"<p class='err'>{e(NOT_HERE)}</p></body></html>")
            return "".join(out)
        projects = p.projects()
        pid = _int(q.get("project"))
        if pid is None and len(projects) == 1:
            pid = projects[0]["id"]
        if pid is not None and pid not in {x["id"] for x in projects}:
            pid = None
        out.append(f"<h2>Project history</h2>{self._project_bar(view, pid, projects)}")
        if pid is None:
            out.append("<p>Pick a project to see its whole history: every record created, "
                       "edited, replaced, undone or erased, review items, conflicts, todo claims "
                       "and sessions, with which app, model and machine did each.</p></body></html>")
            return "".join(out)
        try:
            if view == "todos":
                out.append(self._todos(pid, q))
            elif view == "record":
                out.append(self._record(pid, q))
            else:
                out.append(self._timeline(pid, q))
        except (H.HistoryPageError, who.WhoError) as err:
            out.append(f"<p class='err'>{e(err.payload['message'])}</p>")
        out.append("</body></html>")
        return "".join(out)

    def _timeline(self, pid: int, q: dict) -> str:
        e = self.e
        newest = q.get("order") == "newest"
        res = H.timeline(self.p.db_path, pid, q.get("type") or None, q.get("app"), q.get("since"),
                         q.get("until"), q.get("q"), page=_int(q.get("page")) or 1,
                         newest_first=newest)
        types = "".join(f'<option value="{e(t)}"{" selected" if t == q.get("type") else ""}>'
                        f'{e(H.TYPE_WORDS.get(t, t))}</option>' for t in res["types"])

        def field(n: str, ph: str, kind: str = "text") -> str:
            return f'<input type="{kind}" name="{n}" placeholder="{ph}" value="{e(q.get(n, ""))}">'
        out = [f"""<form method="get" class="filters"><input type="hidden" name="view" value="history"><input type="hidden" name="project" value="{pid}">
<div class="row">{field('q', 'search', 'search')}<select name="type"><option value="">All types</option>{types}</select>{field('app', 'app or machine')}</div>
<div class="row">{field('since', 'from (2026-10-17)')}{field('until', 'to')}<select name="order"><option value="oldest">Oldest first</option><option value="newest"{' selected' if newest else ''}>Newest first</option></select><button>Filter</button></div></form>""",
               f"<p class='meta'>{res['total']} entr{'y' if res['total'] == 1 else 'ies'}. Times are "
               f"this computer's local time. Model is shown only when the app gave it. Tap a "
               f"record for its full edit history. Read-only: approve and undo stay in the review "
               f"queue and Who did what.</p>", self._faded(res)]
        if not res["rows"]:
            out.append("<p>Nothing here.</p>")
        out.append(self._entries(pid, res["rows"]))
        keep = {k: q.get(k) for k in ("type", "app", "since", "until", "q", "order")}
        out.append(self._pages("history", res, {"project": pid, **keep}))
        return "".join(out)

    def _todos(self, pid: int, q: dict) -> str:
        e = self.e
        status = q.get("status") if q.get("status") in H.TODO_STATUSES else "all"
        res = H.todos(self.p.db_path, pid, status, _int(q.get("page")) or 1)
        c = res["counts"]
        total = sum(c.values())
        tabs = " · ".join(
            (f"<b>{w}</b>" if s == status else f"<a href='{self.url('todos', project=pid, status=s)}'>{w}</a>")
            + f" ({total if s == 'all' else c.get(s, 0)})" for s, w in _STATUS_WORDS.items())
        out = [f"<h3>Todos</h3><p class='tabs'>{tabs}</p>",
               "<p class='meta'>Who created each todo, and its status now. Tap one for its whole "
               "timeline: edits, status changes, claims, completion, reopening, deletion.</p>"]
        if not res["todos"]:
            out.append("<p>Nothing here.</p>")
        out.append("<ol class='tl'>")
        for t in res["todos"]:
            st = t["status"] or ""
            badge = f"<span class='state{' st-gone' if t['state'] != 'live' else ''}'>{e(st)}</span>"
            claim = (f"<div class='who'>claimed by {e(t['claim']['by'])} until {e(t['claim']['until'])}</div>"
                     if t["claim"] else "")
            title = e(t["title"]) if t["state"] != "erased" else e(H.ERASED)
            out.append(f"<li><div><a href='{self.url('record', project=pid, type='todo', id=t['id'])}'>todo #{t['id']}</a> "
                       f"{title} {badge}{(' · ' + e(t['priority'])) if t['priority'] else ''}</div>"
                       f"<div class='who'>created {e(t['created_at'] or 'at a time not recorded')} "
                       f"{_who_line(e, t['creator'])}</div>{claim}"
                       f"{('<div class=when>last change ' + e(t['last_at']) + '</div>') if t['last_at'] else ''}</li>")
        out.append("</ol>")
        out.append(self._pages("todos", {"page": res["page"], "pages": res["pages"]},
                               {"project": pid, "status": status}))
        return "".join(out)

    def _record(self, pid: int, q: dict) -> str:
        e = self.e
        rtype, rid = q.get("type", ""), _int(q.get("id"))
        if not rtype or rid is None or len(rtype) > 30:
            return "<p class='err'>Pick a record from the timeline.</p>"
        d = H.record_detail(self.p.db_path, pid, rtype, rid)
        res = H.timeline(self.p.db_path, pid, record=(rtype, rid), page=_int(q.get("page")) or 1)
        if d["state"] in ("erased", "") and not res["total"] and not d["edits"]:
            return "<p>Nothing in this project's history names this record.</p>"
        out = [f"<p><a href='{self.url('history', project=pid)}'>Back to the timeline</a></p>",
               f"<h3>{e(rtype)} #{rid}{_state(e, d['state'])}</h3>"]
        if d["state"] == "erased":
            out.append(f"<p><b>{e(H.ERASED)}</b></p><p class='note'>This record was erased for "
                       f"good with <code>flowstate erase</code>. Its text and its edit history "
                       f"are gone; the entries below only say that something happened.</p>")
        elif d["label"]:
            out.append(f"<p><b>{e(d['label'])}</b></p>")
        if d["state"] == "deleted":
            out.append("<p class='note'>Deleted. The edit history below still has it, so an undo "
                       "can bring it back (from Who did what, or by asking the app).</p>")
        if d["fields"]:
            out.append("<h4>Now</h4><p class='meta'>As stored (times here are UTC).</p><table class='fields'>" + "".join(
                f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in d["fields"].items()
                if v not in (None, "")) + "</table>")
        if d["edits"]:
            out.append(f"<h4>Edit history ({len(d['edits']) + d['more_edits']})</h4><ol class='tl'>")
            for x in d["edits"]:
                rows = "".join(f"<tr><th>{e(k)}</th><td class='old'>{e(v['old'])}</td>"
                               f"<td class='new'>{e(v['new'])}</td></tr>" for k, v in x["changes"].items())
                table = (f"<table class='diff'><tr><th>Field</th><th>Before</th><th>After</th></tr>"
                         f"{rows}</table>") if rows else ""
                out.append(f"<li><div class='when'>{e(x['at'])} · {e(x['tool'] or '')}</div>"
                           f"<div class='what'>{e(x['what'])}</div><div class='who'>{_who_line(e, x)}</div>"
                           f"{table}</li>")
            out.append("</ol>")
            if d["more_edits"]:
                out.append(f"<p class='meta'>{d['more_edits']} later edits are not shown here; "
                           f"filter the timeline by date to see them.</p>")
        elif d["state"] != "erased":
            out.append("<p class='meta'>No edit history for this record (it may be older than "
                       "version 2.4.0, or never edited through FlowState).</p>")
        out.append(f"<h4>Everything about it ({res['total']})</h4>{self._faded(res)}")
        out.append(self._entries(pid, res["rows"]))
        out.append(self._pages("record", res, {"project": pid, "type": rtype, "id": rid}))
        return "".join(out)
