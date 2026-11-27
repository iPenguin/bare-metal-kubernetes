"""Tests for renovate_review.py: python3 -m unittest discover -s scripts -p 'test_*.py'"""
from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

import renovate_review as rr

REPO = "homelab/bare-metal-kubernetes"
SHA = "a" * 40
GREEN = [
    {"context": "pr-checks / pr-checks (pull_request)", "status": "success"},
    {"context": "pr-checks / renovate-config (pull_request)", "status": "success"},
    {"context": "renovate/stability-days", "status": "pending"},
]
BASE = "b" * 40
SIGNED = [{
    "sha": SHA,
    "author": {"login": "svc_renovate"},
    "committer": {"login": "svc_renovate"},
    "commit": {"verification": {"verified": True, "signer": {"username": "svc_renovate"}}},
    "files": [{"filename": "argo-apps/apps/a/values.yaml", "status": "modified"}],
}]


def pr(**over):
    base = {
        "number": 1,
        "state": "open",
        "draft": False,
        "user": {"login": "svc_renovate"},
        "labels": [],
        "head": {"ref": "renovate/foo", "sha": SHA, "repo": {"full_name": REPO}},
        "merge_base": BASE,
        "base": {"ref": "master", "repo": {"full_name": REPO}},
    }
    base.update(over)
    return base


def table(*rows, cols="| Package | Update | Change |"):
    return "This PR contains the following updates:\n\n" + cols + "\n|---|---|---|\n" + "\n".join(rows) + "\n\n---\n"


def level(body, files, labels=(), pins=frozenset()):
    return rr.LEVELS[rr.rate(rr.parse_deps(body), list(files), list(labels), set(pins)).level]


