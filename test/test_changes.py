"""Tests for loom-changes (loom docs/specs/changes.md, "Acceptance criteria", Changes piece).

    python3 -m unittest discover -s test -p 'test_*.py'

Nothing here reaches a forge: gh and the approvals piece are stand-ins in test/ that record what they were asked.
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

from loom_changes import app, forges  # noqa: E402
from loom_changes.forges import github  # noqa: E402

FIXTURE = HERE / "fixtures" / "github-list.json"
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
SHA = lambda n: f"{n:040x}"  # noqa: E731
RID = "github:acme/app"
OWNER = {"kind": "owner", "who": "olive-owner", "agents": "*"}


class Env(unittest.TestCase):
    """A sandbox: a state dir, the fake gh and approvals, one project naming acme/app."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        t = Path(self.td.name)
        self.cards = t / "cards.json"
        self.cards.write_text(json.dumps([{"slug": "app", "name": "The App", "links": {"repos": ["Acme/App"]}},
                                          {"slug": "other", "name": "Other", "links": {"repos": ["github:locked/repo", "not a repo"]}}]))
        self.env = {
            "CHANGES_DIR": str(t / "data"), "CHANGES_GH": str(HERE / "fake-gh"), "FAKE_GH_LOG": str(t / "gh.log"),
            "FAKE_GH_FIXTURE": str(FIXTURE), "CHANGES_PROJECTS": f"cat {self.cards}",
            "CHANGES_IDENTITIES": json.dumps({"github:acme": "ada-bot"}),
            "CHANGES_AGENTS": json.dumps({"ada": {"logins": ["ada-bot"], "branches": ["ada/"]}}),
            "CHANGES_OWNER_LOGINS": json.dumps(["octo-owner"]),
            "CHANGES_APPROVALS": str(HERE / "fake-approvals"), "FAKE_APPROVALS_LOG": str(t / "ap.log"),
            "CHANGES_TASKS": f"sh -c 'cat {t / 'tasks.json'}' tasks",
        }
        (t / "tasks.json").write_text(json.dumps({"tasks": [
            {"id": "t-900", "title": "Ship the thing", "status": "open", "description": "PR is acme/app#1, see it",
             "comments": [{"text": "also github:acme/app#2"}]},
            {"id": "t-901", "title": "Unrelated", "status": "open", "description": "acme/app#14x and acme/app#140", "comments": []}]}))
        self.p = mock.patch.dict(os.environ, self.env)
        self.p.start()
        self.np = mock.patch.object(app, "now", lambda: NOW)
        self.np.start()

    def tearDown(self):
        self.np.stop()
        self.p.stop()
        self.td.cleanup()

    def sync(self):
        with mock.patch.object(github, "datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            return app.sync_once()

    def change(self, n):
        return next(c for c in app.load()["changes"] if c["number"] == n)

    def gh_calls(self):
        return [json.loads(x) for x in Path(self.env["FAKE_GH_LOG"]).read_text().splitlines()]

    def ap_calls(self):
        p = Path(self.env["FAKE_APPROVALS_LOG"])
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class Adapter(Env):
    def test_maps_the_recorded_answer_to_the_change_shape(self):
        self.sync()
        c = self.change(1)
        self.assertEqual(c["id"], "github:acme/app#1")
        self.assertEqual(c["page"], "c/github/acme/app/1")
        self.assertEqual((c["state"], c["draft"], c["mergeable"], c["conflicts"]), ("open", False, "yes", False))
        self.assertEqual(c["checks"], {"state": "passing", "failing": [], "running": [], "total": 1})
        self.assertEqual(c["review"], {"state": "approved", "by": ["Sam Lee"]})
        self.assertEqual(c["head"], {"ref": "ada/t-1-thing", "id": SHA(1)})
        self.assertEqual(self.change(2)["checks"]["state"], "failing")
        self.assertEqual(self.change(2)["checks"]["failing"], ["build"])
        self.assertEqual(self.change(3)["checks"], {"state": "running", "failing": [], "running": ["build", "ci/legacy"], "total": 2})
        self.assertEqual(self.change(4)["checks"]["state"], "none")
        self.assertEqual(self.change(5)["checks"]["state"], "failing", "a failed check wins over one still running")
        self.assertEqual(self.change(6)["review"]["state"], "changes_requested")
        self.assertEqual(self.change(7)["review"]["state"], "required")
        self.assertEqual([p["role"] for p in self.change(7)["people"]], ["author", "requested", "requested"])
        self.assertEqual((self.change(8)["mergeable"], self.change(8)["conflicts"]), ("no", True))
        self.assertEqual(self.change(9)["mergeable"], "unknown")
        self.assertTrue(self.change(10)["draft"])
        nums = [c["number"] for c in app.load()["changes"]]
        self.assertIn(20, nums, "merged within 8 days")
        self.assertNotIn(21, nums, "closed long ago")
        out = json.dumps(app.load())
        for name in ("statusCheckRollup", "reviewDecision", "mergeStateStatus", "headRefOid", "isDraft", "bodyText",
                     "MERGEABLE", "CONFLICTING", "REVIEW_REQUIRED", "CheckRun"):
            self.assertNotIn(name, out)

    def test_the_gate_refuses_anything_but_a_read(self):
        ok = [["api", "graphql", "-f", "query=query { viewer { login } }"], ["api", "repos/a/b/pulls"],
              ["api", "-X", "GET", "repos/a/b"]]
        for argv in ok:
            github.gate(argv)
        bad = [["pr", "merge", "1"], ["api", "graphql", "-f", "query=mutation { addComment }"],
               ["api", "graphql", "-f", "query=query {} mutation x {}"], ["api", "-X", "POST", "repos/a/b/issues"],
               ["api", "--method=PATCH", "repos/a/b"], ["api", "repos/a/b/issues", "-f", "title=x"],
               ["api", "graphql", "--input", "q.graphql"], ["api", "graphql"], ["auth", "token"], [],
               ["api", "graphql", "-F", "query=@mutation.graphql"], ["api", "graphql", "-f", "query=@q.graphql"],
               ["api", "graphql", "-f", "query=query { x }", "-F", "n=@file"]]
        for argv in bad:
            with self.assertRaises(github.NotARead, msg=argv):
                github.gate(argv)

    def test_a_call_that_isnt_a_read_never_starts_a_process(self):
        for argv in (["pr", "merge", "1"], ["api", "-X", "POST", "repos/a/b/issues"], ["api", "graphql", "-f", "query=mutation { x }"]):
            with self.assertRaises(github.NotARead):
                github.call(argv, "ada-bot")
        self.assertFalse(Path(self.env["FAKE_GH_LOG"]).exists(), "not even auth token ran")

    def test_a_token_shaped_string_in_an_error_is_redacted_whoever_read(self):
        with mock.patch.object(github, "RUN") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="bad credentials ghp_" + "A" * 36 + " and github_pat_" + "b" * 30)
            with self.assertRaises(RuntimeError) as e:
                github.call(["api", "graphql", "-f", "query=query { x }"], None)
        self.assertNotIn("ghp_A", str(e.exception))
        self.assertNotIn("github_pat_b", str(e.exception))
        self.assertIn("[redacted]", str(e.exception))

    def test_at_the_process_boundary_only_reads_ran_and_the_token_went_nowhere(self):
        self.sync()
        calls = self.gh_calls()
        for c in calls:
            a = c["argv"]
            self.assertTrue(a[:2] == ["auth", "token"] or a[:2] == ["api", "graphql"], a)
            if a[:2] == ["api", "graphql"]:
                github.gate(a)
        # The identity for acme's repos read them; locked/ had no identity, so no token was passed.
        reads = [c for c in calls if c["argv"][:2] == ["api", "graphql"]]
        self.assertEqual({next(x for x in c["argv"] if x.startswith("owner=")): c["token"] for c in reads},
                         {"owner=acme": "tok-SECRET-ada-bot", "owner=locked": None})
        # Planted token: nowhere in the piece's files, pages, feed, or what it prints.
        printed = []
        with mock.patch.dict(os.environ, {"CHANGES_IDENTITIES": json.dumps({"github:acme": "ada-bot", "github:locked": "ada-bot"})}):
            with mock.patch.object(github, "datetime", wraps=datetime) as dt:
                dt.now.return_value = NOW
                snap = app.sync_once(printed.append)
        self.assertTrue(printed, "the failing repo was logged")
        self.assertFalse(any("tok-SECRET" in m for m in printed), "nothing the piece prints carries it")
        self.assertIn("[redacted]", snap["repos"]["github:locked/repo"]["error"])
        seen = [p.read_text() for p in Path(self.env["CHANGES_DIR"]).iterdir() if p.is_file()]
        seen += [json.dumps(app.ui_manifest()), json.dumps(app.ui_page({"path": ""})),
                 json.dumps(app.ui_page({"path": "c/github/acme/app/1"})),
                 json.dumps(app.project_feed({"project": "app", "repo": ["github:acme/app"], "name": [], "venue": []}))]
        for text in seen:
            self.assertNotIn("tok-SECRET", text)

    def test_selectors_are_literal(self):
        self.assertEqual(forges.repo_id("Acme/App"), "github:acme/app")
        self.assertEqual(forges.repo_id("github:acme/app"), "github:acme/app")
        for bad in ("github:acme/*", "acme/a?b", "acme/[ab]", "github:acme", "acme/app b", "github:acme/app#1", ""):
            with self.assertRaises(ValueError, msg=bad):
                forges.repo_id(bad)
        with self.assertRaises(ValueError):
            forges.repo_id("forgejo:codeberg.org/a/b")       # no adapter yet


