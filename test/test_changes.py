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

from loom_changes import app, collections, forges  # noqa: E402
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
        data = page["changes"]
        self.assertEqual([g["id"] for g in data["groups"]], app.ORDER)
        self.assertEqual(data["rows"][0]["group"], "you")
        self.assertIn("Merged yesterday", [r["title"] for r in data["rows"] if r["group"] == "done"])
        self.assertIn("Checking for conflicts", " ".join(r["status"] for r in data["rows"]))
        other = app.ui_page({"path": "", "query": {"project": "other"}})["changes"]["rows"]
        self.assertTrue(all(r["repo"] == "acme/other" for r in other))
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
        page = page["changes"]["reader"]
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
            self.assertNotIn('"buttons"', json.dumps(page["changes"]["reader"]))
            self.assertIn("didn't answer", json.dumps(page))

    def test_split_reader_keeps_authority_and_rule_verdict(self):
        for tier in ("do", "tell", "ask"):
            with mock.patch.dict(os.environ, {"FAKE_RULES": json.dumps({f"github:acme/app#1@{SHA(1)}": {"tier": tier}})}):
                page = app.ui_page({"path": "c/github/acme/app/1", "caller": OWNER})
                data = page["changes"]
                self.assertEqual(data["selected"], "c/github/acme/app/1")
                rule = next(s for s in data["reader"]["sections"] if s["id"] == "rule")
                self.assertEqual(rule["blocks"][0]["text"], tier)
                original = app.change_page({"caller": OWNER}, app.load(), self.change(1))
                state = next(s for s in original["sections"] if s["id"] == "state")
                self.assertEqual(data["reader"]["sections"][0], state)

    def test_lately_reads_the_real_rule_without_adding_actions(self):
        c = self.change(20)
        target = app.target_of(c)
        with mock.patch.dict(os.environ, {"FAKE_RULES": json.dumps({target: {"tier": "do"}})}):
            page = app.ui_page({"path": c["page"], "caller": OWNER})["changes"]["reader"]
        rule = next(s for s in page["sections"] if s["id"] == "rule")
        self.assertEqual(rule["blocks"][0]["text"], "do")
        self.assertFalse(any(b["type"] == "buttons" for s in page["sections"] for b in s["blocks"]))
        self.assertTrue(all(c["argv"][0] == "rule" for c in self.ap_calls()))

    def test_list_rule_outage_cannot_change_the_readers_buttons(self):
        target = f"github:acme/app#1@{SHA(1)}"
        with mock.patch.object(app, "rules_for", side_effect=[None, {target: {"tier": "ask"}}]):
            page = app.ui_page({"path": "c/github/acme/app/1", "caller": OWNER})
        reader = page["changes"]["reader"]
        labels = [i["label"] for s in reader["sections"] for b in s["blocks"] if b["type"] == "buttons" for i in b["items"]]
        self.assertEqual(labels, ["Yes, merge it", "Not yet", "Always for The App"])

    def test_a_reviewer_is_named_when_one_was_asked(self):
        row = app.row_of(self.change(7), {})[1]
        self.assertIn("needs review from Sam Lee, Core", row["meta"])

    def test_tasks_both_ways(self):
        self.assertEqual(app.tasks_cited(self.change(1)), ["t-1"])
        self.assertEqual(app.tasks_cited(self.change(14)), ["t-14", "t-1781"], "body (t-14), branch t1781")
        c = dict(self.change(1), title="at-12 and (t-12) and t-12x", body="", head={"ref": "x/t-30-fix", "id": SHA(1)})
        self.assertEqual(app.tasks_cited(c), ["t-12", "t-30"])
        page = app.ui_page({"path": "c/github/acme/app/1"})
        tasks = next(s for s in page["changes"]["reader"]["sections"] if s["id"] == "tasks")["blocks"][0]["items"]
        self.assertEqual([(t["meta"].split(" ")[0], t["page"]) for t in tasks],
                         [("t-1", "loom:p/tasks/task/t-1"), ("t-900", "loom:p/tasks/task/t-900")])
        self.assertEqual(app.load()["citing"], {"github:acme/app#1": [{"id": "t-900", "title": "Ship the thing", "status": "open"}],
                                                "github:acme/app#2": [{"id": "t-900", "title": "Ship the thing", "status": "open"}]})
        self.assertEqual([x["id"] for x in app.cited_by("t-900", app.load())], ["github:acme/app#1", "github:acme/app#2"])
        self.assertEqual([x["id"] for x in app.cited_by("t-14", app.load())], ["github:acme/app#14"])


