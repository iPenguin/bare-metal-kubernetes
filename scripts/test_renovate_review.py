"""Tests for renovate_review.py: python3 -m unittest discover -s scripts -p 'test_*.py'"""
from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

import renovate_review as rr

REPO = "homelab/bare-metal-kubernetes"
SHA = "a" * 40
GREEN = [
    {"context": "pr-checks / pr-checks (pull_request)", "status": "success",
     "target_url": f"/{REPO}/actions/runs/7/jobs/70"},
    {"context": "pr-checks / renovate-config (pull_request)", "status": "success",
     "target_url": f"/{REPO}/actions/runs/7/jobs/71"},
    {"context": "renovate/stability-days", "status": "pending", "creator": {"login": "svc_renovate"}},
]
# PR #41 run 494 shape.
RUN = {
    "id": 7, "path": "pr-checks.yaml@refs/pull/1/head", "event": "pull_request", "head_sha": SHA,
    "status": "completed", "conclusion": "success",
    "actor": {"login": "svc_renovate"}, "trigger_actor": {"login": "svc_renovate"},
}
JOBS = [
    {"id": 70, "run_id": 7, "name": "pr-checks", "status": "completed", "conclusion": "success"},
    {"id": 71, "run_id": 7, "name": "renovate-config", "status": "completed", "conclusion": "success"},
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


def diff_of(path, *pairs):
    """A commit diff changing one line per (old line, new line) pair in path."""
    out = f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@\n"
    for old, new in pairs:
        out += f" spec:\n-{old}\n+{new}\n"
    return out


def matching_diff(body, files):
    """A version-only diff that matches the body table, in the first file."""
    deps = rr.parse_deps(body)
    return diff_of(files[0] if files else "x", *[(f"  version: {d.old}", f"  version: {d.new}") for d in deps])


def rating(body, files, labels=(), pins=frozenset(), diff=None):
    diff = matching_diff(body, list(files)) if diff is None else diff
    return rr.rate(rr.parse_deps(body), list(files), list(labels), set(pins), diff)


def level(body, files, labels=(), pins=frozenset(), diff=None):
    return rr.LEVELS[rating(body, files, labels, pins, diff).level]


class Gate(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(rr.gate(pr(), GREEN, SIGNED, REPO, SHA, RUN, JOBS), [])

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
                self.assertTrue(rr.gate(p, GREEN, SIGNED, REPO, SHA, RUN, JOBS))

    def test_sha_moved(self):
        self.assertTrue(rr.gate(pr(), GREEN, SIGNED, REPO, "c" * 40, RUN, JOBS))

    def test_checks(self):
        self.assertTrue(rr.gate(pr(), [], SIGNED, REPO, SHA, RUN, JOBS))
        self.assertTrue(rr.gate(pr(), GREEN[2:], SIGNED, REPO, SHA, RUN, JOBS))
        red = [dict(GREEN[0], status="failure"), GREEN[1]]
        self.assertTrue(rr.gate(pr(), red, SIGNED, REPO, SHA, RUN, JOBS))
        pending = [dict(GREEN[0], status="pending"), GREEN[1]]
        self.assertTrue(rr.gate(pr(), pending, SIGNED, REPO, SHA, RUN, JOBS))

    def test_skipped_push_statuses(self):
        push = [
            {"context": "pr-checks / pr-checks (push)", "status": "skipped"},
            {"context": "pr-checks / renovate-config (push)", "status": "skipped"},
        ]
        # PR #41 shape: skipped push statuses beside green pull_request ones.
        self.assertEqual(rr.gate(pr(), push + GREEN, SIGNED, REPO, SHA, RUN, JOBS), [])
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
                self.assertTrue(rr.gate(pr(), statuses, SIGNED, REPO, SHA, RUN, JOBS))

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
                self.assertTrue(rr.gate(pr(), GREEN, commits, REPO, SHA, RUN, JOBS))


    def test_l2_run_verification(self):
        ok = rr.gate(pr(), GREEN, SIGNED, REPO, SHA, RUN, JOBS)
        self.assertEqual(ok, [])
        forged = dict(GREEN[0], target_url=None, creator={"login": "svc_renovate"})
        cases = {
            "no run": (GREEN, None, []),
            "run failed": (GREEN, dict(RUN, conclusion="failure"), JOBS),
            "run in progress": (GREEN, dict(RUN, status="running", conclusion=""), JOBS),
            "run by another actor": (GREEN, dict(RUN, actor={"login": "svc_grokbot"}), JOBS),
            "triggered by another actor": (GREEN, dict(RUN, trigger_actor={"login": "bmilco"}), JOBS),
            "status from the API": ([forged, GREEN[1]], RUN, JOBS),
            "status links another run": ([dict(GREEN[0], target_url=f"/{REPO}/actions/runs/6/jobs/70"), GREEN[1]], RUN, JOBS),
            "status names no job": ([dict(GREEN[0], context="pr-checks / lint (pull_request)"), GREEN[1]], RUN, JOBS),
            "job failed": (GREEN, RUN, [dict(JOBS[0], conclusion="failure"), JOBS[1]]),
            "job without status": (GREEN, RUN, JOBS + [{"id": 72, "run_id": 7, "name": "extra", "conclusion": "success"}]),
            "jobs of another run": (GREEN, RUN, [dict(j, run_id=6) for j in JOBS]),
        }
        for name, (statuses, run, jobs) in cases.items():
            with self.subTest(name):
                self.assertTrue(rr.gate(pr(), statuses, SIGNED, REPO, SHA, run, jobs))

    def test_latest_checks_run(self):
        old = dict(RUN, id=5, conclusion="failure")
        new = dict(RUN, id=9, conclusion="failure")
        other_pr = dict(RUN, id=12, path="pr-checks.yaml@refs/pull/2/head")
        other_sha = dict(RUN, id=13, head_sha="c" * 40)
        review = dict(RUN, id=14, path="renovate-review.yaml@refs/heads/master", event="workflow_run")
        runs = [old, RUN, new, other_pr, other_sha, review]
        # A newer failed run on the same SHA wins over an older success.
        self.assertEqual(rr.latest_checks_run(runs, 1, SHA)["id"], 9)
        self.assertEqual(rr.latest_checks_run(runs[:2], 1, SHA)["id"], 7)
        self.assertIsNone(rr.latest_checks_run([other_pr, other_sha, review], 1, SHA))


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


class Diff(unittest.TestCase):
    VELERO = "argo-apps/apps/velero/values.yaml"
    BODY = table("| [velero/velero](https://x) | patch | `v1.18.4` → `v1.18.5` |")

    def test_version_only_lines_are_low(self):
        diff = diff_of(self.VELERO, ("    image: velero/velero:v1.18.4", "    image: velero/velero:v1.18.5"))
        self.assertEqual(level(self.BODY, [self.VELERO], diff=diff), "low")
        # tag plus digest on one line; the body shows only the tag
        d1, d2 = "sha256:abc1234" + "1" * 57, "sha256:def5678" + "2" * 57
        diff = diff_of(self.VELERO, (f"  image: velero/velero:v1.18.4@{d1}", f"  image: velero/velero:v1.18.5@{d2}"))
        self.assertEqual(level(self.BODY, [self.VELERO], diff=diff), "low")
        # digest-only bump; Renovate shortens digests in the body
        body = table("| velero/velero | digest | `abc1234` → `def5678` |")
        diff = diff_of(self.VELERO, (f"  image: velero/velero:v1.18.4@{d1}", f"  image: velero/velero:v1.18.4@{d2}"))
        self.assertEqual(level(body, [self.VELERO], diff=diff), "low")

    def test_any_other_changed_line_is_medium(self):
        version = ("    image: velero/velero:v1.18.4", "    image: velero/velero:v1.18.5")
        cases = {
            "extra changed line": diff_of(self.VELERO, version, ("  replicas: one", "  replicas: two")),
            "added line": diff_of(self.VELERO, version) + "+  hostNetwork: true\n",
            "removed line": diff_of(self.VELERO, version) + "-  readOnlyRootFilesystem: true\n",
            "version and key changed": diff_of(self.VELERO, ("    image: velero/velero:v1.18.4", "    img: velero/velero:v1.18.5")),
            "binary": diff_of(self.VELERO, version) + "diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n",
            "no diff": "",
        }
        for name, diff in cases.items():
            with self.subTest(name):
                self.assertEqual(level(self.BODY, [self.VELERO], diff=diff), "medium")

    def test_body_and_diff_must_match(self):
        cases = {
            "diff bumps something else": ("    image: evil/thing:v1.0.0", "    image: evil/thing:v1.0.1"),
            "diff goes elsewhere": ("    image: velero/velero:v1.18.4", "    image: velero/velero:v1.18.6"),
        }
        for name, pair in cases.items():
            with self.subTest(name):
                self.assertEqual(level(self.BODY, [self.VELERO], diff=diff_of(self.VELERO, pair)), "medium")
        # body dep matches, plus an extra version change the body doesn't list
        listed = ("    image: velero/velero:v1.18.4", "    image: velero/velero:v1.18.5")
        extra = ("    image: evil/thing:v1.0.0", "    image: evil/thing:v1.0.1")
        self.assertEqual(level(self.BODY, [self.VELERO], diff=diff_of(self.VELERO, listed, extra)), "medium")
        # body lists two deps, diff changes one
        body = table("| velero/velero | patch | `v1.18.4` → `v1.18.5` |", "| b | patch | `2.0.0` → `2.0.1` |")
        diff = diff_of(self.VELERO, ("    image: velero/velero:v1.18.4", "    image: velero/velero:v1.18.5"))
        self.assertEqual(level(body, [self.VELERO], diff=diff), "medium")

    def test_diff_update_type_overrides_body(self):
        # the body's update table claims patch for a minor version change
        r = rr.Rating()
        dep = rr.Dep("velero/velero", "patch", "v1.18.4", "v1.19.0")
        changes, _ = rr.parse_diff(diff_of(self.VELERO, ("  image: velero/velero:v1.18.4", "  image: velero/velero:v1.19.0")))
        rr.rate_diff(r, [dep], changes)
        self.assertEqual(rr.LEVELS[r.level], "medium")

    def test_critical_set_by_prefix_or_regex(self):
        for name in ("traefik", "argo-cd", "quay.io/argoproj/argocd", "ghcr.io/kube-vip/kube-vip",
                     "openbao/openbao", "docker.io/bitnami/keycloak", "lldap/lldap", "external-secrets"):
            with self.subTest(name):
                self.assertTrue(rr.CRITICAL_RE.search(name))
        for name in ("velero/velero", "nextcloud", "external-dns", "argo-apps/apps/plex/values.yaml", "traefikee-ish"):
            with self.subTest(name):
                self.assertFalse(rr.CRITICAL_RE.search(name))
        # body names it innocently; the diff shows a critical image
        path = "argo-apps/apps/x/values.yaml"
        body = table("| x | patch | `1.2.3` → `1.2.4` |")
        diff = diff_of(path, ("    image: ghcr.io/kube-vip/kube-vip:1.2.3", "    image: ghcr.io/kube-vip/kube-vip:1.2.4"))
        self.assertEqual(level(body, [path], diff=diff), "medium")
        # chart name on a context line above targetRevision
        path = "argo-apps/appsets/x.yaml"
        diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@\n"
                "           chart: argo-cd\n-          targetRevision: 1.2.3\n+          targetRevision: 1.2.4\n")
        self.assertEqual(level(body, [path], diff=diff), "medium")
        # path of a critical app
        self.assertEqual(level(body, ["argo-apps/apps/keycloak/values.yaml"]), "medium")

    def test_rename_checks_old_and_new_paths(self):
        changes, paths = rr.parse_diff(
            "diff --git a/scripts/x.py b/argo-apps/apps/a/x.yaml\nsimilarity index 100%\n"
            "rename from scripts/x.py\nrename to argo-apps/apps/a/x.yaml\n"
        )
        self.assertEqual(paths, {"scripts/x.py", "argo-apps/apps/a/x.yaml"})
        commits = [{"files": [{"filename": "argo-apps/apps/a/x.yaml", "status": "renamed",
                               "previous_filename": "scripts/x.py"}]}]
        self.assertEqual(rr.changed_files(commits), ["argo-apps/apps/a/x.yaml", "scripts/x.py"])
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        # old path only in the compare file list
        self.assertEqual(level(body, ["argo-apps/apps/a/values.yaml", "scripts/x.py"]), "high")
        # old path only in the diff
        diff = matching_diff(body, ["argo-apps/apps/a/values.yaml"]) + (
            "diff --git a/.gitea/workflows/x.yaml b/argo-apps/apps/a/x.yaml\n"
            "rename from .gitea/workflows/x.yaml\nrename to argo-apps/apps/a/x.yaml\n"
        )
        self.assertEqual(level(body, ["argo-apps/apps/a/values.yaml"], diff=diff), "high")

    def test_bootstrap_and_appsets_floor_high(self):
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        boot = "argo-apps/bootstrap/a.yaml"
        self.assertEqual(level(body, [boot]), "high")
        appset = "argo-apps/appsets/a.yaml"
        version = ("          targetRevision: 1.0.0", "          targetRevision: 1.0.1")
        # chart bump only: rated per dependency
        self.assertEqual(level(body, [appset], diff=diff_of(appset, version)), "low")
        # anything else in an appset is high
        more = diff_of(appset, version, ("      project: a", "      project: b"))
        self.assertEqual(level(body, [appset], diff=more), "high")
        self.assertEqual(level(body, [appset], diff=diff_of(appset, version) + "+          path: x\n"), "high")

    def test_rbac_crd_networkpolicy_at_least_medium(self):
        path = "argo-apps/apps/a/values.yaml"
        body = table("| a | patch | `1.0.0` → `1.0.1` |")
        version = ("  version: 1.0.0", "  version: 1.0.1")
        for name, extra in {
            "role added": "+kind: ClusterRole\n",
            "binding": "+kind: RoleBinding\n",
            "crd": "+kind: CustomResourceDefinition\n",
            "netpol": "+kind: CiliumNetworkPolicy\n",
            "rbac values": "-  rbac: false\n+  rbac: true\n",
            "verbs": "+    verbs: [\"*\"]\n",
        }.items():
            with self.subTest(name):
                r = rating(body, [path], diff=diff_of(path, version) + extra)
                self.assertGreaterEqual(r.level, rr.MEDIUM)
                self.assertTrue(any("RBAC" in x for x in r.reasons))
        # version-only bump inside a CRD manifest: kind on a context line
        diff = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@\n"
                " kind: CustomResourceDefinition\n-  version: 1.0.0\n+  version: 1.0.1\n")
        self.assertEqual(level(body, [path], diff=diff), "medium")

    def test_real_shapes(self):
        # PR #42 (versions.yaml, depName comments) and #47 (requirements.yml)
        diff = (
            "diff --git a/hosts/group_vars/all/versions.yaml b/hosts/group_vars/all/versions.yaml\n"
            "--- a/hosts/group_vars/all/versions.yaml\n+++ b/hosts/group_vars/all/versions.yaml\n"
            "@@ -42,4 +42,4 @@ x\n # renovate: datasource=github-releases depName=kubernetes/kubernetes\n"
            "-kubernetes_version: 1.36.6\n+kubernetes_version: 1.37.3\n"
        )
        body = table("| something | patch | `1.36.6` → `1.37.3` |")
        r = rating(body, ["hosts/group_vars/all/versions.yaml"], diff=diff)
        self.assertEqual(rr.LEVELS[r.level], "high")


