"""Forge adapters (loom docs/specs/changes.md, "The forge adapter").

An adapter reads one forge and answers in the Change shape's words only: no forge's own field names leave it.
Each one is a module with:

    FORGE                         its name, the repo id's prefix ("github")
    LABEL                         how the page names it ("GitHub")
    normalize(locator) -> str     the locator as stored (GitHub: lowercase), or raises ValueError
    owner_key(locator) -> str     what an identity is configured by ("onterris" for github:onterris/on-pond)
    list(repo_id, identity) -> [Change]     open changes, and those closed or merged in the last 8 days
    read(change_id, identity) -> Change | None
    people(change_id, identity) -> [Person]

Every call an adapter makes to its forge goes through its own read-only gate, which refuses anything but a read
before it runs. Nothing here writes.
"""
import re

from . import github

ADAPTERS = {github.FORGE: github}

FORGE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
# A locator is literal: segments of [A-Za-z0-9._-], so nothing in a change's id can read as a pattern.
SEGMENT = r"[A-Za-z0-9._-]+"
LOCATOR = re.compile(rf"^{SEGMENT}(?:/{SEGMENT}){{1,3}}$")
LOCATOR_MAX = 120


def split_repo(selector):
    """A project card's repo selector as (forge, locator): `github:acme/app`, or the bare `acme/app` (github's).
    Raises ValueError for anything else."""
    sel = str(selector or "").strip()
    head, colon, rest = sel.partition(":")
    if colon and "/" not in head:
        forge, loc = head, rest
    else:
        forge, loc = "github", sel
    if not FORGE.match(forge) or not LOCATOR.match(loc) or len(loc) > LOCATOR_MAX:
        raise ValueError(f"not a repo selector: {selector!r}")
    return forge, loc


def repo_id(selector):
    """The selector as a repo id, normalized by its adapter; ValueError for a forge with no adapter."""
    forge, loc = split_repo(selector)
    a = ADAPTERS.get(forge)
    if not a:
        raise ValueError(f"no adapter for {forge}")
    return f"{forge}:{a.normalize(loc)}"


def adapter_of(rid):
    return ADAPTERS[rid.split(":", 1)[0]]


def owner_key(rid):
    forge, _, loc = rid.partition(":")
    return f"{forge}:{ADAPTERS[forge].owner_key(loc)}"