class Actions(Env):
    buttons = Pages.buttons

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

    def test_a_merge_asked_for_someone_else_says_whom_and_its_yes_is_for_them(self):
        # t-1956: Ada asked to merge app#1 for Loren. The owner's own rule (do) doesn't answer that, so the page waits on
        # him, says whom it's for, has no Always (his own rule), and Yes and Not yet answer Loren's card (--person).
        t = f"github:acme/app#1@{SHA(1)}"
        rules = json.dumps({t: {"tier": "do", "asked_for": [{"for": "loren", "name": "Loren", "id": "a-0000beef"}]}})
        labels, page = self.buttons(1, rules)
        self.assertEqual(labels, ["Yes, merge it", "Not yet"])
        self.assertIn("Waiting on you, for Loren", json.dumps(page))
        self.assertIn("asked to merge this for Loren", json.dumps(page))
        items = [i for s in page["sections"] for b in s["blocks"] if b["type"] == "buttons" for i in b["items"]]
        self.assertEqual({i["fields"]["person"] for i in items}, {"loren"})
        with mock.patch.dict(os.environ, {"FAKE_RULES": rules}):
            r = app.ui_act({"action": "yes", "caller": OWNER, "fields": {"id": "github:acme/app#1", "head": SHA(1), "person": "loren"}})
            self.assertTrue(r["ok"], r)
            self.assertIn("for Loren", r["message"])
            # A person the agent didn't ask for, or Always with one, is refused before approvals is told anything.
            for bad in ({"person": "dan"}, {"person": "loren", "_always": 1}):
                act = "always" if "_always" in bad else "yes"
                r = app.ui_act({"action": act, "caller": OWNER, "fields": {"id": "github:acme/app#1", "head": SHA(1), "person": bad["person"]}})
                self.assertFalse(r["ok"])
        grants = [c["argv"] for c in self.ap_calls() if c["argv"][0] == "grant"]
        self.assertEqual(grants, [["grant", "--kind", "merge", "--target", t, "--for", "ada", "--by", "olive-owner", "--person", "loren"]])
        # The standing rule says it's for his own work.
        self.assertIn("this one was asked for Loren", json.dumps(page))
        # Allowed for her, not yet merged: no buttons, says he said yes for her (never "Waiting on you" again).
        allowed = json.dumps({t: {"tier": "ask", "asked_for": [{"for": "loren", "name": "Loren", "id": "a-0000beef", "state": "allowed"}]}})
        labels, page = self.buttons(1, allowed)
        self.assertEqual(labels, [])
        self.assertIn("You said yes for Loren", json.dumps(page))
        # Junk where the list goes: read as none, never an error.
        for junk in (5, "loren", [5], [{"for": "Not A Slug"}], [{"for": "loren", "state": "used"}]):
            labels, _ = self.buttons(1, json.dumps({t: {"tier": "ask", "asked_for": junk}}))
            self.assertEqual(labels, ["Yes, merge it", "Not yet", "Always for The App"], junk)
        # Once spent (no one else listed), the page is the owner's own rule again.
        labels, page = self.buttons(1, json.dumps({t: {"tier": "do"}}))
        self.assertEqual(labels, [])

    def test_approvals_runs_as_itself_never_with_this_pieces_home(self):
        # The parent has this piece's home and name, its CHANGES_* settings (Env sets CHANGES_DIR and more), and loom's
        # own LOOM_HOME: the child gets LOOM_HOME and none of the rest.
        self.assertTrue(any(k.startswith("CHANGES_") for k in os.environ))
        with mock.patch.dict(os.environ, {"LOOM_PIECE_HOME": "/somewhere/changes", "LOOM_PIECE_NAME": "changes", "LOOM_HOME": "/the/loom"}):
            self.assertTrue(self.act("yes")["ok"])
            app.rules_for([self.change(1)])
        calls = self.ap_calls()
        self.assertEqual([c["argv"][0] for c in calls], ["grant", "rule"])
        for c in calls:
            self.assertIsNone(c["piece_home"], c["argv"])
            self.assertIsNone(c["piece_name"], c["argv"])
            self.assertEqual(c["changes_env"], [], c["argv"])
            self.assertEqual(c["loom_home"], "/the/loom", "everything else passes through")

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
        self.assertNotIn('"buttons"', json.dumps([s for s in page["changes"]["reader"]["sections"] if s["id"] == "state"]))
        r = app.ui_act({"action": "yes", "fields": {"id": "github:acme/app#1", "head": SHA(1)}})
        self.assertFalse(r["ok"])
        self.assertFalse(app.ui_act({"action": "yes", "caller": {"kind": "agent", "agent": "ada"},
                                     "fields": {"id": "github:acme/app#1", "head": SHA(1)}})["ok"])
        self.assertEqual([c["argv"][0] for c in self.ap_calls()], ["rule", "rule"], "list and reader read rules only: no grant, no rule set")

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