class Sync(Env):
    def test_reads_only_named_repos_keeps_going_and_replaces_whole(self):
        snap = self.sync()
        self.assertEqual(sorted(snap["repos"]), ["github:acme/app", "github:locked/repo"])
        self.assertIn("error", snap["repos"]["github:locked/repo"])
        self.assertNotIn("error", snap["repos"]["github:acme/app"])
        self.assertEqual(Path(self.env["CHANGES_DIR"], "snapshot.json").stat().st_mode & 0o777, 0o600)
        # A card no longer naming a repo: it drops out with its changes.
        self.cards.write_text(json.dumps([{"slug": "other", "name": "Other", "links": {"repos": ["locked/repo"]}}]))
        snap = self.sync()
        self.assertEqual(list(snap["repos"]), ["github:locked/repo"])
        self.assertEqual(snap["changes"], [])

    def test_a_failing_repo_keeps_its_last_changes(self):
        self.sync()
        with mock.patch.dict(os.environ, {"FAKE_GH_FIXTURE": "/no/such/file"}):
            snap = self.sync()
        self.assertTrue(snap["repos"]["github:acme/app"].get("error"))
        self.assertTrue(any(c["repo"] == "github:acme/app" for c in snap["changes"]))

    def test_the_snapshot_is_replaced_by_rename_never_rewritten_in_place(self):
        self.sync()
        p = Path(self.env["CHANGES_DIR"], "snapshot.json")
        before = p.stat().st_ino
        self.sync()
        self.assertNotEqual(p.stat().st_ino, before)


