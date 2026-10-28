"""What approving or rejecting a review item does, by kind (2.4.4).

One place for the two ways a decision reaches the queue from an app: manage_review approve /
reject (server._review_decide, which checks who may decide first) and the review card's
buttons (review_cards.resolve_card). Both get the same apply functions: a suggestion or
import becomes its record, a session summary the session's record, a clean-up, markdown edit,
delete, setting or "did you say this?" item is applied by its own module, a possible conflict
is confirmed or dismissed, a disagreement goes to its card's outcome.
"""

from pathlib import Path
from typing import Optional

from . import review


def approve(db_path: Path, item: dict, args: dict, user_request: str, client: str,
            done: dict, path: Optional[str] = None, move_to: Optional[int] = None) -> dict:
    """review.approve with every kind's apply function. `args` carries the optional edits
    (summary, details, priority, category, component), confirm_name and keep; `move_to` is
    a clean-up's resolved target project id.
    `done` collects extra answer fields (safety_copy, note, deleted). `path` names how the
    decision was made for a disagreement's own log (the card: "card"). Raises ReviewError,
    deletes.DeleteError, safety.SafetyError."""
    from . import card_offer, cleanup, conflict_cards, conflicts, deletes, history, provenance, \
        handoffs, review_mode, rules, safety
    from . import server as S

    def apply(item: dict, payload: dict):   # a source tag is the queuing app's say-so
        r = S._log_work(item["project_id"], payload, None, (item.get("app") or client, item.get("model")))
        if isinstance(r, dict) and "error" in r:
            raise review.ReviewError(r["error"], r.get("message", "could not create the record"))
        rec = r.get("record") if isinstance(r, dict) else None
        rid = rec.get("id") if isinstance(rec, dict) else None
        if rid is not None:
            S._safe_call(history.record_created, db_path, payload["kind"], rid, "approve", client)
        return S._RECORD_TABLE[payload["kind"]], rid

    def apply_cleanup(item: dict, _edits: dict):
        return cleanup.apply(db_path, item, client, move_to)

    def apply_edit(item: dict, _edits: dict):
        from .markdown_io import apply_edit as do
        return do(db_path, item, client)

    def apply_conflict(item: dict, _edits: dict):
        done["note"] = ("Kept as a possible conflict; it stays in the brief until a decision "
                        "replaces one of them (log_work replaces= or manage_links supersedes). "
                        "Ask the user which one stands.")
        return conflicts.confirm(db_path, item, client)

    def apply_delete(item: dict, _edits: dict):
        done.update(deletes.execute(db_path, item, client, args.get("confirm_name")))
        return deletes.TYPES[done["deleted"]["record_type"]][0], done["deleted"]["id"]

    def apply_setting(item: dict, _edits: dict):
        return review_mode.apply_queued(db_path, item, client)

    def apply_disagreement(item: dict, _edits: dict):
        return conflict_cards.apply_keep(db_path, item, args.get("keep"), client, path=path)

    edits = {k: args.get(k) for k in ("summary", "details", "priority", "category", "component")}
    if safety.needs_copy(item):   # a verified copy first, or no change at all
        done["safety_copy"] = safety.copy_before(db_path).name
    return review.approve(db_path, int(item["id"]), user_request, client, apply, edits,
                          others={"cleanup": apply_cleanup, "edit": apply_edit,
                                  "delete": apply_delete, "conflict": apply_conflict,
                                  "setting": apply_setting, "disagreement": apply_disagreement,
                                  "source": lambda it, _e: provenance.confirm(db_path, it),
                                  "handoff": lambda it, _e: handoffs.apply_queued(db_path, it),
                                  "card_offer": lambda it, _e: card_offer.apply(db_path, it, client),
                                  "rule": lambda it, _e: rules.confirm(
                                      db_path, it, path or _path_of(client))},
                          path=path or _path_of(client))


def reject(db_path: Path, item: dict, reason: str, user_request: str, client: str) -> dict:
    """review.reject, then what rejecting means for the kind: a possible conflict is dismissed
    (the two decisions fit together), a disagreement card is dismissed."""
    from . import conflict_cards, conflicts
    from . import server as S
    out = review.reject(db_path, int(item["id"]), reason, user_request, client)
    if out.get("kind") == "conflict":   # the user says the two decisions fit together
        out.update(conflicts.dismiss(db_path, int(item["id"]), client))
    elif out.get("kind") == "disagreement":
        out.update(S._safe_call(conflict_cards.on_reject, db_path, item, client) or {})
    return out


def _path_of(client: str) -> str:
    """How a decision without a named path was made: an AI in the chat ("agent"), else the
    user's surface named by `client` (review page, phone, terminal...)."""
    from . import approvals, identity
    return "agent" if identity.current() is not None else approvals.path_of(client)


def for_card(db_path: Path, client: str):
    """The review card's decide(item, decision, reason) (review_cards.resolve_card): the same
    approve / reject as manage_review, with the tap as the recorded request. Errors come back
    as their payload."""
    from . import deletes, safety

    def decide(item: dict, decision: str, reason: str) -> dict:
        from . import card_trust
        from .conflict_cards import CARD_WORDS
        words = CARD_WORDS.format(button="Approve" if decision == "approve" else "Reject",
                                  card="review", app=card_trust.shown(client))
        by = card_trust.label(client)   # "card (<app>)" like a record's own Keep / Remove
        try:
            if decision == "reject":
                return reject(db_path, item, reason, words, by)
            done: dict = {}
            return {**approve(db_path, item, {}, words, by, done, path="card"), **done}
        except (review.ReviewError, deletes.DeleteError, safety.SafetyError) as e:
            return e.payload
    return decide
