"""The GitHub adapter: reads pull requests with the `gh` CLI, read-only, as the identity configured for the owner.

It maps GitHub's fields to the Change shape (loom docs/specs/changes.md) and passes none of them through.
The credential: `gh auth token --user <identity>` from gh's own store, put in the one call's child environment as
GH_TOKEN, and never printed, logged, written or returned.
"""
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

FORGE = "github"
LABEL = "GitHub"
TIMEOUT = 20
BODY_MAX = 4000
RECENT_DAYS = 8

# Tests replace these.
RUN = subprocess.run


def gh():
    return os.environ.get("CHANGES_GH") or "gh"


TOKENISH = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


class NotARead(Exception):
    """A call that isn't a read: refused before it runs."""


def gate(argv):
    """The read-only rule: `gh api graphql` with a query document, or `gh api <path>` with GET. Anything else is
    refused before it runs: another subcommand, a method other than GET, fields on a REST call, a mutation."""
    if not argv or argv[0] != "api" or len(argv) < 2:
        raise NotARead("only gh api is a read here")
    i, method, fields, graphql = 1, None, [], argv[1] == "graphql"
    while i < len(argv):
        a = argv[i]
        if a in ("-X", "--method"):
            method = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1]
        elif a in ("-f", "-F", "--raw-field", "--field"):
            fields.append(argv[i + 1] if i + 1 < len(argv) else "")
            i += 2
            continue
        elif a.startswith(("--input", "--raw-field=", "--field=")):
            if a.startswith("--input"):
                raise NotARead("no --input: the document must be seen")
            fields.append(a.split("=", 1)[1])
        i += 1
    if any(f.split("=", 1)[-1].startswith("@") for f in fields):
        raise NotARead("no field from a file (@…): the document must be seen")
    if method and method.upper() != "GET":
        raise NotARead(f"method {method} isn't a read")
    if graphql:
        q = [f.split("=", 1)[1] for f in fields if f.startswith("query=")]
        if len(q) != 1:
            raise NotARead("graphql needs exactly one query document")
        if re.search(r"\bmutation\b", q[0], re.I) or re.search(r"\bsubscription\b", q[0], re.I):
            raise NotARead("a mutation isn't a read")
    elif fields:
        raise NotARead("a REST read takes no fields (they'd make it a POST)")
    return argv


def token_for(identity):
    if not identity:
        return None
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", identity):
        raise ValueError("an identity is a GitHub login")
    r = RUN([gh(), "auth", "token", "--user", identity], capture_output=True, text=True, timeout=TIMEOUT,
            stdin=subprocess.DEVNULL)
    tok = (r.stdout or "").strip()
    if r.returncode != 0 or not tok:
        # gh's own words could name the account but never the token: there is none to name.
        raise RuntimeError(f"gh has no token for {identity}")
    return tok


def call(argv, identity):
    """One read, as `identity`. Returns the parsed JSON. Errors never carry the token."""
    gate(argv)
    env = dict(os.environ)
    tok = token_for(identity)
    if tok:
        env["GH_TOKEN"] = tok
    try:
        r = RUN([gh(), *argv], capture_output=True, text=True, timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"GitHub didn't answer in {TIMEOUT} s") from None
    out, err = r.stdout or "", r.stderr or ""
    if tok:
        out, err = out.replace(tok, "[redacted]"), err.replace(tok, "[redacted]")
    # Whatever identity read (the CLI's default too): anything shaped like a GitHub token never leaves.
    err = TOKENISH.sub("[redacted]", err)
    if r.returncode != 0:
        line = next((x for x in err.splitlines() if x.strip()), "") or f"gh exited {r.returncode}"
        raise RuntimeError(line[:300])
    try:
        return json.loads(out)
    except ValueError:
        raise RuntimeError("GitHub's answer wasn't JSON") from None


def normalize(locator):
    if locator.count("/") != 1:
        raise ValueError("a GitHub repo is owner/repo")
    return locator.lower()


def owner_key(locator):
    return locator.split("/", 1)[0].lower()


FRAGMENT = """fragment pr on PullRequest { number title url state isDraft createdAt updatedAt closedAt mergedAt
  author { login ... on User { name } } headRefName headRefOid baseRefName bodyText mergeable reviewDecision
  reviewRequests(first: 10) { nodes { requestedReviewer { ... on User { login name } ... on Team { slug name } } } }
  latestReviews(first: 10) { nodes { state author { login ... on User { name } } } }
  assignees(first: 5) { nodes { login name } }
  participants(first: 15) { nodes { login name } }
  commits(last: 1) { nodes { commit { statusCheckRollup { state
    contexts(first: 60) { nodes { __typename ... on CheckRun { name status conclusion } ... on StatusContext { context state } } } } } } } }"""

LIST_QUERY = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {
  open: pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes { ...pr } }
  done: pullRequests(states: [MERGED, CLOSED], first: 20, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes { ...pr } } } }
""" + FRAGMENT

READ_QUERY = """query($owner: String!, $name: String!, $n: Int!) { repository(owner: $owner, name: $name) {
  pullRequest(number: $n) { ...pr } } }