class WaitsOn(Env):
    def setUp(self):
        super().setUp()
        self.sync()

    def w(self, n, rules=None):
        return app.waits_on(self.change(n), rules if rules is not None else {})

    def test_each_row_first_match_wins(self):
        self.assertEqual(self.w(20), ("done", "Merged"))
        self.assertEqual(self.w(10), ("author", "Draft"))
        self.assertEqual(self.w(8), ("author", "Conflicts"))
        self.assertEqual(self.w(2), ("author", "CI red"))
        self.assertEqual(self.w(6), ("author", "Changes asked for"))
        self.assertEqual(self.w(3), ("ci", "CI running"))
        self.assertEqual(self.w(9), ("ci", "Checking for conflicts"))
        self.assertEqual(self.w(7), ("reviewer", "Needs review"))
        self.assertEqual(self.w(11), ("author", "Ready"))
        self.assertEqual(self.w(13), ("author", "Yours"))
        self.assertEqual(self.w(1), ("you", "Waiting on you"), "an agent's, ready, no rule: ask")
        self.assertEqual(self.w(4), ("you", "Waiting on you"), "no CI configured counts as ready")
        t = f"github:acme/app#1@{SHA(1)}"
        self.assertEqual(self.w(1, {t: {"tier": "do"}}), ("agent", "Ready for Ada to merge"))
        self.assertEqual(self.w(1, {t: {"tier": "ask", "approval": {"id": "a-1", "state": "allowed"}}}), ("agent", "Ready for Ada to merge"))
        self.assertEqual(self.w(1, {t: {"tier": "ask", "approval": {"id": "a-1", "state": "denied"}}}), ("you", "You said not yet"))
        c = dict(self.change(1), mergeable="no", conflicts=False)
        self.assertEqual(app.waits_on(c, {}), ("author", "Can't be merged"))

    def test_whose_it_is(self):
        self.assertEqual(app.agent_of(self.change(1)), "ada")
        self.assertEqual(app.agent_of(self.change(14)), "ada", "a prefix under the owner's login")
        self.assertIsNone(app.agent_of(self.change(12)), "a prefix alone, by anyone else, is not the agent's")
        self.assertEqual(self.w(12), ("author", "Ready"))
        self.assertIsNone(app.agent_of(self.change(11)))


