"""The Changes piece's shared collection (loom docs/specs/collections.md, "Declaring a provider"; t-2029): every change in
the snapshot as a record, `changes/change/<id>`, so loom's windows read one set wherever a change appears (Changes,
a project's page, a Comms thread's work panel), and this piece stays the authority.

    loom-changes collections query --json     loom's batch of queries, one viewer's, on stdin; JSON lines out

Who sees what: Changes' pages show every household owner the same changes (it doesn't declare owners: 2), so a
record's audience is household owners, and this provider answers no rows to anyone else; it decides for its viewer
before it pages. It has no change log: loom takes snapshots and works out what changed itself (the bridge). The sync
service rewrites the snapshot every CHANGES_INTERVAL seconds, and loom's next pull (15 s) carries it on.

A record's id is the change's own (`github:acme/app#3`) in the record grammar loom allows ([A-Za-z0-9._:-]):
`github:acme:app:3`. `ref` keeps the change's own form, which is how a thread names it. Nothing secret is a field: no
token, no credential, no body text (the page has it), and nothing that moves on every read with nothing changed (when
its repo was last read): a record moves only when the change does.
"""
import json
import re

VERSION = "1.0"
H = "household"
TYPE = {
    "version": VERSION,
    "schema": {"type": "object", "properties": {"title": {"type": "string"}, "state": {"type": "string"}, "ref": {"type": "string"}}},
    "fields": {
        "ref": {"class": H, "filter": ["eq", "in"]},
        "repo": {"class": H, "filter": ["eq", "in"], "order": True},
        "number": {"class": H, "order": True},
        "title": {"class": H, "search": True},
        "state": {"class": H, "filter": ["eq", "in"], "order": True},
        "draft": {"class": H, "filter": ["eq"]},
        "author": {"class": H, "filter": ["eq", "in"]},
        "agent": {"class": H, "filter": ["eq", "in", "has"]},
        "mine": {"class": H, "filter": ["eq"]},
        "projects": {"class": H},
        "head": {"class": H},
        "base": {"class": H, "filter": ["eq"]},
        "checks": {"class": H, "filter": ["eq", "in"]},
        "failing": {"class": H},
        "review": {"class": H, "filter": ["eq", "in"]},
        "mergeable": {"class": H, "filter": ["eq"]},
        "url": {"class": H},
        "page": {"class": H},
        "created": {"class": H, "filter": ["lt", "lte", "gt", "gte"], "order": True},
        "updated": {"class": H, "filter": ["lt", "lte", "gt", "gte"], "order": True},
        "closed": {"class": H, "filter": ["has", "gte"], "order": True},
    },
    "default_fields": ["ref", "title", "state", "checks", "review", "updated"],
    "max_page": 500,
    "aggregates": {"count": {"by": ["state"]}},
}
DECLARATION = {"query": ["loom-changes", "collections", "query", "--json"], "types": {"change": TYPE}}

ID_OK = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def record_id(change_id):
    """`github:acme/app#3` as a record id, `github:acme:app:3`; None when it can't be one."""
    rid = change_id.replace("/", ":").replace("#", ":")
    return rid if ID_OK.fullmatch(rid) else None


def sees(viewer):
    """loom itself, or a household owner: whom Changes' pages show."""
    v = viewer if isinstance(viewer, dict) else {}
    return v.get("kind") == "core" or (v.get("kind") == "member" and v.get("household") is True)