class Gate(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(rr.gate(pr(), GREEN, SIGNED, REPO, SHA), [])

    def test_blocks(self):
        cases = {
            "author": pr(user={"login": "bmilco"}),
            "branch": pr(head={"ref": "feature", "sha": SHA, "repo": {"full_name": REPO}}),
            "fork": pr(head={"ref": "renovate/x", "sha": SHA, "repo": {"full_name": "someone/fork"}}),
            "closed": pr(state="closed"),
            "draft": pr(draft=True),
            "skip": pr(labels=[{"name": "bot/skip"}]),
        }
        for name, p in cases.items():
            with self.subTest(name):
                self.assertTrue(rr.gate(p, GREEN, SIGNED, REPO, SHA))

    def test_sha_moved(self):
        self.assertTrue(rr.gate(pr(), GREEN, SIGNED, REPO, "c" * 40))

    def test_checks(self):
        self.assertTrue(rr.gate(pr(), [], SIGNED, REPO, SHA))
        self.assertTrue(rr.gate(pr(), GREEN[2:], SIGNED, REPO, SHA))
        red = [dict(GREEN[0], status="failure"), GREEN[1]]
        self.assertTrue(rr.gate(pr(), red, SIGNED, REPO, SHA))
        pending = [dict(GREEN[0], status="pending"), GREEN[1]]
        self.assertTrue(rr.gate(pr(), pending, SIGNED, REPO, SHA))

    def test_skipped_push_statuses(self):
        push = [
            {"context": "pr-checks / pr-checks (push)", "status": "skipped"},
            {"context": "pr-checks / renovate-config (push)", "status": "skipped"},
        ]
        # PR #41 shape: skipped push statuses beside green pull_request ones.
        self.assertEqual(rr.gate(pr(), push + GREEN, SIGNED, REPO, SHA), [])
        cases = {
            "push only": push + GREEN[2:],
            "push failed": [dict(push[0], status="failure")] + GREEN,
            "push pending": [dict(push[0], status="pending")] + GREEN,
            "pull_request skipped": push + [dict(GREEN[0], status="skipped"), GREEN[1]],
            "job has no pull_request status": push + GREEN[1:],
            "other event skipped": [{"context": "pr-checks / x (pull_request_target)", "status": "skipped"}] + GREEN,
        }
        for name, statuses in cases.items():
            with self.subTest(name):
                self.assertTrue(rr.gate(pr(), statuses, SIGNED, REPO, SHA))

    def test_commits(self):
        c = SIGNED[0]
        v = c["commit"]["verification"]
        cases = {
            "none": [],
            "author": [dict(c, author={"login": "svc_grokbot"})],
            # renovate@ as author email, but committed and signed by someone else
            "committer": [dict(c, committer={"login": "svc_grokbot"})],
            "signer": [dict(c, commit={"verification": dict(v, signer={"username": "svc_grokbot"})})],
            "no signer": [dict(c, commit={"verification": {"verified": True}})],
            "unverified": [dict(c, commit={"verification": dict(v, verified=False)})],
            "head missing": [dict(c, sha="d" * 40)],
            "one bad of two": [dict(c, sha="d" * 40), dict(c, committer=None)],
        }
        for name, commits in cases.items():
            with self.subTest(name):
                self.assertTrue(rr.gate(pr(), GREEN, commits, REPO, SHA))


class Parse(unittest.TestCase):
    def test_columns_vary(self):
        body = table(
            "| [passlib](https://x) ([changelog](https://y)) | `==1.7.4` → `==1.9.5` | ![age](a) | ![c](b) |",
            cols="| Package | Change | [Age](https://a) | [Confidence](https://b) |",
        )
        (dep,) = rr.parse_deps(body)
        self.assertEqual((dep.name, dep.update, dep.old, dep.new), ("passlib", "minor", "==1.7.4", "==1.9.5"))

    def test_declared_lower_than_versions(self):
        (dep,) = rr.parse_deps(table("| app | patch | `1.2.3` → `2.0.0` |"))
        self.assertEqual(dep.update, "major")

    def test_markup_is_stripped(self):
        (dep,) = rr.parse_deps(table("| [x<img src=1>`@here`](https://x) | patch | `1.0.0` → `1.0.1` |"))
        self.assertNotIn("<", dep.name)
        self.assertNotIn("`", dep.name)

    def test_underscores_survive(self):
        (dep,) = rr.parse_deps(table("| [middleware_automation.keycloak](https://x) | patch | `3.1.2` → `3.1.3` |"))
        self.assertEqual(dep.name, "middleware_automation.keycloak")


class Rubric(unittest.TestCase):
    def test_argo_patch_is_low(self):
        self.assertEqual(level(table("| [velero/velero](https://x) | patch | `v1.18.4` → `v1.18.5` |"),
                               ["argo-apps/apps/velero/values.yaml"]), "low")

    def test_argo_minor_is_medium(self):
        self.assertEqual(level(table("| velero/velero | minor | `v1.18.4` → `v1.19.0` |"),
                               ["argo-apps/apps/velero/values.yaml"]), "medium")

    def test_major_is_high(self):
        self.assertEqual(level(table("| traefik | major | `35.4.0` → `43.0.0` |"),
                               ["argo-apps/appsets/traefik.yaml"]), "high")

    def test_critical_patch_is_medium(self):
        self.assertEqual(level(table("| traefik | patch | `35.4.0` → `35.4.1` |"),
                               ["argo-apps/appsets/traefik.yaml"]), "medium")

    def test_upgrade_components(self):
        files = ["hosts/group_vars/all/versions.yaml"]
        self.assertEqual(level(table("| kubernetes/kubernetes | minor | `1.36.6` → `1.37.3` |"), files), "high")
        self.assertEqual(level(table("| rook-ceph | patch | `1.20.10` → `1.20.11` |"), files), "medium")

    def test_pins_are_medium(self):
        files = ["hosts/group_vars/all/versions.yaml"]
        self.assertEqual(level(table("| helm/helm | patch | `3.22.1` → `3.22.2` |"), files), "medium")
        self.assertEqual(level(table("| nvidia-device-plugin | patch | `0.20.2` → `0.20.3` |"), files), "medium")

    def test_group_takes_highest_member(self):
        body = table("| a | patch | `1.0.0` → `1.0.1` |", "| b | minor | `1.0.0` → `1.1.0` |")
        self.assertEqual(level(body, ["argo-apps/appsets/a.yaml"]), "medium")

    def test_path_floors(self):
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        for path, want in [
            (".gitea/workflows/pr-checks.yaml", "high"),
            ("scripts/x.py", "high"),
            ("renovate.json", "high"),
            (".pre-commit-config.yaml", "high"),
            ("requirements-ci.lock", "high"),
            ("roles/cilium/defaults/main.yaml", "high"),
            ("roles/openbao/tasks/main.yaml", "high"),
            ("requirements.txt", "medium"),
            ("argo-apps/apps/gitea/actions-values.yaml", "medium"),
            ("argo-apps/apps/x/secrets.yaml", "medium"),
            ("README.md", "medium"),
        ]:
            with self.subTest(path):
                self.assertEqual(level(body, ["argo-apps/apps/a/values.yaml", path]), want)

    def test_unreadable_body_is_medium(self):
        self.assertEqual(level("no table here", ["argo-apps/apps/a/values.yaml"]), "medium")

    def test_unknown_update_type_is_medium(self):
        self.assertEqual(level(table("| a | replacement | `x` → `y` |"), ["argo-apps/apps/a/values.yaml"]), "medium")

    def test_labels_only_raise(self):
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        files = ["argo-apps/apps/a/values.yaml"]
        self.assertEqual(level(body, files, ["risk-floor/high"]), "high")
        self.assertEqual(level(table("| a | major | `1.0.0` → `2.0.0` |"), files, ["risk-floor/low", "risk-allow/low"]), "high")


class Comment(unittest.TestCase):
    def test_marker_and_idempotence(self):
        rating = rr.rate(rr.parse_deps(table("| a | patch | `1.0.0` → `1.0.1` |")), ["argo-apps/a.yaml"], [], set())
        one = rr.render(SHA, rating, ["argo-apps/a.yaml"], GREEN, SIGNED, "1")
        two = rr.render(SHA, rating, ["argo-apps/a.yaml"], GREEN, SIGNED, "2")
        self.assertTrue(one.startswith(f"<!-- grokbot-review sha={SHA} run=1 -->"))
        self.assertTrue(rr.same_review(one, two))
        self.assertIn("### Review: LOW", one)

    def test_no_mentions_or_links(self):
        body = table("| [@admin https://evil.example/x www.evil.example a@b.example](https://x) | patch | `1.0.0` → `1.0.1` |")
        files = ["argo-apps/a.yaml", "argo-apps/@team/http://x.yaml"]
        out = rr.render(SHA, rr.rate(rr.parse_deps(body), files, [], set()), files, GREEN, SIGNED, "1")
        text = out.split("\n", 1)[1]
        for bad in ("@admin", "@team", "@b.example", "://", "www.", "WWW."):
            self.assertNotIn(bad, text)
        self.assertIn("@" + rr.WJ + "admin", text)


class Api(unittest.TestCase):
    def test_https_only(self):
        for url in ("http://git.example", "file:///etc", "git.example"):
            with self.subTest(url), self.assertRaises(SystemExit):
                rr.Gitea(url, REPO, "t")

    def test_token_not_redirected(self):
        api = rr.Gitea("https://git.example", REPO, "t")
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_open(req, timeout):
            seen.append(req)
            return Resp()

        with mock.patch.object(api.opener, "open", fake_open):
            api.call("GET", "/pulls/1")
        (req,) = seen
        self.assertTrue(req.full_url.startswith("https://"))
        self.assertEqual(req.unredirected_hdrs.get("Authorization"), "token t")
        self.assertNotIn("Authorization", req.headers)

    def test_no_redirect_off_https(self):
        import urllib.request

        h = rr.HttpsOnlyRedirect()
        req = urllib.request.Request("https://git.example/api/v1/x")
        with self.assertRaises(urllib.error.HTTPError):
            h.redirect_request(req, None, 302, "Found", {}, "http://git.example/api/v1/x")
        self.assertIsNotNone(h.redirect_request(req, None, 302, "Found", {}, "https://git.example/api/v1/y"))


class Review(unittest.TestCase):
    def test_reads_by_checked_sha(self):
        class Api:
            calls: list[str] = []

            def call(self, method, path, data=None):
                self.calls.append(f"{method} {path}")
                if path.startswith("/commits/"):
                    return {"statuses": GREEN}
                if path.startswith("/compare/"):
                    return {"commits": SIGNED}
                raise AssertionError(path)

            def pages(self, path):
                self.calls.append(f"PAGES {path}")
                return []

        api = Api()
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        with mock.patch("builtins.print"):
            rr.review(api, pr(body=body), REPO, SHA, "comment", True, set(), "1")
        self.assertEqual(api.calls, [
            f"GET /commits/{SHA}/status",
            f"GET /compare/{BASE}...{SHA}?verification=true&files=true",
        ])


class Targets(unittest.TestCase):
    class Api:
        def __init__(self, prs):
            self.prs = prs

        def pages(self, path):
            assert path == "/pulls?state=open", path
            return self.prs

    def test_workflow_run_finds_pr_by_head_sha(self):
        # Gitea 28.4 pull_request run: no head_branch, PR in pull_requests.
        event = {"workflow_run": {
            "event": "pull_request",
            "head_sha": SHA,
            "pull_requests": [{"number": 1, "head": {"ref": "renovate/foo", "sha": SHA}}],
        }}
        other = pr(number=2, head={"ref": "renovate/bar", "sha": BASE, "repo": {"full_name": REPO}})
        prs, sha = rr.targets(self.Api([pr(), other]), "", "", event)
        self.assertEqual([p["number"] for p in prs], [1])
        self.assertEqual(sha, SHA)

    def test_sweep_takes_renovate_branches_only(self):
        mine = pr(number=3, head={"ref": "codebot/x", "sha": BASE, "repo": {"full_name": REPO}})
        prs, sha = rr.targets(self.Api([pr(), mine]), "", "", {})
        self.assertEqual([p["number"] for p in prs], [1])
        self.assertIsNone(sha)

    def test_bad_sha_refused(self):
        with self.assertRaises(SystemExit):
            rr.targets(self.Api([]), "", "", {"workflow_run": {"head_sha": "master"}})


class Modes(unittest.TestCase):
    def run_mode(self, mode):
        import io
        import os
        from contextlib import redirect_stdout
        from unittest import mock

        out = io.StringIO()
        with mock.patch.dict(os.environ, {"REVIEW_MODE": mode} if mode else {}, clear=True), redirect_stdout(out):
            rc = rr.main([])
        return rc, out.getvalue()

    def test_off_approve_fix_do_nothing(self):
        for mode in ("", "off", "approve", "fix", "bogus"):
            with self.subTest(mode or "unset"):
                rc, out = self.run_mode(mode)
                self.assertEqual(rc, 0)
                self.assertIn("doing nothing", out)
        self.assertIn("not enabled", self.run_mode("approve")[1])


if __name__ == "__main__":
    unittest.main()
