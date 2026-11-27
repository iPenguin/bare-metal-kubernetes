"""Tests for renovate_review.py: python3 -m unittest discover -s scripts -p 'test_*.py'"""
from __future__ import annotations

import unittest

import renovate_review as rr

REPO = "homelab/bare-metal-kubernetes"
SHA = "a" * 40
GREEN = [
    {"context": "pr-checks / pr-checks (pull_request)", "status": "success"},
    {"context": "pr-checks / renovate-config (pull_request)", "status": "success"},
    {"context": "renovate/stability-days", "status": "pending"},
]
SIGNED = [{"sha": SHA, "author": {"login": "svc_renovate"}, "commit": {"verification": {"verified": True}}}]


def pr(**over):
    base = {
        "number": 1,
        "state": "open",
        "draft": False,
        "user": {"login": "svc_renovate"},
        "labels": [],
        "head": {"ref": "renovate/foo", "sha": SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "master", "repo": {"full_name": REPO}},
    }
    base.update(over)
    return base


def table(*rows, cols="| Package | Update | Change |"):
    return "This PR contains the following updates:\n\n" + cols + "\n|---|---|---|\n" + "\n".join(rows) + "\n\n---\n"


def level(body, files, labels=(), commits=SIGNED, pins=frozenset()):
    return rr.LEVELS[rr.rate(rr.parse_deps(body), list(files), list(labels), commits, set(pins)).level]


class Gate(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(rr.gate(pr(), GREEN, REPO, SHA), [])

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
                self.assertTrue(rr.gate(p, GREEN, REPO, SHA))

    def test_sha_moved(self):
        self.assertTrue(rr.gate(pr(), GREEN, REPO, "b" * 40))

    def test_checks(self):
        self.assertTrue(rr.gate(pr(), [], REPO, SHA))
        self.assertTrue(rr.gate(pr(), GREEN[2:], REPO, SHA))
        red = [dict(GREEN[0], status="failure"), GREEN[1]]
        self.assertTrue(rr.gate(pr(), red, REPO, SHA))
        pending = [dict(GREEN[0], status="pending"), GREEN[1]]
        self.assertTrue(rr.gate(pr(), pending, REPO, SHA))


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

    def test_commits(self):
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        files = ["argo-apps/apps/a/values.yaml"]
        other = [dict(SIGNED[0], author={"login": "svc_grokbot"})]
        unsigned = [dict(SIGNED[0], commit={"verification": {"verified": False}})]
        self.assertEqual(level(body, files, commits=other), "medium")
        self.assertEqual(level(body, files, commits=unsigned), "high")


class Comment(unittest.TestCase):
    def test_marker_and_idempotence(self):
        rating = rr.rate(rr.parse_deps(table("| a | patch | `1.0.0` → `1.0.1` |")), ["argo-apps/a.yaml"], [], SIGNED, set())
        one = rr.render(SHA, rating, ["argo-apps/a.yaml"], GREEN, SIGNED, "1")
        two = rr.render(SHA, rating, ["argo-apps/a.yaml"], GREEN, SIGNED, "2")
        self.assertTrue(one.startswith(f"<!-- grokbot-review sha={SHA} run=1 -->"))
        self.assertTrue(rr.same_review(one, two))
        self.assertIn("### Review: LOW", one)


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