class Pages(Env):
    def setUp(self):
        super().setUp()
        self.sync()

    def test_index_groups_by_project_waiting_on_you_first(self):
        page = app.ui_page({"path": "", "caller": OWNER})
        ids = [s["id"] for s in page["sections"]]
        self.assertEqual(ids[:2], ["summary", "p-app"])
        sec = page["sections"][1]
        self.assertEqual(sec["tone"], "warn")
        first = sec["blocks"][0]["items"]
        self.assertTrue(all(i["tone"] == "warn" for i in first))
        self.assertTrue(first[0]["text"].startswith("app#"))
        lately = next(b for b in sec["blocks"] if b["type"] == "details")
        self.assertEqual(lately["summary"], "Lately")
        self.assertEqual([i["text"] for i in lately["blocks"][0]["items"]], ["app#20 Merged yesterday"])
        order = [i["meta"] for b in sec["blocks"] if b["type"] == "list" for i in b["items"]]
        self.assertIn("Checking for conflicts", " ".join(order))
        # The project filter narrows; an unknown path is not found.
        self.assertEqual([s["id"] for s in app.ui_page({"path": "", "query": {"project": "other"}})["sections"]], ["summary", "p-other"])
        self.assertEqual(app.ui_page({"path": "c/github/acme/app/999"}), {"title": "Not found", "sections": []})

    def test_an_old_snapshot_says_how_old(self):
        snap = app.load()
        snap["at"] = "2026-11-05T11:00:00Z"
        app.write_private(Path(self.env["CHANGES_DIR"]) / "snapshot.json", json.dumps(snap))
        blocks = app.ui_page({"path": ""})["sections"][0]["blocks"]
        self.assertEqual(blocks[0]["text"][:21], "Last read 1 hour ago.")
        snap["at"] = "2026-11-05T11:55:00Z"
        app.write_private(Path(self.env["CHANGES_DIR"]) / "snapshot.json", json.dumps(snap))
        fresh = json.dumps(app.ui_page({"path": ""})["sections"][0]["blocks"])
        self.assertNotIn("Last read", fresh)

    def buttons(self, n, rules="{}", caller=OWNER):
        with mock.patch.dict(os.environ, {"FAKE_RULES": rules}):
            page = app.ui_page({"path": f"c/github/acme/app/{n}", "caller": caller})
        return [i["label"] for s in page["sections"] for b in s["blocks"] if b["type"] == "buttons" for i in b["items"]], page

    def test_a_change_page_shows_buttons_only_when_it_waits_on_you(self):
        labels, page = self.buttons(1)
        self.assertEqual(labels, ["Yes, merge it", "Not yet", "Always for The App"])
        self.assertEqual(page["title"], "app#1")
        self.assertIn("Open on GitHub", json.dumps(page))
        labels, page = self.buttons(1, json.dumps({f"github:acme/app#1@{SHA(1)}": {"tier": "do"}}))
        self.assertEqual(labels, [])
        self.assertIn("Ada merges this on its own once it's green", json.dumps(page))
        self.assertEqual(self.buttons(11)[0], [], "someone else's: no buttons")
        self.assertEqual(self.buttons(12)[0], [], "an outsider on an ada/ branch: no buttons")
        self.assertEqual(self.buttons(1, caller={"kind": "owner", "who": "sam", "agents": ["ada"]})[0], [])
        # The approvals piece doesn't answer, or answers nonsense: no buttons, and the page says why.
        for broken in ({"CHANGES_APPROVALS": "/no/such/approvals"}, {"FAKE_RULES": json.dumps({f"github:acme/app#1@{SHA(1)}": {"tier": "sure"}})}):
            with mock.patch.dict(os.environ, broken):
                page = app.ui_page({"path": "c/github/acme/app/1", "caller": OWNER})
            self.assertNotIn('"buttons"', json.dumps(page))
            self.assertIn("didn't answer", json.dumps(page))

    def test_a_reviewer_is_named_when_one_was_asked(self):
        row = app.row_of(self.change(7), {})[1]
        self.assertIn("needs review from Sam Lee, Core", row["meta"])

    def test_tasks_both_ways(self):
        self.assertEqual(app.tasks_cited(self.change(1)), ["t-1"])
        self.assertEqual(app.tasks_cited(self.change(14)), ["t-14", "t-1781"], "body (t-14), branch t1781")
        c = dict(self.change(1), title="at-12 and (t-12) and t-12x", body="", head={"ref": "x/t-30-fix", "id": SHA(1)})
        self.assertEqual(app.tasks_cited(c), ["t-12", "t-30"])
        page = app.ui_page({"path": "c/github/acme/app/1"})
        tasks = next(s for s in page["sections"] if s["id"] == "tasks")["blocks"][0]["items"]
        self.assertEqual([(t["meta"].split(" ")[0], t["page"]) for t in tasks],
                         [("t-1", "loom:p/tasks/task/t-1"), ("t-900", "loom:p/tasks/task/t-900")])
        self.assertEqual(app.load()["citing"], {"github:acme/app#1": [{"id": "t-900", "title": "Ship the thing", "status": "open"}],
                                                "github:acme/app#2": [{"id": "t-900", "title": "Ship the thing", "status": "open"}]})
        self.assertEqual([x["id"] for x in app.cited_by("t-900", app.load())], ["github:acme/app#1", "github:acme/app#2"])
        self.assertEqual([x["id"] for x in app.cited_by("t-14", app.load())], ["github:acme/app#14"])