CV = "cv-0123456789ab"


class Thread(Env):
    """A Comms thread's changes (loom comms.md, "Work in this thread", criterion 25). Made here comes only from a task
    that vouches for the change (its description, or a comment by an agent or the owner) and whose source is the
    thread's conversation."""

    def setUp(self):
        super().setUp()
        self.tasks = Path(self.td.name) / "tasks.json"
        d = json.loads(self.tasks.read_text())
        d["tasks"] += [
            {"id": "t-2", "title": "Fix the failing one", "status": "open", "source": f"conversation:{CV}",
             "description": "The fix is acme/app#2.", "comments": []},
            {"id": "t-3", "title": "Elsewhere", "status": "open", "source": "conversation:cv-ba9876543210",
             "description": "acme/app#3", "comments": []},
            {"id": "t-4", "title": "Commented", "status": "open", "source": f"conversation:{CV}", "description": "",
             "comments": [{"actor": "gabe", "text": "this is acme/app#7"}, {"actor": "ada", "text": "opened acme/app#8"}]},
        ]
        self.tasks.write_text(json.dumps(d))
        self.sync()

    def rewrite(self, n, **kw):
        snap = app.load()
        for c in snap["changes"]:
            if c["number"] == n:
                c.update(kw)
        app.write_private(Path(self.env["CHANGES_DIR"]) / "snapshot.json", json.dumps(snap))

    def feed(self, *refs):
        return app.thread_feed(app.thread_args(["--json", "--conversation", CV, *sum((["--ref", r] for r in refs), [])]))

    def test_named_and_made_with_their_state(self):
        f = self.feed("github:acme/app#20", "github:acme/app#999", "github:acme/app#2", "t-12", "note:pond/plan")
        by = {i["ref"]: i for i in f["items"]}
        # Made here: #2 (t-2's description cites it) and #8 (an agent's comment on t-4). Not #3 (t-3 is another's).
        self.assertTrue(by["github:acme/app#2"]["made"])
        self.assertEqual((by["github:acme/app#2"]["status"], by["github:acme/app#2"]["tone"]), ("CI red", "bad"))
        self.assertTrue(by["github:acme/app#8"]["made"])
        self.assertNotIn("github:acme/app#3", by)
        self.assertEqual((by["github:acme/app#20"]["status"], by["github:acme/app#20"]["tone"]), ("Merged", "ok"))
        self.assertNotIn("made", by["github:acme/app#20"])
        self.assertEqual(by["github:acme/app#999"], {"title": "acme/app#999", "ref": "github:acme/app#999", "status": "Not tracked",
                                                     "href": "https://github.com/acme/app/issues/999"})
        self.assertFalse(any(not r.startswith("github:") for r in by))
        self.assertEqual([i["ref"] for i in f["items"]][-2:], ["github:acme/app#20", "github:acme/app#999"])
        for i in f["items"]:
            self.assertTrue(set(i) <= {"title", "ref", "status", "tone", "when", "page", "href", "made"})
            self.assertLessEqual(len(i["status"]), 24)
            self.assertFalse("page" in i and "href" in i)

    def test_a_cv_id_in_a_changes_own_text_never_attributes_it_whoever_wrote_it(self):
        # #6 is ada's (an agent) in a project repo; #11 is Gabe's. Title, body and branch all carry the cv- id.
        for n in (6, 11):
            self.rewrite(n, title=f"From {CV}", body=f"made in {CV}", head={"ref": f"ada/{CV}", "id": SHA(n)})
            self.assertIsNone(app.conversation_of(self.change(n), app.load()))
        by = {i["ref"]: i for i in self.feed("github:acme/app#6", "github:acme/app#11")["items"]}
        self.assertNotIn("made", by["github:acme/app#6"])
        self.assertNotIn("made", by["github:acme/app#11"])

    def test_a_change_citing_a_made_task_is_linked_not_attributed(self):
        # #5's own text cites t-5... give a made task t-5 that never cites #5 back: linked only.
        d = json.loads(self.tasks.read_text())
        d["tasks"].append({"id": "t-5", "title": "Five", "status": "open", "source": f"conversation:{CV}", "description": "", "comments": []})
        self.tasks.write_text(json.dumps(d))
        self.sync()
        self.assertIn("t-5", app.tasks_cited(self.change(5)))
        self.assertIsNone(app.conversation_of(self.change(5), app.load()))

    def test_a_comment_by_someone_who_isnt_an_agent_doesnt_attribute(self):
        self.assertIsNone(app.conversation_of(self.change(7), app.load()))
        self.assertEqual(app.conversation_of(self.change(8), app.load()), CV)

    def test_the_owners_comment_counts_once_named(self):
        d = json.loads(self.tasks.read_text())
        next(t for t in d["tasks"] if t["id"] == "t-4")["comments"].append({"actor": "olive", "text": "and acme/app#9"})
        self.tasks.write_text(json.dumps(d))
        self.sync()
        self.assertIsNone(app.conversation_of(self.change(9), app.load()))
        with mock.patch.dict(os.environ, {"CHANGES_TASK_OWNER": "olive"}):
            self.sync()
        self.assertEqual(app.conversation_of(self.change(9), app.load()), CV)

    def test_a_ref_as_written_finds_the_snapshots_change_once(self):
        by = [i for i in self.feed("github:Acme/App#2", "github:acme/app#2")["items"] if i["ref"] == "github:acme/app#2"]
        self.assertEqual(len(by), 1)
        self.assertEqual(by[0]["status"], "CI red")

    def test_caps_hold_against_data_that_would_break_them(self):
        snap = app.load()
        base = self.change(1)
        snap["changes"] = [dict(base, number=100 + i, id=f"github:acme/app#{100 + i}", page=f"c/github/acme/app/{100 + i}",
                                title="word " * 80, head={"ref": "x", "id": SHA(100 + i)}) for i in range(60)]
        snap["trusted"] = {c["id"]: ["t-2"] for c in snap["changes"]}
        app.write_private(Path(self.env["CHANGES_DIR"]) / "snapshot.json", json.dumps(snap))
        items = self.feed()["items"]
        self.assertEqual(len(items), 50)
        self.assertTrue(all(i["made"] and len(i["title"]) <= 200 and i["title"].endswith("…") for i in items))

    def test_a_change_page_says_where_it_came_from(self):
        page = app.ui_page({"path": self.change(2)["page"], "caller": OWNER})
        self.assertIn({"id": "from", "place": "side", "blocks": [{"type": "from", "conversation": CV}]}, page["changes"]["reader"]["sections"])
        other = app.ui_page({"path": self.change(1)["page"], "caller": OWNER})
        self.assertFalse(any(s["id"] == "from" for s in other["changes"]["reader"]["sections"]))

    def test_the_hub_takes_only_the_projects_own_repos(self):
        self.assertEqual(app.project_feed(app.feed_args(["--project", "nope", "--conversation", CV]))["groups"], [])
        f = app.project_feed(app.feed_args(["--project", "app", "--repo", "github:acme/app", "--conversation", CV]))
        self.assertIn("github:acme/app#2", [i["ref"] for g in f["groups"] for i in g["items"]])


