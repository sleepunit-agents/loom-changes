"""loom-changes: what's waiting to go up, across the forges a household's projects live on (loom docs/specs/changes.md).

Read-only toward every forge. The sync service reads the project cards and each repo they name into a snapshot;
pages and the hub feed draw from the snapshot alone, so they answer inside loom's 3 s. The owner's buttons (Yes,
merge it; Not yet; Always for <project>) go to the approvals piece; the agent whose change it is does the merge.
"""
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import forges


# ---------------------------------------------------------------- settings

def piece_home():
    h = os.environ.get("LOOM_PIECE_HOME")
    if h:
        return Path(h)
    here = Path(__file__).resolve().parent
    for d in [here, *here.parents][:10]:
        if (d / "piece.json").is_file() and (d / "versions").is_dir():
            return d
    return None


PIECE_HOME = piece_home()


def _owner_settings():
    try:
        d = json.loads((PIECE_HOME / "settings.json").read_text()) if PIECE_HOME else {}
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_SETTINGS = _owner_settings()


def setting(name, default=""):
    v = os.environ.get(name)
    if v in (None, ""):
        v = _SETTINGS.get(name)
    return v if isinstance(v, str) and v != "" else default


def json_setting(name, default):
    try:
        v = json.loads(setting(name, ""))
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


def state_dir():
    return Path(setting("CHANGES_DIR") or (PIECE_HOME / "data" if PIECE_HOME else Path.home() / ".local" / "state" / "loom-changes")).expanduser()


def interval():
    try:
        return max(60, min(3600, int(setting("CHANGES_INTERVAL", "180"))))
    except ValueError:
        return 180


