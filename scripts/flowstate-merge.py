#!/usr/bin/env python3
"""Three-way merge of two FlowState databases that diverged from a common ancestor.

Both machines descend from a February snapshot. Ids were handed out independently
afterwards, so the same number means different things on each side. This keeps the
MAC ids exactly as they are (project notes cite learning #245 and #261-266) and
renumbers the PC's own additions above them, remapping every foreign key to match.

Ancestral rows share ids across both machines, so a foreign key pointing at one
needs no remapping -- only references to PC-new rows move.

Re-runnable: pass --save-ancestor to write the merged result as the new baseline,
so the next run is still a true three-way merge rather than a guess.

  python3 flowstate-merge.py --anc A.db --mac M.db --pc P.db --out OUT.db [--apply]
"""
import argparse, os, shutil, sqlite3, sys, datetime

# parents before children: a row is inserted only after whatever it points at
ORDER = ["projects", "components", "problems", "solution_attempts", "solutions",
         "sessions", "changes", "todos", "conversations", "learnings",
         "attachments", "session_state", "project_methods"]

# column -> table it references. Anything not listed is left alone.
FK = {
    "components":        {"project_id": "projects", "parent_component_id": "components"},
    "problems":          {"component_id": "components"},
    "solution_attempts": {"problem_id": "problems", "parent_attempt_id": "solution_attempts"},
    "solutions":         {"problem_id": "problems", "winning_attempt_id": "solution_attempts"},
    "sessions":          {"project_id": "projects", "focus_component_id": "components",
                          "focus_problem_id": "problems"},
    "changes":           {"component_id": "components"},
    "todos":             {"project_id": "projects", "component_id": "components",
                          "blocked_by_problem_id": "problems"},
    # conversations.session_id holds a TEXT label ("session-18"), not a row id -- never remap it
    "conversations":     {"project_id": "projects"},
    "learnings":         {"project_id": "projects", "component_id": "components"},
    "attachments":       {"project_id": "projects", "component_id": "components",
                          "problem_id": "problems"},
    "session_state":     {"project_id": "projects", "previous_state_id": "session_state"},
    "project_methods":   {"project_id": "projects", "related_component_id": "components"},
}