class Actions(Env):
    def setUp(self):
        super().setUp()
        self.sync()

    def act(self, action, n=1, head=None, caller=OWNER):
        return app.ui_act({"action": action, "caller": caller,
                           "fields": {"id": f"github:acme/app#{n}", "head": head or SHA(n)}})

    def test_yes_not_yet_and_always_call_approvals_exactly(self):
        t = f"github:acme/app#1@{SHA(1)}"
        self.assertTrue(self.act("yes")["ok"])
        self.assertTrue(self.act("not-yet")["ok"])
        self.assertTrue(self.act("always")["ok"])
        calls = [c["argv"] for c in self.ap_calls()]
        grant = ["grant", "--kind", "merge", "--target", t, "--for", "ada", "--by", "olive-owner"]
        self.assertEqual(calls, [grant, grant + ["--deny"], ["ui", "act", "--json"], grant])
        rule = json.loads(self.ap_calls()[2]["stdin"])
        self.assertEqual(rule, {"action": "rule", "fields": {"kind": "merge", "scope": "project:app", "tier": "do"}, "caller": OWNER})

    def test_refused_for_anyone_but_the_owner_and_for_a_stale_head(self):
        for c in ({"kind": "owner", "who": "sam", "agents": ["ada"]}, {"kind": "agent", "agent": "ada"}):
            for a in ("yes", "not-yet", "always", "refresh"):
                self.assertFalse(app.ui_act({"action": a, "caller": c, "fields": {"id": "github:acme/app#1", "head": SHA(1)}})["ok"])
        r = self.act("yes", head=SHA(99))
        self.assertFalse(r["ok"])
        self.assertIn("new commit", r["message"])
        self.assertFalse(self.act("yes", n=11)["ok"], "someone else's change")
        self.assertEqual(self.ap_calls(), [])
        self.assertTrue(app.ui_act({"action": "refresh", "caller": OWNER})["ok"])
        self.assertTrue(Path(self.env["CHANGES_DIR"], "refresh.request").exists())