def now():
    return datetime.now(timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(stamp):
    try:
        return datetime.strptime(str(stamp)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- projects and the snapshot

def projects():
    """The project cards: [{slug, name, repos: [repo id]}], in loom's order. A selector that isn't one is skipped."""
    cmd = setting("CHANGES_PROJECTS", "loom project list --json")
    try:
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        rows = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        rows = None
    if not isinstance(rows, list):
        raise RuntimeError("couldn't list the projects")
    out = []
    for p in rows:
        if not isinstance(p, dict) or not isinstance(p.get("slug"), str):
            continue
        repos = []
        for sel in ((p.get("links") or {}).get("repos") or []):
            try:
                rid = forges.repo_id(sel)
            except ValueError:
                continue
            if rid not in repos:
                repos.append(rid)
        out.append({"slug": p["slug"], "name": p.get("name") or p["slug"], "repos": repos})
    return out


def identity_for(rid):
    return json_setting("CHANGES_IDENTITIES", {}).get(forges.owner_key(rid))


def write_private(path, text):
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    with os.fdopen(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as f:
        f.write(text)
    tmp.replace(path)


def load():
    try:
        d = json.loads((state_dir() / "snapshot.json").read_text())
        return d if isinstance(d, dict) and d.get("v") == 1 else None
    except (OSError, ValueError):
        return None


def sync_once(log=lambda m: None):
    """Read every repo the cards name, and replace the snapshot whole. A repo that fails keeps its last changes,
    marked with the error."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    with open(d / "sync.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = load() or {}
        cards = projects()
        repos = []
        for c in cards:
            repos += [r for r in c["repos"] if r not in repos]
        snap = {"v": 1, "at": iso(now()), "projects": cards, "repos": {}, "changes": []}
        for rid in repos:
            try:
                got = forges.adapter_of(rid).list(rid, identity_for(rid))
                snap["repos"][rid] = {"at": iso(now())}
                snap["changes"] += got
            except Exception as e:  # one repo's failure is that repo's alone
                why = str(e)[:300] or e.__class__.__name__
                snap["repos"][rid] = {"at": (old.get("repos", {}).get(rid) or {}).get("at"), "error": why}
                snap["changes"] += [c for c in old.get("changes", []) if c.get("repo") == rid]
                log(f"changes: {rid}: {why}")
        exported = tasks_export()
        snap["citing"] = tasks_index([c["id"] for c in snap["changes"]], exported)
        snap["sources"] = task_sources(exported)
        snap["trusted"] = trusted_index([c["id"] for c in snap["changes"]], exported)
        write_private(d / "snapshot.json", json.dumps(snap, indent=1) + "\n")
        return snap


def request_sync():
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(d / "refresh.request", iso(now()) + "\n")


def serve(stop=lambda: False):
    """The sync service: a sync every interval, or sooner when Refresh asks."""
    poke = state_dir() / "refresh.request"
    while not stop():
        try:
            sync_once(lambda m: print(m, file=sys.stderr, flush=True))
        except Exception as e:
            print(f"changes: sync failed: {e}", file=sys.stderr, flush=True)
        try:
            poke.unlink()
        except FileNotFoundError:
            pass
        deadline = time.monotonic() + interval()
        while time.monotonic() < deadline and not stop():
            if poke.exists():
                break
            time.sleep(2)


# ---------------------------------------------------------------- whose it is, and what it waits on

def agent_of(c):
    """The agent whose change it is: its author's login is listed, or its head ref starts with a listed prefix."""
    owners = json_setting("CHANGES_OWNER_LOGINS", [])
    for agent, how in json_setting("CHANGES_AGENTS", {}).items():
        if not isinstance(how, dict) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", agent):
            continue
        if c["author"]["login"] in (how.get("logins") or []):
            return agent
        # A branch prefix counts only under the owner's own login (an agent opening changes as the owner): a branch
        # name alone never makes someone else's change an agent's.
        if c["author"]["login"] in owners and any(isinstance(p, str) and p and c["head"]["ref"].startswith(p)
                                                   for p in (how.get("branches") or [])):
            return agent
    return None


def is_owners(c):
    return c["author"]["login"] in json_setting("CHANGES_OWNER_LOGINS", [])


def target_of(c):
    return f"{c['id']}@{c['head']['id']}"


def approvals_bin():
    own = setting("CHANGES_APPROVALS")
    if own:
        return own
    home = os.environ.get("LOOM_HOME") or str(Path.home() / ".config" / "loom")
    p = Path(home) / "pieces" / "bin" / "approvals"
    return str(p) if p.exists() else (shutil.which("approvals") or "")


def approvals(argv, stdin=None, timeout=2.5):
    b = approvals_bin()
    if not b:
        raise FileNotFoundError("the approvals piece isn't installed")
    # Run as the approvals piece, not as this one: without this piece's home in its environment it finds its own
    # (from where it's installed) and reads its own store and settings, never a store under this piece's data.
    env = {k: v for k, v in os.environ.items() if k not in ("LOOM_PIECE_HOME", "LOOM_PIECE_NAME") and not k.startswith("CHANGES_")}
    r = subprocess.run([b, *argv], input=stdin, capture_output=True, text=True, timeout=timeout, env=env,
                       stdin=None if stdin is not None else subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "approvals failed")
    return r.stdout


def rules_for(changes):
    """{target: {tier, scope?, approval?}} for every agent's open change, one approvals call per agent; None when the
    approvals piece can't say (its buttons are then absent)."""
    by_agent = {}
    for c in changes:
        a = agent_of(c)
        if a and c["state"] == "open" and re.fullmatch(r"[0-9a-f]{40}", c["head"]["id"]):
            by_agent.setdefault(a, []).append(target_of(c))
    out = {}
    try:
        for agent, ts in by_agent.items():
            for i in range(0, len(ts), 200):
                got = json.loads(approvals(["rule", "merge", *ts[i:i + 200], "--for", agent, "--json"]))
                if not isinstance(got, dict):
                    return None
                for t, r in got.items():
                    # An answer that isn't one reads as no answer: row 7, without buttons.
                    if not isinstance(r, dict) or r.get("tier") not in ("do", "tell", "ask"):
                        return None
                    ap = r.get("approval")
                    out[t] = {"tier": r["tier"], **({"approval": ap} if isinstance(ap, dict) and isinstance(ap.get("state"), str) else {})}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return None
    return out


# The index's order (changes.md, "Pages"): you, the agent, CI, a reviewer, an author; then what's done.
ORDER = ["you", "agent", "ci", "reviewer", "author", "done"]
GROUP = {"you": "Waiting on you", "agent": "Ready to merge", "ci": "Waiting on CI", "reviewer": "Waiting on a reviewer",
         "author": "Waiting on its author", "done": "Lately"}


def name_of(agent):
    return agent[:1].upper() + agent[1:] if agent else "An agent"


def who_name(p):
    return p.get("name") or p.get("login") or "someone"


def waits_on(c, rules):
    """(class, words) by the table in changes.md, first match winning."""
    if c["state"] in ("merged", "closed"):
        return "done", "Merged" if c["state"] == "merged" else "Closed"
    if c["draft"]:
        return "author", "Draft"
    if c["conflicts"]:
        return "author", "Conflicts"
    if c["mergeable"] == "no":
        return "author", "Can't be merged"
    if c["checks"]["state"] == "failing":
        return "author", "CI red"
    if c["review"]["state"] == "changes_requested":
        return "author", "Changes asked for"
    if c["checks"]["state"] == "running":
        return "ci", "CI running"
    if c["mergeable"] == "unknown":
        return "ci", "Checking for conflicts"
    if c["review"]["state"] == "required":
        return "reviewer", "Needs review"
    agent = agent_of(c)
    if not agent:
        return "author", "Yours" if is_owners(c) else "Ready"
    r = (rules or {}).get(target_of(c)) or {"tier": "ask"}
    ap = r.get("approval") or {}
    if r["tier"] == "ask" and ap.get("state") != "allowed":
        return "you", "You said not yet" if ap.get("state") == "denied" else "Waiting on you"
    return "agent", f"Ready for {name_of(agent)} to merge"


def state_line(c, words):
    ch = c["checks"]
    ci = {"passing": "CI green", "running": "CI running", "none": "no CI"}.get(ch["state"])
    if ch["state"] == "failing":
        ci = "CI red" + (f": {', '.join(ch['failing'][:3])}" if ch["failing"] else "")
    rv = {"approved": "approved", "required": "needs review", "changes_requested": "changes asked for"}.get(c["review"]["state"])
    parts = [ci, rv, "conflicts" if c["conflicts"] else None]
    if c["state"] != "open":
        parts = []
    have = {p.lower() for p in parts if p}
    if words.lower() not in have and not (words == "CI red" and ch["state"] == "failing"):
        parts.append(words)
    return ", ".join(p for p in parts if p) or words


def short(c):
    """on-pond#212: the repo's last segment and the number."""
    return f"{c['repo'].rsplit('/', 1)[-1]}#{c['number']}"


def ago(stamp, t=None):
    d = parse(stamp)
    if not d:
        return ""
    s = max(0, ((t or now()) - d).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} hour{'' if h == 1 else 's'} ago"
    days = int(s // 86400)
    return "yesterday" if days == 1 else f"{days} days ago" if days < 30 else "on " + d.strftime("%b %-d")


def initials(name):
    w = [x for x in re.split(r"[\s._-]+", name or "") if x]
    return ((w[0][:1] + (w[1][:1] if len(w) > 1 else "")) or "?").upper()


# ---------------------------------------------------------------- tasks cited (B2)

TASK = re.compile(r"(?<![A-Za-z0-9])t-([0-9]{1,7})(?![0-9])")


def tasks_cited(c):
    """t-NNN in the change's title, body or head ref, as a word."""
    seen = []
    for text in (c.get("title", ""), c["head"]["ref"], c.get("body", "")):
        for m in TASK.finditer(text or ""):
            t = f"t-{m.group(1)}"
            if t not in seen:
                seen.append(t)
    # Branches name a task as t1234 too (art/t1781-…): a head ref's leading t<digits> segment.
    for m in re.finditer(r"(?:^|/)t-?([0-9]{2,7})(?=[-_/]|$)", c["head"]["ref"]):
        t = f"t-{m.group(1)}"
        if t not in seen:
            seen.append(t)
    return seen[:20]


def short_forms(c):
    """The forms a task's text cites the change by: its id, and for GitHub owner/repo#n."""
    forms = [c["id"]]
    if c["repo"].startswith("github:"):
        forms.append(f"{c['repo'][len('github:'):]}#{c['number']}")
    return forms


def cited_by(task_id, snap):
    """The changes that cite a task, from the snapshot: [{id, title, words, page}]."""
    out = []
    citing = (snap or {}).get("citing") or {}
    for c in (snap or {}).get("changes", []):
        if task_id in tasks_cited(c) or any(t["id"] == task_id for t in citing.get(c["id"], [])):
            out.append({"id": c["id"], "title": c["title"], "short": short(c), "page": c["page"],
                        "words": waits_on(c, None)[1] if c["state"] != "open" else state_line(c, waits_on(c, None)[1])})
    return out


CITE = re.compile(r"(?<![A-Za-z0-9._/-])(?:([a-z][a-z0-9-]{0,23}):)?([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){1,3})#([1-9][0-9]{0,9})(?![0-9A-Za-z])")


def citations(text):
    """The change ids a task's text cites: <forge>:<locator>#n, or GitHub's short owner/repo#n."""
    out = []
    for m in CITE.finditer(text or ""):
        forge = m.group(1) or "github"
        try:
            rid = forges.repo_id(f"{forge}:{m.group(2)}")
        except ValueError:
            continue
        out.append(f"{rid}#{m.group(3)}")
    return out


def tasks_export():
    """The tasks piece's own export (read-only, `CHANGES_TASKS export`); {} when there's no tasks piece."""
    cmd = setting("CHANGES_TASKS", "loom tasks")
    try:
        r = subprocess.run([*shlex.split(cmd), "export"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        d = json.loads(r.stdout) if r.returncode == 0 else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return d if isinstance(d, dict) else {}


def task_sources(d):
    """{task id: cv-…} for the tasks made in a conversation (their source is conversation:cv-…; taskstore-core
    it-ts-source, loom comms.md "Made here"). Only the id, nothing else of the task."""
    out = {}
    for t in (d.get("tasks") or []) if isinstance(d, dict) else []:
        if isinstance(t, dict) and isinstance(t.get("id"), str):
            m = re.fullmatch(r"conversation:(cv-[0-9a-f]{12})", t.get("source") or "")
            if m:
                out[t["id"]] = m.group(1)
    return out


def trusted_actors():
    """Who a task comment may be by to vouch for a change: the household's agents (CHANGES_AGENTS' names) and the
    owner as the tasks piece names them (CHANGES_TASK_OWNER)."""
    agents = json_setting("CHANGES_AGENTS", {})
    owner = setting("CHANGES_TASK_OWNER", "").strip()
    return set(agents if isinstance(agents, dict) else []) | ({owner} if owner else set())


def trusted_index(change_ids, d):
    """{change id: [task id]} for the citations that may say where a change came from (loom comms.md, "Made here"):
    a task's description (the store writes it), and its comments by an agent or the owner. A change's own text, and
    anyone else's comment, never count: those are free text anyone with access can type."""
    wanted, out, who = set(change_ids), {}, trusted_actors()
    for t in (d.get("tasks") or []) if isinstance(d, dict) else []:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str):
            continue
        # A comment is {actor, at, text} (taskstore-core it-ts-annotate); an agent comments as its loom name.
        text = "\n".join([t.get("description") or ""] + [c.get("text") or "" for c in t.get("comments") or []
                                                        if isinstance(c, dict) and c.get("actor") in who])
        for cid in set(citations(text)) & wanted:
            out.setdefault(cid, []).append(t["id"])
    return out


def conversation_of(c, snap):
    """The conversation a change was made in, or None: only through a task that vouches for it (trusted_index) and
    whose source, which only an agent's own taskstore write sets, names a conversation. Nothing on the forge counts."""
    sources = (snap or {}).get("sources") or {}
    return next((sources[t] for t in ((snap or {}).get("trusted") or {}).get(c["id"], []) if t in sources), None)


def tasks_index(change_ids, d=None):
    """{change id: [{id, title, status}]} for the tasks whose description or comments cite a change, from the tasks
    piece's own export (read-only, `CHANGES_TASKS export`). {} when there's no tasks piece."""
    d = tasks_export() if d is None else d
    wanted, out = set(change_ids), {}
    for t in (d.get("tasks") or []) if isinstance(d, dict) else []:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str):
            continue
        text = "\n".join([t.get("description") or ""] + [(c or {}).get("text") or "" for c in t.get("comments") or []])
        for cid in set(citations(text)) & wanted:
            out.setdefault(cid, []).append({"id": t["id"], "title": (t.get("title") or t["id"])[:200], "status": t.get("status")})
    return out


# ---------------------------------------------------------------- loom's page

def find(snap, path):
    for c in (snap or {}).get("changes", []):
        if c["page"] == path:
            return c
    return None


def is_owner(req):
    """The household's owner, by loom's own rule (src/ui/pieces.ts, withGrant): a caller of kind owner whose agents
    are unrestricted (absent or "*"). Fails closed: no caller (a run that isn't loom's page for a signed-in member,
    or a loom without a household) is not the owner, so no button is drawn and no action runs."""
    c = (req or {}).get("caller")
    return isinstance(c, dict) and c.get("kind") == "owner" and c.get("agents") in (None, "*")


def stale_note(snap):
    at = parse((snap or {}).get("at"))
    if at and (now() - at).total_seconds() > 3 * interval():
        return {"type": "callout", "tone": "warn", "text": f"Last read {ago(snap['at'])}. Refresh asks for a fresh read."}
    return None


def row_of(c, rules):
    cls, words = waits_on(c, rules)
    asked = [who_name(p) for p in c["people"] if p["role"] == "requested"]
    line = state_line(c, words) + (f" from {', '.join(asked[:3])}" if cls == "reviewer" and asked else "")
    meta = " · ".join(x for x in (line, who_name(c["author"]), ago(c["updated"])) if x)
    item = {"text": f"{short(c)} {c['title']}", "meta": meta, "page": c["page"], "avatar": initials(who_name(c["author"]))}
    agent = agent_of(c)
    if agent:
        item = {k: v for k, v in item.items() if k != "avatar"}
        item["agent"] = agent
    if cls == "you":
        item["tone"] = "warn"
    elif words == "CI red" or words == "Conflicts":
        item["tone"] = "bad"
    return cls, item


def sort_key(c, rules):
    return (ORDER.index(waits_on(c, rules)[0]), -(parse(c["updated"]) or now()).timestamp())


def ui_manifest():
    snap = load()
    tab = {"label": "Changes", "icon": "git-pull-request", "accent": "blue"}
    if not snap:
        return {"v": 2, "tab": tab}
    open_ = [c for c in snap["changes"] if c["state"] == "open"]
    rules = rules_for(open_)
    mine = [c for c in open_ if waits_on(c, rules)[0] == "you"]
    tile = {"label": "Changes", "value": f"{len(mine)} waiting on you" if mine else f"{len(open_)} open",
            "detail": f"{len(open_)} open across {len(snap['repos'])} repos", "page": ""}
    if mine:
        tile["tone"] = "warn"
    return {"v": 2, "tab": tab, "tile": tile}


def index_page(req, snap, rules=False):
    q = (req or {}).get("query") or {}
    cards = snap["projects"]
    only = q.get("project", "")
    if rules is False:
        rules = rules_for([c for c in snap["changes"] if c["state"] == "open"])
    sections = []
    head = []
    note = stale_note(snap)
    if note:
        head.append(note)
    errs = [f"{rid.split(':', 1)[1]}: {r['error']}" for rid, r in snap["repos"].items() if r.get("error")]
    if errs:
        head.append({"type": "callout", "tone": "warn", "title": "Some repos couldn't be read", "text": "; ".join(errs)[:1900]})
    if rules is None:
        head.append({"type": "text", "tone": "muted", "text": "The approvals piece didn't answer, so nothing here can be said yes to right now."})
    opn = [c for c in snap["changes"] if c["state"] == "open"]
    yours = [c for c in opn if waits_on(c, rules)[0] == "you"]
    head.append({"type": "stats", "items": [{"label": "Waiting on you", "value": str(len(yours)), **({"tone": "warn"} if yours else {})},
                                            {"label": "Open", "value": str(len(opn))}]})
    if len(cards) > 1:
        head.append({"type": "filters", "param": "project", "options": [{"value": "", "label": "All"}] + [
            {"value": p["slug"], "label": p["name"]} for p in cards if p["repos"]][:12]})
    head.append({"type": "buttons", "items": [{"action": "refresh", "label": "Refresh"}]})
    sections.append({"id": "summary", "blocks": head})
    shown = [p for p in cards if p["repos"] and (not only or p["slug"] == only)]
    for p in shown[:8]:
        mine = sorted((c for c in snap["changes"] if c["repo"] in p["repos"]), key=lambda c: sort_key(c, rules))
        blocks = []
        live = [row_of(c, rules) for c in mine if c["state"] == "open"]
        you = [i for cls, i in live if cls == "you"]
        rest = [i for cls, i in live if cls != "you"]
        if you:
            blocks.append({"type": "list", "items": you, "wrap": True})
        if rest:
            blocks.append({"type": "list", "items": rest[:100], "wrap": True})
        if not live:
            blocks.append({"type": "text", "tone": "muted", "text": "Nothing open."})
        done = [row_of(c, rules)[1] for c in mine if c["state"] != "open"]
        if done:
            blocks.append({"type": "details", "summary": "Lately", "meta": f"{len(done)} merged or closed in 8 days",
                           "blocks": [{"type": "list", "items": done[:60]}]})
        sections.append({"id": f"p-{p['slug']}", "title": p["name"] + (f" · {len(you)} waiting on you" if you else ""),
                         **({"tone": "warn"} if you else {}), "blocks": blocks})
    if not shown:
        sections.append({"id": "none", "blocks": [{"type": "text", "tone": "muted",
                         "text": "No project names a repo yet. A project card's links.repos (github:owner/repo) is what's read."}]})
    return {"title": "Changes", "intro": "What's waiting to go up, by project. Art merges; you say yes where a rule asks you.",
            "sections": sections}


def change_page(req, snap, c):
    rules = rules_for([c]) if c["state"] == "open" else {}
    cls, words = waits_on(c, rules)
    agent = agent_of(c)
    forge = forges.adapter_of(c["repo"])
    sections, top = [], []
    note = stale_note(snap)
    if note:
        top.append(note)
    top.append({"type": "health", "tone": {"you": "warn", "agent": "ok", "done": "ok"}.get(cls, "bad" if words in ("CI red", "Conflicts") else "muted"),
                "text": words, "detail": state_line(c, words)})
    if cls == "you" and is_owner(req) and rules is not None:
        proj = next((p for p in snap["projects"] if c["repo"] in p["repos"]), None)
        f = {"id": c["id"], "head": c["head"]["id"]}
        top.append({"type": "text", "text": f"Saying yes lets {name_of(agent)} merge exactly this commit, {c['head']['id'][:7]}. A new push asks again."})
        top.append({"type": "buttons", "items": [
            {"action": "yes", "label": "Yes, merge it", "tone": "primary", "fields": f},
            {"action": "not-yet", "label": "Not yet", "fields": f},
            *([{"action": "always", "label": f"Always for {proj['name']}", "fields": f,
                "confirm": f"From now on {name_of(agent)} merges {proj['name']}'s changes once they're green, without asking."}] if proj else [])]})
    elif cls == "agent" and agent:
        r = (rules or {}).get(target_of(c)) or {}
        top.append({"type": "text", "tone": "muted", "text": (f"{name_of(agent)} merges this on its own once it's green" if r.get("tier") in ("do", "tell")
                                                              else f"You said yes; {name_of(agent)} merges it") + "."})
    elif cls == "you" and rules is None and is_owner(req):
        top.append({"type": "text", "tone": "muted", "text": "The approvals piece didn't answer, so this can't be said yes to right now."})
    sections.append({"id": "state", "blocks": top})
    rule = (rules or {}).get(target_of(c)) or {}
    sections.append({"id": "rule", "title": "Standing rule", "blocks": [
        {"type": "text", "text": rule.get("tier", "Unavailable" if rules is None else "ask")} ]})
    cv = conversation_of(c, snap)
    if cv:
        # loom draws it: only the thread's own member sees its title and a link (loom comms.md, "From").
        sections.append({"id": "from", "place": "side", "blocks": [{"type": "from", "conversation": cv}]})
    ch = c["checks"]
    kv = [{"label": "Repo", "value": c["repo"].split(":", 1)[1]},
          {"label": "Branch", "value": f"{c['head']['ref']} into {c['base']['ref']}"},
          {"label": "Author", "value": who_name(c["author"]) + (f" ({name_of(agent)}'s)" if agent and who_name(c["author"]).lower() != agent else "")},
          {"label": "Opened", "value": ago(c["created"])}, {"label": "Updated", "value": ago(c["updated"])},
          {"label": "CI", "value": {"passing": f"Green ({ch['total']} checks)", "none": "No checks configured",
                                    "running": "Running: " + ", ".join(ch["running"][:5]),
                                    "failing": "Red: " + ", ".join(ch["failing"][:5])}[ch["state"]]},
          {"label": "Review", "value": {"approved": "Approved" + (f" by {', '.join(c['review']['by'])}" if c["review"]["by"] else ""),
                                        "required": "Needs review", "changes_requested": "Changes asked for",
                                        "not_required": "Not required"}[c["review"]["state"]]},
          {"label": "Merging", "value": "Conflicts with " + c["base"]["ref"] if c["conflicts"] else
                                        {"yes": "No conflicts", "no": "Can't be merged as it is", "unknown": "Not worked out yet"}[c["mergeable"]]}]
    sections.append({"id": "facts", "title": "Where it stands", "blocks": [{"type": "kv", "items": kv},
                     {"type": "link", "text": f"Open on {forge.LABEL}", "href": c["url"]}] if c["url"] else [{"type": "kv", "items": kv}]})
    tasks = [{"id": t, "title": t} for t in tasks_cited(c)]
    for t in (snap.get("citing") or {}).get(c["id"], []):
        if t["id"] not in [x["id"] for x in tasks]:
            tasks.append(t)
        else:
            next(x for x in tasks if x["id"] == t["id"])["title"] = t["title"]
    if tasks:
        sections.append({"id": "tasks", "title": "Tasks", "blocks": [{"type": "list", "items": [
            {"text": t["title"] if t["title"] != t["id"] else t["id"], "meta": t["id"] + (f" · {t['status']}" if t.get("status") else ""),
             "page": f"loom:p/tasks/task/{t['id']}"} for t in tasks[:20]]}]})
    if c["people"]:
        role = {"author": "opened it", "reviewer": "reviewed", "requested": "asked to review", "assignee": "assigned", "commenter": "commented"}
        sections.append({"id": "people", "title": "People", "blocks": [{"type": "list", "items": [
            {"text": who_name(p), "meta": role.get(p["role"], p["role"]), "avatar": initials(who_name(p))} for p in c["people"][:20]]}]})
    if c["body"]:
        sections.append({"id": "body", "title": "What it says", "fold": "closed", "blocks": [{"type": "text", "text": c["body"][:1990]}]})
    return {"title": short(c), "intro": c["title"], "sections": sections}


def split_page(req, snap, selected=None):
    """The same snapshot and reader, arranged as groups/list/reader. No new authority."""
    rules = rules_for([c for c in snap["changes"] if c["state"] == "open"])
    result = index_page(req, snap, rules)
    q = (req or {}).get("query") or {}
    only = q.get("project", "")
    repos = {r for p in snap["projects"] if not only or p["slug"] == only for r in p["repos"]}
    rows = []
    for c in sorted(snap["changes"], key=lambda c: sort_key(c, rules)):
        if c["repo"] not in repos:
            continue
        group, words = waits_on(c, rules)
        rows.append({"id": c["page"], "group": group, "title": c["title"], "repo": c["repo"].split(":", 1)[1],
                     "number": str(c["number"]), "author": who_name(c["author"]), "ago": ago(c["updated"]),
                     "status": state_line(c, words)})
    opened = change_page(req, snap, selected) if selected else None
    if opened:
        for section in opened["sections"]:
            if section["id"] == "body":
                section.pop("fold", None)
                section["title"] = "Summary"
    result["changes"] = {"groups": [{"id": g, "label": GROUP[g]} for g in ORDER], "rows": rows[:500],
                         "selected": selected["page"] if selected else "",
                         "reader": opened}
    # Keep the notices, project filter and refresh action from the existing page.
    result["sections"] = result["sections"][:1]
    return result


def ui_page(req):
    path = (req or {}).get("path", "")
    snap = load()
    if not snap:
        if path:
            return {"title": "Not found", "sections": []}
        return {"title": "Changes", "intro": "What's waiting to go up, by project.", "sections": [{"id": "first", "blocks": [
            {"type": "callout", "busy": True, "text": "Reading the repos your projects name for the first time…"},
            {"type": "buttons", "items": [{"action": "refresh", "label": "Refresh"}]}]}], "refresh": 10}
    if path == "":
        return split_page(req, snap)
    c = find(snap, path)
    return split_page(req, snap, c) if c else {"title": "Not found", "sections": []}


def caller_who(req):
    c = (req or {}).get("caller") or {}
    who = c.get("who") if isinstance(c, dict) and c.get("kind") == "owner" else None
    return who if isinstance(who, str) and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", who) else None


def ui_act(req):
    action = (req or {}).get("action")
    f = (req or {}).get("fields") or {}
    if not is_owner(req):
        return {"ok": False, "message": "That's the household owner's to say.", "page": ""}
    if action == "refresh":
        request_sync()
        return {"ok": True, "message": "Reading the repos again; it takes a minute.", "page": ""}
    if action not in ("yes", "not-yet", "always"):
        return {"ok": False, "message": "Changes has no such action.", "page": ""}
    snap = load()
    c = next((x for x in (snap or {}).get("changes", []) if x["id"] == f.get("id")), None)
    back = c["page"] if c else ""
    if not c or c["state"] != "open":
        return {"ok": False, "message": "That change isn't open any more.", "page": back}
    if c["head"]["id"] != f.get("head") or not re.fullmatch(r"[0-9a-f]{40}", f.get("head") or ""):
        return {"ok": False, "message": "A new commit arrived since this page was drawn; look again.", "page": back}
    agent = agent_of(c)
    if not agent:
        return {"ok": False, "message": "That isn't one of your agents' changes.", "page": back}
    by = caller_who(req)
    grant = ["grant", "--kind", "merge", "--target", target_of(c), "--for", agent] + (["--by", by] if by else [])
    try:
        if action == "always":
            proj = next((p for p in snap["projects"] if c["repo"] in p["repos"]), None)
            if not proj:
                return {"ok": False, "message": "No project names this repo.", "page": back}
            res = json.loads(approvals(["ui", "act", "--json"], stdin=json.dumps({
                "action": "rule", "fields": {"kind": "merge", "scope": f"project:{proj['slug']}", "tier": "do"},
                **({"caller": req["caller"]} if (req or {}).get("caller") is not None else {})}), timeout=10))
            if not res.get("ok"):
                return {"ok": False, "message": res.get("message") or "The rule wasn't set.", "page": back}
        out = approvals(grant + (["--deny"] if action == "not-yet" else []), timeout=10)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        return {"ok": False, "message": f"Approvals said: {e}", "page": back}
    aid = out.split(":")[0].strip()
    msg = {"yes": f"Yes: {name_of(agent)} may merge {short(c)} at {c['head']['id'][:7]} ({aid}).",
           "not-yet": f"Not yet: {name_of(agent)} won't merge {short(c)} at this commit ({aid}).",
           "always": f"From now on {name_of(agent)} just merges these, and may merge {short(c)} now ({aid})."}[action]
    return {"ok": True, "message": msg, "page": back}


# ---------------------------------------------------------------- the hub's section (loom docs/specs/projects.md)

def project_feed(args):
    snap = load()
    if not snap:
        return {"groups": []}
    repos = []
    for r in args.get("repo", []):
        try:
            repos.append(forges.repo_id(r))
        except ValueError:
            pass
    if not repos:
        # Before loom passes --repo, the card's own repos, as the snapshot read them.
        names = [args.get("project")] + args.get("name", [])
        repos = next((p["repos"] for p in snap["projects"] if p["slug"] in names), [])
    # --conversation (the project's Comms threads, loom comms.md "Project hubs") is accepted and adds nothing: a change
    # made in a thread counts only from a repo the project already names, and those are all listed here already.
    mine = [c for c in snap["changes"] if c["repo"] in repos]
    rules = rules_for([c for c in mine if c["state"] == "open"])
    groups = {}
    for c in sorted(mine, key=lambda c: sort_key(c, rules)):
        cls, words = waits_on(c, rules)
        title = f"{short(c)} {c['title']}"
        if len(title) > 200:
            title = title[:199].rsplit(" ", 1)[0] + "…"
        groups.setdefault(cls, []).append({"title": title, "when": iso(parse(c["updated"]) or now()),
                                          "who": [who_name(c["author"])[:80]], "status": words[:24], "ref": c["id"][:200],
                                          "page": c["page"]})
    out, total = [], 0
    for cls in ORDER:
        items = groups.get(cls, [])[:max(0, 50 - total)]
        if items:
            total += len(items)
            out.append({"title": GROUP[cls], **({"waiting": True} if cls == "you" else {}), "items": items})
    return {"groups": out[:8]}


# ---------------------------------------------------------------- a Comms thread's section (loom docs/specs/comms.md)

# The Words that read as a problem in a thread's panel (its "bad" tone).
PROBLEM_WORDS = ("CI red", "Conflicts", "Changes asked for", "Can't be merged")


def thread_item(c, rules, made):
    cls, words = waits_on(c, rules)
    title = f"{short(c)} {c['title']}"
    if len(title) > 200:
        title = title[:199].rsplit(" ", 1)[0] + "…"
    tone = "bad" if words in PROBLEM_WORDS else "warn" if cls == "you" else "ok" if c["state"] == "merged" else None
    it = {"title": title, "ref": c["id"][:200], "status": words[:24], "when": iso(parse(c["updated"]) or now()), "page": c["page"]}
    if tone:
        it["tone"] = tone
    if made:
        it["made"] = True
    return it


def thread_feed(args):
    """A Comms thread's changes (loom comms.md, "The thread feed"): each change ref it names, from the snapshot, and
    every change made in its conversation, marked made. A ref the snapshot doesn't hold is "Not tracked", opening on
    GitHub. At most 50 items, made ones first."""
    cv = args.get("conversation")
    snap = load() or {"changes": []}
    by_id = {c["id"]: c for c in snap["changes"]}
    made = [c for c in snap["changes"] if conversation_of(c, snap) == cv]
    # A ref as the thread wrote it (Acme/App#2) is the snapshot's id once normalised, as citations are (repo_id).
    named = []
    for r in args.get("ref", []):
        m = re.fullmatch(r"github:([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([1-9][0-9]{0,9})", r)
        if m:
            try:
                rid = f"{forges.repo_id('github:' + m.group(1))}#{m.group(2)}"
            except ValueError:
                continue
            if rid not in named:
                named.append(rid)
    rules = rules_for([c for c in made + [by_id[r] for r in named if r in by_id] if c["state"] == "open"])
    out, seen = [], set()
    for c in sorted(made, key=lambda c: -(parse(c["updated"]) or now()).timestamp()):
        out.append(thread_item(c, rules, True))
        seen.add(c["id"])
    for r in named:
        if r in seen:
            continue
        seen.add(r)
        if r in by_id:
            out.append(thread_item(by_id[r], rules, False))
        else:
            loc, n = r[len("github:"):].rsplit("#", 1)
            out.append({"title": f"{loc}#{n}"[:200], "ref": r[:200], "status": "Not tracked",
                        "href": f"https://github.com/{loc}/issues/{n}"})
    return {"items": out[:50]}


def thread_args(argv):
    out = {"ref": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--conversation" and i + 1 < len(argv):
            out["conversation"] = argv[i + 1]
            i += 2
        elif a == "--ref" and i + 1 < len(argv):
            if len(out["ref"]) < 200:
                out["ref"].append(argv[i + 1])
            i += 2
        else:
            i += 1
    return out


def feed_args(argv):
    out = {"name": [], "venue": [], "repo": [], "conversation": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--project", "--since", "--epic") and i + 1 < len(argv):
            out[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("--name", "--venue", "--repo", "--conversation") and i + 1 < len(argv):
            out[a[2:]].append(argv[i + 1])
            i += 2
        else:
            i += 1
    return out


# ---------------------------------------------------------------- the CLI

USAGE = """loom-changes: what's waiting to go up, across the forges your projects live on (read-only).

    loom-changes sync [--once]          read the repos the project cards name (the service; --once: one read)
    loom-changes refresh                ask the service for a read now
    loom-changes list                   the open changes, from the snapshot
    loom-changes cited-by t-123 --json  the changes that cite a task
    loom-changes project-feed --json --project SLUG --since ISO [--repo R]…   the hub's section
    loom-changes thread-feed --json --conversation CV [--ref R]…              a Comms thread's changes

loom's verbs: ui --json, ui page --json, ui act --json.
"""


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["ui"]:
        rest = [a for a in args[1:] if a != "--json"]
        req = json.loads(sys.stdin.read() or "{}") if rest else None
        fn = {(): lambda: ui_manifest(), ("page",): lambda: ui_page(req), ("act",): lambda: ui_act(req)}.get(tuple(rest))
        if not fn:
            sys.exit(2)
        print(json.dumps(fn()))
        return
    if args[:1] == ["api"]:
        # The piece API: Changes offers none. Every call is refused, owner's or agent's, with a caller or without,
        # so nothing here acts for anyone but loom's page (ui act).
        print(json.dumps({"status": 404, "body": {"error": "Changes has no API; its actions are the owner's, on loom's page"}}))
        return
    if args[:1] == ["project-feed"]:
        print(json.dumps(project_feed(feed_args(args[1:]))))
        return
    if args[:1] == ["thread-feed"]:
        a = thread_args(args[1:])
        if not re.fullmatch(r"cv-[0-9a-f]{12}", a.get("conversation") or ""):
            sys.exit("loom-changes: thread-feed --conversation cv-… [--ref github:owner/repo#n]…")
        print(json.dumps(thread_feed(a)))
        return
    if args[:1] == ["cited-by"]:
        t = next((a for a in args[1:] if a != "--json"), "")
        if not re.fullmatch(r"t-[0-9]+", t):
            sys.exit("loom-changes: cited-by t-123")
        print(json.dumps(cited_by(t, load())))
        return
    if args[:1] == ["sync"]:
        if "--once" in args:
            snap = sync_once(lambda m: print(m, file=sys.stderr))
            print(f"read {len(snap['repos'])} repos: {sum(1 for c in snap['changes'] if c['state'] == 'open')} open changes")
        else:
            serve()
        return
    if args[:1] == ["refresh"]:
        request_sync()
        print("asked for a read")
        return
    if args[:1] == ["list"]:
        snap = load() or {"changes": []}
        rules = rules_for([c for c in snap["changes"] if c["state"] == "open"])
        for c in sorted((c for c in snap["changes"] if c["state"] == "open"), key=lambda c: sort_key(c, rules)):
            print(f"{c['id']}  {state_line(c, waits_on(c, rules)[1])}  — {c['title']}")
        return
    print(USAGE.strip())
    if args and args[0] not in ("-h", "--help"):
        sys.exit(2)