def cols(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def ids(con, table):
    try:
        return {r[0] for r in con.execute(f"SELECT id FROM {table}")}
    except sqlite3.Error:
        return set()


# the column that says what a row *is*. Two rows sharing an ancestral id but carrying
# different values here are different things that happen to collide on a number.
IDENTITY = {"projects": "name", "components": "name", "problems": "title",
            "solutions": "summary", "solution_attempts": "description",
            "sessions": "summary", "todos": "title", "learnings": "insight",
            "project_methods": "name"}


def ancestral_matches(anc, pc, table):
    """Ancestral ids the PC still agrees with. An id the PC has REPURPOSED -- same
    number, different thing, like project 9 being 'Web App' here and 'Helper AI'
    there -- is deliberately excluded, so it gets imported as a new row instead of
    being silently dropped. Compared on the identity column only: 8 components and
    7 todos differ from the ancestor in nothing but a timestamp, and duplicating
    those would be wrong."""
    col = IDENTITY.get(table)
    anc_ids = ids(anc, table)
    if not col:
        return anc_ids
    try:
        A = dict(anc.execute(f"SELECT id, {col} FROM {table}"))
        P = dict(pc.execute(f"SELECT id, {col} FROM {table}"))
    except sqlite3.Error:
        return anc_ids
    return {i for i in anc_ids if i in P and (P[i] or '') == (A.get(i) or '')}


def merge(anc_path, mac_path, pc_path, out_path, verbose=True):
    # the merged database starts as the Mac's, so Mac ids and rows are untouched
    shutil.copyfile(mac_path, out_path)
    out = sqlite3.connect(out_path)
    anc = sqlite3.connect(f"file:{anc_path}?mode=ro", uri=True)
    pc = sqlite3.connect(f"file:{pc_path}?mode=ro", uri=True)
    out.execute("PRAGMA foreign_keys=OFF")

    idmap = {t: {} for t in ORDER}   # table -> {pc_id: new_id}
    report = []

    for t in ORDER:
        anc_ids = ancestral_matches(anc, pc, t)
        pc_rows_new = [r for r in pc.execute(f"SELECT * FROM {t}")
                       if r[0] not in anc_ids]
        if not pc_rows_new:
            report.append((t, 0, 0, 0))
            continue
        c = cols(pc, t)
        out_c = cols(out, t)
        shared_cols = [x for x in c if x in out_c]
        dropped = [x for x in c if x not in out_c]
        nxt = (out.execute(f"SELECT COALESCE(MAX(id),0) FROM {t}").fetchone()[0]) + 1
        for r in pc_rows_new:                      # allocate first, so self-references resolve
            idmap[t][r[0]] = nxt
            nxt += 1
        placeholders = ",".join("?" * len(shared_cols))
        for r in pc_rows_new:
            row = dict(zip(c, r))
            row["id"] = idmap[t][r[0]]
            for col, target in FK.get(t, {}).items():
                v = row.get(col)
                if v is None:
                    continue
                # ancestral ids are identical on both machines -- only PC-new ids move
                if v in idmap.get(target, {}):
                    row[col] = idmap[target][v]
            out.execute(f"INSERT INTO {t} ({','.join(shared_cols)}) VALUES ({placeholders})",
                        [row[x] for x in shared_cols])
        report.append((t, len(pc_rows_new), len(anc_ids), len(dropped)))
        if dropped and verbose:
            print(f"  ! {t}: PC columns not present on the Mac, skipped: {dropped}")

    # sqlite_sequence drives future AUTOINCREMENT ids; leave it above the new maximum
    for t in ORDER:
        mx = out.execute(f"SELECT COALESCE(MAX(id),0) FROM {t}").fetchone()[0]
        out.execute("UPDATE sqlite_sequence SET seq=? WHERE name=? AND seq<?", (mx, t, mx))

    rebuild_fts(out)
    out.commit()
    integrity = out.execute("PRAGMA integrity_check").fetchone()[0]
    out.close(); anc.close(); pc.close()
    return report, integrity


def rebuild_fts(out):
    """memory_fts is a derived search index. Copying rows would leave it pointing at
    the PC's old ids, so it is emptied and rebuilt from the merged content."""
    try:
        out.execute("DELETE FROM memory_fts")
    except sqlite3.Error:
        return 0
    n = 0
    feeds = [("learning", "learnings", "insight", "project_id"),
             ("problem", "problems", "description", None),
             ("solution", "solutions", "description", None),
             ("change", "changes", "description", None),
             ("todo", "todos", "task", "project_id")]
    for ctype, table, textcol, projcol in feeds:
        have = cols(out, table)
        if textcol not in have:
            textcol = next((x for x in ("description", "insight", "task", "title", "content")
                            if x in have), None)
            if not textcol:
                continue
        pc_expr = projcol if (projcol and projcol in have) else "NULL"
        rows = out.execute(f"SELECT id, {pc_expr}, {textcol} FROM {table}").fetchall()
        out.executemany(
            "INSERT INTO memory_fts (content_type, content_id, project_id, searchable_text) "
            "VALUES (?,?,?,?)",
            [(ctype, r[0], r[1], r[2] or "") for r in rows])
        n += len(rows)
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--anc", required=True); p.add_argument("--mac", required=True)
    p.add_argument("--pc", required=True);  p.add_argument("--out", required=True)
    p.add_argument("--save-ancestor", help="write the merged result here as the next baseline")
    a = p.parse_args()

    for f in (a.anc, a.mac, a.pc):
        if not os.path.exists(f):
            sys.exit(f"missing: {f}")

    print(f"ancestor : {a.anc}\nmac      : {a.mac}\npc       : {a.pc}\nout      : {a.out}\n")
    report, integrity = merge(a.anc, a.mac, a.pc, a.out)

    print(f"{'table':20} {'imported':>9} {'ancestral':>10}")
    total = 0
    for t, imported, ancestral, _ in report:
        print(f"{t:20} {imported:>9} {ancestral:>10}")
        total += imported
    print(f"{'TOTAL':20} {total:>9}")

    out = sqlite3.connect(f"file:{a.out}?mode=ro", uri=True)
    print(f"\nintegrity_check: {integrity}")
    print(f"{'table':20} {'merged rows':>12}")
    for t in ORDER:
        print(f"{t:20} {out.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>12}")
    print("\nspot-check -- these must still be the LiveFlyer learnings:")
    for i in (245, 261, 266, 268):
        r = out.execute("SELECT id, substr(insight,1,64) FROM learnings WHERE id=?", (i,)).fetchone()
        print("   ", r)
    out.close()

    if a.save_ancestor:
        shutil.copyfile(a.out, a.save_ancestor)
        print(f"\nnew baseline written: {a.save_ancestor}")


if __name__ == "__main__":
    main()