if __name__ == "__main__":
    unittest.main()


class Collections(Env):
    """The shared collection, changes/change (loom collections.md; t-2029): the snapshot's changes as records, for loom
    and household owners only, filtered before paging, keyset pages, only the fields asked for."""
    OWNER_V = {"kind": "member", "household": True}

    def setUp(self):
        super().setUp()
        self.snap = self.sync()
        self.recs = collections.records(self.snap, app.agent_of, app.is_owners)

    def q(self, **o):
        return {"id": "q", "type": "change", "version": 1, "where": [], "order": [], "limit": 500, "after": None,
                "fields": list(collections.TYPE["default_fields"]), **o}

    def rows(self, viewer, *queries):
        return [json.loads(l) for l in collections.answer({"viewer": viewer, "queries": list(queries)}, self.recs)]

    def test_declares_in_package_json_exactly_what_it_answers(self):
        pkg = json.loads((HERE.parent / "package.json").read_text())
        self.assertEqual(pkg["loom"]["collections"], collections.DECLARATION)

    def test_every_change_once_as_its_record_in_loom_grammar(self):
        ids = [c["id"] for c in self.snap["changes"]]
        self.assertTrue(ids)
        self.assertEqual(sorted(r["ref"] for r in self.recs), sorted(set(ids)))
        for r in self.recs:
            self.assertRegex(r["id"], r"^[A-Za-z0-9._:-]{1,128}$")
            self.assertEqual(r["id"], r["ref"].replace("/", ":").replace("#", ":"))
            self.assertEqual(r["audience"], {"owners": True})
        one = next(r for r in self.recs if r["ref"] == "github:acme/app#1")
        c = next(c for c in self.snap["changes"] if c["id"] == "github:acme/app#1")
        self.assertEqual((one["state"], one["checks"], one["head"], one["projects"]), (c["state"], c["checks"]["state"], c["head"]["id"], ["app"]))
        self.assertEqual(one["agent"], app.agent_of(c))
        # Nothing of the body, nobody's credential.
        self.assertNotIn("body", one)
        self.assertNotIn("token", json.dumps(self.recs).lower())

    def test_keyset_pages_cover_each_once_in_order(self):
        order = [["updated", "desc"]]
        seen, after = [], None
        for _ in range(50):
            lines = self.rows(self.OWNER_V, self.q(order=order, limit=2, after=after))
            page = [l["row"] for l in lines if "row" in l]
            seen += [r["id"] for r in page]
            if not next(l for l in lines if "more" in l)["more"]:
                break
            after = [page[-1]["updated"], page[-1]["id"]]
        self.assertEqual(sorted(seen), sorted(r["id"] for r in self.recs))
        self.assertEqual(len(seen), len(set(seen)))

    def test_filters_as_loom_does_and_sends_only_whats_asked(self):
        open_ = self.rows(self.OWNER_V, self.q(where=[["state", "eq", "open"]], fields=["ref"]))
        self.assertEqual(sorted(l["row"]["ref"] for l in open_ if "row" in l), sorted(c["id"] for c in self.snap["changes"] if c["state"] == "open"))
        for l in open_:
            if "row" in l:
                self.assertEqual(set(l["row"]), {"id", "ref", "audience"})
        named = self.rows(self.OWNER_V, self.q(where=[["ref", "in", ["github:acme/app#1", "github:nope/x#1"]]], fields=["title"]))
        self.assertEqual([l["row"]["id"] for l in named if "row" in l], ["github:acme:app:1"])
        self.assertEqual(self.rows(self.OWNER_V, self.q(type="nope"), self.q(id="v2", version=2)),
                         [{"id": "q", "error": "unsupported-version"}, {"id": "v2", "error": "unsupported-version"}])

    def test_household_owners_and_loom_only_decided_before_paging(self):
        self.assertEqual(len([l for l in self.rows({"kind": "core"}, self.q()) if "row" in l]), len(self.recs))
        for v in ({"kind": "agent", "agent": "ada"}, {"kind": "member", "household": False}, None):
            self.assertEqual(self.rows(v, self.q(limit=1)), [{"id": "q", "more": False}])

    def test_the_cli_answers_lines_and_unavailable_before_the_first_read(self):
        import subprocess
        batch = json.dumps({"viewer": self.OWNER_V, "queries": [self.q(fields=["ref"])]})
        run = lambda: subprocess.run([str(HERE.parent / "bin" / "changes"), "collections", "query", "--json"], input=batch,
                                     capture_output=True, text=True, env=os.environ)
        got = run()
        self.assertEqual(got.returncode, 0, got.stderr)
        lines = [json.loads(l) for l in got.stdout.strip().splitlines()]
        self.assertEqual(lines[-1], {"id": "q", "more": False})
        self.assertEqual(len(lines) - 1, len(self.recs))
        Path(self.env["CHANGES_DIR"], "snapshot.json").unlink()
        self.assertEqual(json.loads(run().stdout), {"id": "q", "error": "unavailable"})

    def test_a_read_with_nothing_changed_moves_no_record(self):
        again = collections.records(self.sync(), app.agent_of, app.is_owners)
        self.assertEqual(again, self.recs)
