# loom-changes

loom's **Changes** piece: what's waiting to go up.

Every open change (a pull request, a merge request) in the repos your
projects name, grouped by project, with what's waiting on you first. Each
row says in words where it stands ("CI green, needs review", "CI red: build",
"Conflicts") and who it waits on: you, CI, a reviewer, its author, or the
agent that will merge it. Other people's changes are listed too, read-only.

Where a standing rule says a merge needs asking (the approvals piece), the
change's page has **Yes, merge it**, **Not yet** and **Always for
<project>**. The agent whose change it is does the merge; you never merge
from loom.

```
loom-changes sync [--once]          read the repos the project cards name (the service; --once: one read)
loom-changes refresh                ask the service for a read now
loom-changes list                   the open changes, from the snapshot
loom-changes cited-by t-123 --json  the changes that cite a task
loom-changes project-feed --json --project SLUG --since ISO [--repo R]…   a project hub's section
```

The design is loom's `docs/specs/changes.md`.

## Read-only, and holding no secret

- **It never writes to a forge.** Every forge call goes through one gate per
  adapter that refuses anything but a read before it runs. For GitHub that
  means `gh api` with GET, and `gh api graphql` with a query document (a
  mutation is refused). The tests check the gate, and they check every call a
  sync makes, by recording a stand-in `gh`.
- **It keeps no token.** A repo is read as the identity you configure for its
  owner (`CHANGES_IDENTITIES`). Its credential comes from the forge CLI's own
  store for that one call (`gh auth token --user <login>`), and goes into that
  call's environment only. It is never printed, logged, written or returned,
  and an error that echoes it is redacted. A change shows only what the forge
  showed that identity.
- **Its only writes** are its own snapshot, and on your buttons, an approval
  (Yes, Not yet) and a standing rule (Always), both made by the approvals
  piece through its own verbs.

## Forges

Changes reads forges through adapters (`lib/loom_changes/forges/`). Each
adapter answers in one shape, the *Change*: state, draft, author, head and
base, checks (`passing`, `failing`, `running` or `none`), review (`approved`,
`changes_requested`, `required` or `not_required`), mergeability, conflicts,
and people. No forge's own field names get past it. **GitHub** is the only
adapter so far. A Forgejo or Azure DevOps one is a new module with the same
four functions (`normalize`, `owner_key`, `list`, `read`) and a gate of its
own.

A project card's `links.repos` names repos as `<forge>:<locator>`
(`github:acme/app`). A bare `acme/app` means GitHub. Locators are literal:
segments of letters, digits, `.`, `_` and `-`.

## Who a change waits on

First match wins:

1. merged or closed: nobody
2. a draft: its author
3. conflicts, can't be merged, CI red, or changes asked for: its author
4. CI running, or conflicts not worked out yet: CI
5. review required: a reviewer
6. not an agent's change: its author ("Ready", or "Yours" for your own)
7. an agent's change, its rule `ask`, not yet allowed: **you**
8. otherwise: the agent, which merges it

"Ready" means CI is passing (or none is configured), and the review is
approved or not required. A change is an agent's when its author is one of
the agent's logins, or when its branch has the agent's prefix *and* its
author is you (an agent opening changes under your account). A branch name
alone never makes anyone else's change an agent's.

## Install

It's a loom piece: an npm package with a `loom-changes` bin and a `loom`
block in `package.json` (loom `docs/extensions.md`,
`docs/specs/pieces-install.md`).

```
loom pieces install --from github:sleepunit-agents/loom-changes#<40-char commit> --yes
```

It needs Python 3.9+ and the forge's CLI (`gh` for GitHub), and nothing else
(no npm dependencies). It declares one service, `sync`, which loom runs for
it (`loom-piece-changes-sync.service`).

## Settings

| Setting | Default | What it is |
|---|---|---|
| `CHANGES_DIR` | `${LOOM_PIECE_HOME}/data` | Where the snapshot lives (`snapshot.json`, 0600) |
| `CHANGES_INTERVAL` | `180` | Seconds between reads, clamped to 60 to 3600 |
| `CHANGES_IDENTITIES` | none | JSON: which forge login reads which owner, `{"github:acme": "octocat"}`. With none, the forge CLI's default |
| `CHANGES_AGENTS` | none | JSON: which changes are an agent's, `{"ada": {"logins": ["ada-bot"], "branches": ["ada/"]}}` |
| `CHANGES_OWNER_LOGINS` | none | JSON list of your own forge logins |
| `CHANGES_PROJECTS` | `loom project list --json` | The command that lists project cards |
| `CHANGES_TASKS` | `loom tasks` | The tasks piece's command. Its `export` is read once a sync, for tasks that cite a change |
| `CHANGES_GH` | `gh` | The GitHub CLI to run, when the `gh` on `PATH` is a wrapper |

None holds a secret.

## How it composes with loom

- **The page.** `ui --json` gives a tab (`Changes`, under Places, with a
  `git-pull-request` icon) and a tile ("2 waiting on you"). `ui page --json`
  draws the index (`""`) and a change (`c/<forge>/<locator>/<n>`) from the
  snapshot alone, so it answers well inside loom's 3 s. `ui act --json`
  takes Yes, Not yet, Always and Refresh, for the household's owner only:
  a caller of kind `owner` with every agent, as loom passes it for a signed-in
  member. With no caller (a loom without a household, or a run by hand) the
  buttons are absent and every action is refused: it fails closed. The piece
  has no API; `api --json` refuses every call. Each
  one refuses a head that moved since the page was drawn.
- **The approvals piece.** It's asked once per draw, for each agent with open
  changes: `approvals rule merge <target>… --for <agent> --json`. Yes and Not
  yet are `approvals grant --kind merge --target <id>@<head> --for <agent>
  --by <member> [--deny]`. Always is the approvals piece's own `rule` action,
  for `project:<slug>`, then the same grant. Without the approvals piece the
  buttons are absent, and the page says why.
- **A project's hub.** `sections.project` answers the hub's Changes section:
  a group per who it waits on, with "Waiting on you" marked `waiting`, so
  loom counts it on the Projects index.
- **Tasks.** A change's page lists the tasks it names (`t-123` in its title,
  body or branch) and the tasks that cite it (`owner/repo#n` in a
  description or comment). `cited-by` answers the other way, for the tasks
  piece.

## Tests

```
npm test            # unit tests, then test/smoke.sh
```

The unit tests run against a recorded GraphQL answer
(`test/fixtures/github-list.json`) and stand-ins for `gh` and the approvals
piece, which record what they were asked. `test/smoke.sh` runs the bin in a
clean environment (`env -i`, a throwaway home).

## License

AGPL-3.0-or-later, like loom and its other pieces. See [LICENSE](LICENSE).