class Owner(Env):
    """is_owner fails closed: only a caller of kind owner with unrestricted agents."""

    def test_who_is_the_owner(self):
        self.assertTrue(app.is_owner({"caller": OWNER}))
        self.assertTrue(app.is_owner({"caller": {"kind": "owner", "who": "olive-owner"}}), "agents absent: unrestricted")
        for req in ({}, None, {"caller": None}, {"caller": {"kind": "agent", "agent": "ada"}},
                    {"caller": {"kind": "owner", "who": "sam", "agents": ["ada"]}}, {"caller": "owner"}):
            self.assertFalse(app.is_owner(req), req)

    def test_no_caller_gets_no_buttons_and_no_action(self):
        self.sync()
        page = app.ui_page({"path": "c/github/acme/app/1"})
        self.assertNotIn('"buttons"', json.dumps([s for s in page["sections"] if s["id"] == "state"]))
        r = app.ui_act({"action": "yes", "fields": {"id": "github:acme/app#1", "head": SHA(1)}})
        self.assertFalse(r["ok"])
        self.assertFalse(app.ui_act({"action": "yes", "caller": {"kind": "agent", "agent": "ada"},
                                     "fields": {"id": "github:acme/app#1", "head": SHA(1)}})["ok"])
        self.assertEqual([c["argv"][0] for c in self.ap_calls()], ["rule"], "the page's read only: no grant, no rule set")

    def test_the_api_is_refused_with_or_without_a_caller(self):
        import io
        for body in ({}, {"path": "act", "body": {"action": "yes"}}, {"caller": OWNER, "path": "act"},
                     {"caller": {"kind": "agent", "agent": "ada"}, "path": "run"}):
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(body))), mock.patch.object(sys, "stdout", out):
                app.main(["api", "--json"])
            self.assertEqual(json.loads(out.getvalue())["status"], 404)
        self.assertEqual(self.ap_calls(), [])


class Feed(Env):
    def setUp(self):
        super().setUp()
        self.sync()

    def test_groups_in_order_waiting_marked_pages_and_caps(self):
        f = app.project_feed(app.feed_args(["--project", "app", "--since", "2026-11-01T00:00:00Z", "--repo", "github:acme/app"]))
        titles = [g["title"] for g in f["groups"]]
        self.assertEqual(titles, ["Waiting on you", "Waiting on CI", "Waiting on a reviewer", "Waiting on its author", "Lately"])
        self.assertTrue(f["groups"][0]["waiting"])
        self.assertTrue(all("waiting" not in g for g in f["groups"][1:]))
        it = f["groups"][0]["items"][0]
        self.assertEqual(set(it), {"title", "when", "who", "status", "ref", "page"})
        self.assertTrue(it["page"].startswith("c/github/acme/app/"))
        for g in f["groups"]:
            for i in g["items"]:
                self.assertLessEqual(len(i["status"]), 24)
                self.assertLessEqual(len(i["title"]), 200)
        self.assertLessEqual(sum(len(g["items"]) for g in f["groups"]), 50)
        # Without --repo: the project's own repos, as the snapshot read them; a project with none, nothing.
        self.assertEqual(app.project_feed(app.feed_args(["--project", "app"]))["groups"], f["groups"])
        self.assertEqual(app.project_feed(app.feed_args(["--project", "nope"])), {"groups": []})


    def test_caps_hold_against_data_that_would_break_them(self):
        # Caps against data that would break them: a long title, long words, 60 changes.
        snap = app.load()
        base = self.change(1)
        many = [dict(base, number=100 + i, id=f"github:acme/app#{100 + i}", page=f"c/github/acme/app/{100 + i}",
                     title="word " * 80, head={"ref": "x", "id": SHA(100 + i)}) for i in range(60)]
        snap["changes"] = many
        app.write_private(Path(self.env["CHANGES_DIR"]) / "snapshot.json", json.dumps(snap))
        with mock.patch.object(app, "waits_on", lambda c, r: ("you", "Waiting on the reviewers of everything")):
            capped = app.project_feed(app.feed_args(["--project", "app", "--repo", "github:acme/app"]))
        items = [i for g in capped["groups"] for i in g["items"]]
        self.assertEqual(len(items), 50)
        self.assertTrue(all(len(i["title"]) <= 200 and i["title"].endswith("…") for i in items))
        self.assertTrue(all(len(i["status"]) <= 24 for i in items))


if __name__ == "__main__":
    unittest.main()