class Comment(unittest.TestCase):
    def test_marker_and_idempotence(self):
        r = rating(table("| a | patch | `1.0.0` → `1.0.1` |"), ["argo-apps/a.yaml"])
        one = rr.render(SHA, r, ["argo-apps/a.yaml"], GREEN, SIGNED, "1")
        two = rr.render(SHA, r, ["argo-apps/a.yaml"], GREEN, SIGNED, "2")
        self.assertTrue(one.startswith(f"<!-- grokbot-review sha={SHA} run=1 -->"))
        self.assertTrue(rr.same_review(one, two))
        self.assertIn("### Review: LOW", one)

    def test_no_mentions_or_links(self):
        body = table("| [@admin https://evil.example/x www.evil.example a@b.example](https://x) | patch | `1.0.0` → `1.0.1` |")
        files = ["argo-apps/a.yaml", "argo-apps/@team/http://x.yaml"]
        out = rr.render(SHA, rating(body, files), files, GREEN, SIGNED, "1")
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
                if path.startswith("/actions/runs?"):
                    return {"workflow_runs": [RUN]}
                if path == "/actions/runs/7/jobs":
                    return {"jobs": JOBS}
                raise AssertionError(path)

            def text(self, path):
                self.calls.append(f"TEXT {path}")
                return diff_of("argo-apps/apps/a/values.yaml", ("  version: 1.0.0", "  version: 1.0.1"))

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
            f"GET /actions/runs?head_sha={SHA}&event=pull_request&limit=50",
            "GET /actions/runs/7/jobs",
            f"TEXT /git/commits/{SHA}.diff",
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