def records(snap, agent_of, is_owners):
    """Every change in the snapshot as its record (each id once: the first wins), with the projects whose cards name its repo."""
    if not snap:
        return []
    by_repo = {}
    for p in snap.get("projects") or []:
        for r in p.get("repos") or []:
            by_repo.setdefault(r, []).append(p.get("slug"))
    out, seen = [], set()
    for c in snap.get("changes") or []:
        rid = record_id(c.get("id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        checks = c.get("checks") or {}
        out.append({
            "id": rid, "ref": c["id"], "repo": c.get("repo"), "number": c.get("number"), "title": c.get("title") or "",
            "state": c.get("state"), "draft": bool(c.get("draft")), "author": (c.get("author") or {}).get("login"),
            "agent": agent_of(c), "mine": bool(is_owners(c)), "projects": [s for s in by_repo.get(c.get("repo"), []) if s],
            "head": (c.get("head") or {}).get("id"), "base": (c.get("base") or {}).get("ref"),
            "checks": checks.get("state"), "failing": list(checks.get("failing") or [])[:10],
            "review": (c.get("review") or {}).get("state"), "mergeable": c.get("mergeable"),
            "url": c.get("url") or None, "page": c.get("page"), "created": c.get("created"), "updated": c.get("updated"),
            "closed": c.get("closed"), "audience": {"owners": True},
        })
    return out


def _rank(v):
    return 0 if v is None else 1 if isinstance(v, bool) else 2 if isinstance(v, (int, float)) else 3


def _cmp(a, b):
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        return ra - rb
    if ra == 0:
        return 0
    return -1 if a < b else 1 if a > b else 0


def _matches(r, where):
    """loom's own matching (src/collections/memory.ts): a value of another kind never matches a comparison."""
    for c in where:
        v, op = r.get(c[0]), c[1]
        val = c[2] if len(c) > 2 else None
        if op == "has":
            ok = v is not None
        elif op == "eq":
            ok = _rank(v) == _rank(val) and _cmp(v, val) == 0
        elif op == "in":
            ok = any(_rank(v) == _rank(x) and _cmp(v, x) == 0 for x in (val or []))
        elif op == "prefix":
            ok = isinstance(v, str) and isinstance(val, str) and v.startswith(val)
        elif op in ("lt", "lte", "gt", "gte"):
            c0 = _cmp(v, val)
            ok = _rank(v) == _rank(val) and {"lt": c0 < 0, "lte": c0 <= 0, "gt": c0 > 0, "gte": c0 >= 0}[op]
        else:
            ok = False
        if not ok:
            return False
    return True


def _key(r, order):
    return [r.get(f) for f, _ in order] + [r["id"]]


def _cmp_keys(a, b, order):
    for i, (x, y) in enumerate(zip(a, b)):
        c = _cmp(x, y)
        if c:
            return -c if i < len(order) and order[i][1] == "desc" else c
    return 0


def answer(batch, recs):
    """The answer to loom's batch, as the lines it reads: {"id", "row"} per row, then {"id", "more"}, or {"id", "error"}.
    Each query is loom's, already checked against the declaration. Rows are filtered for the viewer before they're
    paged, and carry only the fields asked for, the order's, their id and their audience."""
    import functools
    mine = recs if sees((batch or {}).get("viewer")) else []
    out = []
    for q in (batch or {}).get("queries") or []:
        qid = q.get("id")
        if q.get("type") != "change" or q.get("version") != 1:
            out.append(json.dumps({"id": qid, "error": "unsupported-version"}))
            continue
        order = [tuple(o) for o in q.get("order") or []]
        live = sorted((r for r in mine if _matches(r, q.get("where") or [])),
                      key=functools.cmp_to_key(lambda a, b: _cmp_keys(_key(a, order), _key(b, order), order)))
        start = 0
        after = q.get("after")
        if after is not None:
            if not isinstance(after, list) or len(after) != len(order) + 1:
                out.append(json.dumps({"id": qid, "error": "bad-query"}))
                continue
            start = next((i for i, r in enumerate(live) if _cmp_keys(_key(r, order), after, order) > 0), len(live))
        limit = int(q.get("limit") or 0)
        page = live[start:start + limit]
        send = {"id", "audience", *(q.get("fields") or []), *(f for f, _ in order)}
        for r in page:
            out.append(json.dumps({"id": qid, "row": {k: v for k, v in r.items() if k in send}}))
        # More only beside a full page, as asked.
        out.append(json.dumps({"id": qid, "more": len(page) == limit and start + limit < len(live)}))
    return out