""" + FRAGMENT

FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}


def _checks(node):
    commit = (((node.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
    roll = commit.get("statusCheckRollup")
    if not roll:
        return {"state": "none", "failing": [], "running": [], "total": 0}
    failing, running, total = [], [], 0
    for c in ((roll.get("contexts") or {}).get("nodes") or []):
        total += 1
        if c.get("__typename") == "CheckRun":
            name = c.get("name") or "a check"
            if c.get("status") != "COMPLETED":
                running.append(name)
            elif (c.get("conclusion") or "") in FAILED:
                failing.append(name)
        else:
            name = c.get("context") or "a status"
            st = c.get("state") or ""
            if st in ("PENDING", "EXPECTED"):
                running.append(name)
            elif st in ("FAILURE", "ERROR"):
                failing.append(name)
    st = roll.get("state")
    state = "failing" if st in ("FAILURE", "ERROR") or failing else "running" if st in ("PENDING", "EXPECTED") or running else "passing"
    return {"state": state, "failing": failing[:10], "running": running[:10], "total": total}


def _person(n, role):
    if not n:
        return None
    login = n.get("login") or n.get("slug")
    return {"login": login, **({"name": n["name"]} if n.get("name") else {}), "role": role} if login else None


def _body(text):
    t = (text or "").replace("\r\n", "\n").strip()
    if len(t) <= BODY_MAX:
        return t
    cut = t[:BODY_MAX]
    return cut[:cut.rfind("\n")] if "\n" in cut else cut


def to_change(rid, node):
    """One pull request, in the Change shape's words (changes.md). Nothing of GitHub's own naming passes through."""
    loc = rid.split(":", 1)[1]
    n = int(node["number"])
    state = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}.get(node.get("state"), "closed")
    reviews = [r for r in ((node.get("latestReviews") or {}).get("nodes") or []) if r]
    decision = node.get("reviewDecision")
    review = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested", "REVIEW_REQUIRED": "required"}.get(decision, "not_required")
    approved_by = [((r.get("author") or {}).get("name") or (r.get("author") or {}).get("login")) for r in reviews if r.get("state") == "APPROVED"]
    people, seen = [], set()

    def add(p):
        if p and p["login"] not in seen:
            seen.add(p["login"])
            people.append(p)
    add(_person(node.get("author"), "author"))
    for r in reviews:
        add(_person(r.get("author"), "reviewer"))
    for r in ((node.get("reviewRequests") or {}).get("nodes") or []):
        add(_person((r or {}).get("requestedReviewer"), "requested"))
    for a in ((node.get("assignees") or {}).get("nodes") or []):
        add(_person(a, "assignee"))
    for a in ((node.get("participants") or {}).get("nodes") or []):
        add(_person(a, "commenter"))
    author = node.get("author") or {}
    url = node.get("url") or ""
    return {
        "id": f"{rid}#{n}", "repo": rid, "number": n, "page": f"c/{FORGE}/{loc.lower()}/{n}",
        "title": (node.get("title") or "")[:300], "url": url if url.startswith("https://") else "",
        "state": state, "draft": bool(node.get("isDraft")),
        "author": {"login": author.get("login") or "ghost", **({"name": author["name"]} if author.get("name") else {})},
        "created": node.get("createdAt"), "updated": node.get("updatedAt"),
        "closed": node.get("mergedAt") or node.get("closedAt"),
        "head": {"ref": node.get("headRefName") or "", "id": (node.get("headRefOid") or "").lower()},
        "base": {"ref": node.get("baseRefName") or ""},
        "body": _body(node.get("bodyText")),
        "checks": _checks(node),
        "review": {"state": review, "by": [x for x in approved_by if x][:8]},
        "mergeable": {"MERGEABLE": "yes", "CONFLICTING": "no"}.get(node.get("mergeable"), "unknown"),
        "conflicts": node.get("mergeable") == "CONFLICTING",
        "people": people[:20],
    }


def _owner_name(rid):
    owner, name = rid.split(":", 1)[1].split("/", 1)
    return owner, name


def list(rid, identity=None, now=None):  # noqa: A001 (the adapter interface's word)
    owner, name = _owner_name(rid)
    d = call(["api", "graphql", "-f", f"query={LIST_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}"], identity)
    repo = ((d or {}).get("data") or {}).get("repository")
    if repo is None:
        raise RuntimeError("this identity can't see that repo")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=RECENT_DAYS)
    out = [to_change(rid, n) for n in ((repo.get("open") or {}).get("nodes") or []) if n]
    for n in ((repo.get("done") or {}).get("nodes") or []):
        c = to_change(rid, n) if n else None
        if c and c["closed"] and datetime.strptime(c["closed"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc) >= cutoff:
            out.append(c)
    return out


def read(change_id, identity=None):
    rid, _, n = change_id.rpartition("#")
    owner, name = _owner_name(rid)
    d = call(["api", "graphql", "-f", f"query={READ_QUERY}", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"n={int(n)}"], identity)
    node = ((((d or {}).get("data") or {}).get("repository") or {}).get("pullRequest"))
    return to_change(rid, node) if node else None


def people(change_id, identity=None):
    c = read(change_id, identity)
    return c["people"] if c else []
