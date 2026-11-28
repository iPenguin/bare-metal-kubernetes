#!/usr/bin/env python3
"""Rate Renovate PRs and post one sticky comment with the rating.

Runs from master (renovate-review.yaml, workflow_run on pr-checks). It
doesn't check out or run PR code: PR data comes from the Gitea API, plus
hosts/group_vars/all/versions.yaml from the master clone. PR titles, bodies
and branch names are data only.

This script is the real gate, not the workflow's `if:`. A PR is reviewed only
if it is open, not a draft, opened by svc_renovate from a renovate/* branch
of this repo, every commit is authored, committed and signed by svc_renovate,
every `pr-checks / *` job has a successful `(pull_request)` status on its
head SHA (skipped `(push)` statuses are ignored; anything else must be
success), and each of those statuses points at a successful job of the
newest `pr-checks.yaml@refs/pull/N/head` Actions run on that SHA, which must
have succeeded and been started by svc_renovate (L2). Files, commits and diffs are read for that head SHA, not
the PR's current head.

The rating is the deterministic floor from the risk rubric: the highest match
wins, and anything not known to be low is at least medium. It is decided from
the commit diffs (M3): every changed line must differ only in versions or
digests, and the PR body's update table must match the diff exactly. The body
only names dependencies and can only raise the rating.

REVIEW_MODE (repo Actions variable) is the kill switch:
  off (default)  do nothing
  comment        post or update the sticky comment
  approve, fix   as the env var alone, not enabled; log and exit 0

Approve is only on with `--mode approve`, and only when the token user is
`svc_approver` and is not the PR author or a commit author or committer.
It posts or updates the sticky comment, then an APPROVED review on a low
PR whose gate passed, pinned to the current head SHA. If the head moves
or the review is stale after the post, it tries to dismiss and exits
non-zero. It never merges, and never approves medium or high.
REVIEW_MODE=off still stops it.

Stdlib only.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

RENOVATE_LOGIN = "svc_renovate"
BOT_LOGIN = "svc_grokbot"
APPROVER_LOGIN = "svc_approver"
BRANCH_PREFIX = "renovate/"
REQUIRED_CHECKS = "pr-checks / *"
PR_EVENT, PUSH_EVENT = "pull_request)", "push)"
CHECKS_WORKFLOW = "pr-checks.yaml"
SKIP_LABEL = "bot/skip"
MARKER = "<!-- grokbot-review"
VERSIONS_FILE = "hosts/group_vars/all/versions.yaml"

LOW, MEDIUM, HIGH = 0, 1, 2
LEVELS = ("low", "medium", "high")

# Minor and major bumps go through upgrade.yaml (dependencyDashboardApproval).
UPGRADE_COMPONENTS = {"kubernetes/kubernetes", "cri-o/cri-o", "ceph/ceph", "rook-ceph", "cilium"}
# Patches of these are medium. Matched anywhere in a dependency name, image
# or path, between separators: `ghcr.io/kube-vip/kube-vip`, `argo-cd`,
# `argo-apps/apps/keycloak/values.yaml`.
CRITICAL_RE = re.compile(
    r"(?:^|[/_.:-])(traefik|argo-?cd|openbao|external-secrets|cert-manager|keycloak|lldap|kube-vip)(?:[/_.:-]|$)",
    re.I,
)
# versions.yaml pins whose patch bumps may be low. nvidia-device-plugin is
# low only once its NFD subchart is checked against nvidia_nfd_version, which
# this script does not do yet, so it stays medium for now.
LOW_PINS: set[str] = set()
NFD_PINS = {"nvidia-device-plugin"}
# Gitea Actions runner chart: CI infrastructure.
RUNNER_DEPS = {"actions"}

# Paths not listed here (run.yaml, upgrade.yaml, setup.sh, ansible.cfg,
# templates/, hosts/, other roles/) rate medium; move them here if medium
# becomes approvable.
HIGH_PATHS = (
    (".gitea/", "CI workflows"),
    ("scripts/", "CI scripts"),
    ("renovate.json", "Renovate config"),
    (".pre-commit-config.yaml", "lint config"),
    (".ansible-lint", "lint config"),
    (".yamllint", "lint config"),
    (".gitleaks.toml", "lint config"),
    (".gitleaksignore", "lint config"),
    (".secrets.baseline", "secrets baseline"),
    ("requirements-ci.", "CI tool pins"),
    ("roles/kubernetes/", "Kubernetes role"),
    ("roles/crio/", "CRI-O role"),
    ("roles/cilium/", "Cilium (CNI) role"),
    ("roles/rook/", "Rook/Ceph role"),
    ("roles/kube-vip/", "kube-vip role"),
    ("argo-apps/bootstrap/", "Argo CD bootstrap"),
)
# Any change here other than a version is high (chart bumps in appsets stay
# rated per dependency).
STRUCTURE_PATHS = ("argo-apps/bootstrap/", "argo-apps/appsets/")
MEDIUM_PATHS = (
    ("requirements.txt", "Python packages"),
    ("requirements.yml", "Ansible collections"),
    ("argo-apps/apps/gitea/actions", "Gitea Actions runner"),
)
SENSITIVE_NAME = re.compile(r"secret|rbac|role|policy|ingress|serviceaccount|crd|certificate", re.I)
SENSITIVE_KIND = re.compile(
    r"\bkind:\s*['\"]?(?:(?:Cluster)?Role(?:Binding)?|CustomResourceDefinition|"
    r"(?:Cilium(?:Clusterwide)?)?NetworkPolicy|ServiceAccount)\b"
)
SENSITIVE_KEY = re.compile(r"\b(?:rbac|apiGroups|verbs|networkPolicy|crds?)\b", re.I)
# A version, image tag or digest inside a line. A changed line is
# version-only if it is identical with these masked.
VERSION_TOKEN = re.compile(r"sha256:[0-9a-f]{64}|\b[0-9a-f]{40}\b|v?\d+(?:\.\d+)+(?:[-+][A-Za-z0-9._+-]*)?")
# 10.0.40.5 and 10.0.0.5.nip.io look like versions to VERSION_TOKEN.
OCTETS = re.compile(r"^v?\d{1,3}(?:\.\d{1,3}){3}(?:$|[-+.][A-Za-z])")
# Version-only lines must use one of these keys (or sit under a # renovate: line).
# Explicit keys only: a `*_version` wildcard would also match min_tls_version.
# `ARG NAME=` / `FROM ` cover Dockerfile image pins; versions.yaml already
# floors at medium and uses `# renovate:` above each pin.
VERSION_KEY = re.compile(
    r"(?i)^\s*(?:tag|version|targetRevision|image|appVersion|ref|ARG\s+\S+|FROM)\s*[:= ]"
)
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x85\u2028\u2029]")
MAX_LINE = 1000
HEX_RE = re.compile(r"(?:sha256:)?[0-9a-f]{7,64}")
NAME_NEAR = re.compile(r"\b(?:chart|name|repository|image|depName)\s*[:=]\s*['\"]?([A-Za-z0-9._/@-]+)")
HUNKLESS = (
    "old mode ", "new mode ", "new file mode ", "deleted file mode ",
    "rename from ", "copy from ", "similarity index ",
)
KNOWN_TARGETS = ("argo-apps/", VERSIONS_FILE, "requirements.txt", "requirements.yml", "images/")

UPDATE_LEVEL = {"major": HIGH, "minor": MEDIUM, "patch": LOW, "digest": LOW, "pindigest": LOW}
UNSAFE = re.compile(r"[^A-Za-z0-9 ()_./:+=@~-]")
# Word joiner: invisible, but breaks Gitea's @mention, URL and email linking.
WJ = "\u2060"
LINKY = re.compile(r"(@|://|(?i:www)\.)")
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def clean(text: str, limit: int = 80) -> str:
    """PR-supplied text reduced to inert characters for logs and comments."""
    return UNSAFE.sub("", str(text))[:limit]


def inert(text: str) -> str:
    """No pings or links from text quoted into a comment."""
    return LINKY.sub(lambda m: m.group(1)[0] + WJ + m.group(1)[1:], text)


@dataclass
class Dep:
    name: str
    update: str  # major | minor | patch | digest | ... | unknown
    old: str = ""
    new: str = ""


@dataclass
class Rating:
    level: int = LOW
    reasons: list[str] = field(default_factory=list)
    deps: list[tuple[Dep, int, str]] = field(default_factory=list)

    def raise_to(self, level: int, reason: str) -> None:
        self.level = max(self.level, level)
        if f"{LEVELS[level]}: {reason}" not in self.reasons:
            self.reasons.append(f"{LEVELS[level]}: {reason}")


# ---------- parsing ----------

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _link_text(cell: str) -> str:
    m = re.match(r"\[([^\]]+)\]", cell)
    return (m.group(1) if m else (cell.split() or [""])[0]).strip("`")


def _version_parts(v: str) -> list[int]:
    m = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", v.strip("`").lstrip("=").lstrip("v"))
    return [int(x) for x in m.groups() if x is not None] if m else []


def compare_versions(old: str, new: str) -> str:
    a, b = _version_parts(old), _version_parts(new)
    if not a or not b:
        return "unknown"
    for i, kind in enumerate(("major", "minor", "patch")):
        if (a[i] if i < len(a) else 0) != (b[i] if i < len(b) else 0):
            return kind
    return "patch" if old != new else "unknown"


def parse_deps(body: str) -> list[Dep]:
    """Read Renovate's update table. Its columns vary by manager."""
    deps: list[Dep] = []
    header: list[str] | None = None
    for line in (body or "").splitlines():
        if not line.startswith("|"):
            if deps:
                break
            header = None
            continue
        cells = _cells(line)
        if header is None:
            names = [re.sub(r"\[([^\]]+)\].*", r"\1", c).lower() for c in cells]
            if "package" in names and "change" in names:
                header = names
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        row = dict(zip(header, cells))
        old, new = (re.findall(r"`([^`]*)`", row.get("change", "")) + ["", ""])[:2]
        declared = row.get("update", "").strip().lower()
        digests = is_digest(old) and is_digest(new)
        computed = compare_versions(old, new) if old and new and not digests else "unknown"
        update = declared or computed
        # Take the riskier of what Renovate says and what the versions say.
        if computed != "unknown" and UPDATE_LEVEL[computed] > UPDATE_LEVEL.get(update, MEDIUM):
            update = computed
        deps.append(Dep(clean(_link_text(row.get("package", ""))), clean(update or "unknown", 20), clean(old, 60), clean(new, 60)))
    return deps


@dataclass
class Change:
    """One removed/added line pair from a diff hunk (None: no counterpart)."""
    path: str
    old: str | None
    new: str | None
    above: list[str] = field(default_factory=list)  # hunk lines just above


def parse_diff(text: str) -> tuple[list[Change], set[str]]:
    """Changed line pairs and every path the diff touches (both sides of a rename)."""
    changes: list[Change] = []
    paths: set[str] = set()
    path, in_hunk = "", False
    above: list[str] = []
    minus: list[str] = []
    plus: list[str] = []

    def flush() -> None:
        for i in range(max(len(minus), len(plus))):
            changes.append(Change(path, minus[i] if i < len(minus) else None,
                                  plus[i] if i < len(plus) else None, above[-4:]))
        above.extend(minus + plus)
        minus.clear()
        plus.clear()

    # split("\n"), not splitlines(): the latter also splits on CR, VT, FF,
    # NEL and Unicode line separators, which can hide keys after a CR.
    for line in (text or "").split("\n"):
        if line.startswith("diff --git "):
            flush()
            in_hunk = False
            m = re.match(r"diff --git a/(\S+) b/(\S+)$", line)
            if m:
                paths.update(m.groups())
                path = m.group(2)
        elif not in_hunk and line.startswith(HUNKLESS):
            changes.append(Change(path, "<header>", None))
            if line.startswith(("rename from ", "copy from ")):
                paths.add(line.split(" ", 2)[-1])
        elif not in_hunk and line.startswith(("rename to ", "copy to ", "--- a/", "+++ b/")):
            paths.add(line.split(" ", 2)[-1][2 if line.startswith(("---", "+++")) else 0:])
        elif not in_hunk and line.startswith("Binary files "):
            changes.append(Change(path, "binary file", None))
        elif line.startswith("@@"):
            flush()
            in_hunk, above = True, []
        elif in_hunk and line.startswith("-"):
            if plus:
                flush()
            minus.append(line[1:])
        elif in_hunk and line.startswith("+"):
            plus.append(line[1:])
        elif in_hunk and line.startswith(" "):
            flush()
            above.append(line[1:])
    flush()
    paths.discard("")
    return changes, paths


def version_pairs(c: Change) -> list[tuple[str, str]] | None:
    """(old, new) versions if the line pair differs only in versions, else None."""
    if c.old is None or c.new is None:
        return None
    if len(c.old) > MAX_LINE or len(c.new) > MAX_LINE:
        return None
    if CONTROL.search(c.old) or CONTROL.search(c.new):
        return None
    if not version_key_allowed(c):
        return None
    if VERSION_TOKEN.sub("\0", c.old) != VERSION_TOKEN.sub("\0", c.new):
        return None
    pairs = [(a, b) for a, b in zip(VERSION_TOKEN.findall(c.old), VERSION_TOKEN.findall(c.new)) if a != b]
    if not pairs or any(OCTETS.search(a) or OCTETS.search(b) for a, b in pairs):
        return None
    return pairs


def version_key_allowed(c: Change) -> bool:
    """Version-only if the key is allowlisted; in appsets/, only targetRevision."""
    line = c.new or ""
    if c.path.startswith("argo-apps/appsets/"):
        return bool(re.search(r"(?i)^\s*targetRevision\s*:", line))
    if VERSION_KEY.search(line):
        return True
    return bool(c.above and re.search(r"^\s*#\s*renovate:", c.above[-1]))


def _norm(v: str) -> str:
    v = v.strip("`").lstrip("=")
    return v.removeprefix("sha256:") if v.startswith("sha256:") else v.lstrip("v")


def is_digest(v: str) -> bool:
    return bool(HEX_RE.fullmatch(_norm(v))) and not v.lstrip("v").replace(".", "").isdigit()


def same_version(body: str, diff: str) -> bool:
    """Body table value vs diff value; Renovate shortens digests in the body."""
    a, b = _norm(body), _norm(diff)
    if not a or not b:
        return False
    return a == b or (is_digest(a) and is_digest(b) and len(a) >= 7 and b.startswith(a))


def diff_names(c: Change) -> list[str]:
    """What a version line belongs to: the image or package on the line, and
    any chart/name/repository/depName just above it in the hunk."""
    names = [m.group(1) for line in c.above + [c.new or ""] for m in NAME_NEAR.finditer(line)]
    raw = c.new or ""
    head = VERSION_TOKEN.split(raw[-200:] if len(raw) > 200 else raw, maxsplit=1)[0]
    m = re.search(r"([A-Za-z0-9._/-]+)[:=@]+\s*$", head)
    if m:
        names.append(m.group(1))
    return names


def name_in(dep_name: str, names: list[str]) -> bool:
    """True if the body dependency is the one named on the changed line."""
    if not names or not dep_name:
        return False
    last = dep_name.rsplit("/", 1)[-1]
    for n in names:
        nlast = n.rsplit("/", 1)[-1]
        if dep_name == n or last == n or last == nlast or dep_name == nlast:
            return True
    return False


def pin_names(versions_text: str) -> set[str]:
    return set(re.findall(r"#\s*renovate:.*?depName=(\S+)", versions_text))


# ---------- rating ----------

def _is_dockerfile(path: str) -> bool:
    name = Path(path).name
    return path.startswith("images/") and (name == "Dockerfile" or name.startswith("Dockerfile."))


def file_kind(files: list[str]) -> str:
    if files and all(f.startswith("argo-apps/") for f in files):
        return "argo"
    if files and all(f == VERSIONS_FILE for f in files):
        return "pins"
    if files and all(_is_dockerfile(f) for f in files):
        return "images"
    return "other"


def rate_dep(dep: Dep, kind: str, pins: set[str]) -> tuple[int, str]:
    u = dep.update.lower()
    level = UPDATE_LEVEL.get(u)
    if level is None:
        return MEDIUM, f"update type {u or 'unknown'} is not patch or digest"
    if u == "major":
        return HIGH, "major update"
    if dep.name in UPGRADE_COMPONENTS:
        if u == "minor":
            return HIGH, "minor of an upgrade.yaml component"
        return MEDIUM, "patch of an upgrade.yaml component"
    if level == MEDIUM:
        return MEDIUM, "minor update"
    if CRITICAL_RE.search(dep.name):
        return MEDIUM, "patch of a critical-set component"
    if dep.name in RUNNER_DEPS:
        return MEDIUM, "Gitea Actions runner"
    if kind == "pins" or dep.name in pins:
        if dep.name in LOW_PINS:
            return LOW, "allowlisted versions.yaml pin"
        if dep.name in NFD_PINS:
            return MEDIUM, "versions.yaml pin; NFD subchart match is not checked yet"
        return MEDIUM, "versions.yaml pin not on the low allowlist"
    if kind == "argo":
        return LOW, "patch or digest of an Argo CD app image or chart"
    if kind == "images":
        return LOW, "patch or digest of a Dockerfile image"
    return MEDIUM, "not an Argo CD app image/chart or allowlisted pin"


def rate(deps: list[Dep], files: list[str], labels: list[str], pins: set[str], diff: str) -> Rating:
    r = Rating()
    changes, diff_paths = parse_diff(diff)
    files = sorted(set(files) | diff_paths)
    for path in files:
        shown = f"`{clean(path, 120)}`"
        for prefix, why in HIGH_PATHS:
            if path.startswith(prefix):
                r.raise_to(HIGH, f"changes {why} ({shown})")
        if "openbao" in path.lower():
            r.raise_to(HIGH, f"changes OpenBao files ({shown})")
        for prefix, why in MEDIUM_PATHS:
            if path.startswith(prefix):
                r.raise_to(MEDIUM, f"changes {why} ({shown})")
        if CRITICAL_RE.search(path):
            r.raise_to(MEDIUM, f"critical-set path ({shown})")
        if path.startswith("argo-apps/") and SENSITIVE_NAME.search(Path(path).name):
            r.raise_to(MEDIUM, f"secret, RBAC, policy or ingress file ({shown})")
        if not path.startswith(KNOWN_TARGETS):
            r.raise_to(MEDIUM, f"file outside the usual Renovate targets ({shown})")
    if not files:
        r.raise_to(MEDIUM, "no changed files listed")
    if not deps:
        r.raise_to(MEDIUM, "could not read the update table")
    kind = file_kind(files)
    for dep in deps:
        level, why = rate_dep(dep, kind, pins)
        r.deps.append((dep, level, why))
        r.level = max(r.level, level)
    rate_diff(r, deps, changes)
    for name in labels:
        floor = name.split("/", 1)[1] if name.startswith("risk-floor/") else ""
        if floor in LEVELS:
            r.raise_to(LEVELS.index(floor), f"label `{clean(name)}`")
    return r


def rate_diff(r: Rating, deps: list[Dep], changes: list[Change]) -> None:
    """M3: the diff decides. It can only raise what the body table gave."""
    if not changes:
        r.raise_to(MEDIUM, "no diff to rate")
    matched: set[int] = set()
    for c in changes:
        where = f"`{clean(c.path, 120)}`"
        lines = [x for x in (c.old, c.new) if x is not None]
        if any(len(x) > MAX_LINE for x in lines):
            r.raise_to(MEDIUM, f"oversized line in the diff ({where})")
            continue
        if any(SENSITIVE_KIND.search(x) or SENSITIVE_KEY.search(x) for x in lines) or any(
            SENSITIVE_KIND.search(x) for x in c.above
        ):
            r.raise_to(MEDIUM, f"RBAC, CRD, ServiceAccount or NetworkPolicy in the diff ({where})")
        pairs = version_pairs(c)
        if pairs is None:
            floor = HIGH if c.path.startswith(STRUCTURE_PATHS) else MEDIUM
            r.raise_to(floor, f"changes more than a version ({where})")
            continue
        names = [clean(n, 120) for n in diff_names(c)]
        for name in names:
            if CRITICAL_RE.search(name):
                r.raise_to(MEDIUM, f"critical-set component `{name}` in the diff")
        for a, b in pairs:
            update = "digest" if is_digest(a) and is_digest(b) else compare_versions(a, b)
            level = UPDATE_LEVEL.get(update, MEDIUM)
            kind = update
            if update in ("minor", "major") and UPGRADE_COMPONENTS & set(names):
                level, kind = HIGH, f"{update} of an upgrade.yaml component"
            hits = [
                i for i, d in enumerate(deps)
                if names and name_in(d.name, names) and same_version(d.old, a) and same_version(d.new, b)
            ]
            matched.update(hits)
            shown = f"`{clean(a, 60)}` → `{clean(b, 60)}`"
            if not hits and not (update == "digest" and len(pairs) > 1):
                r.raise_to(max(level, MEDIUM), f"diff changes {shown} in {where}, which the PR body doesn't list")
            elif hits and level > min(UPDATE_LEVEL.get(deps[i].update.lower(), MEDIUM) for i in hits):
                r.raise_to(level, f"diff {shown} is {kind}")
    for i, d in enumerate(deps):
        if i not in matched:
            r.raise_to(MEDIUM, f"PR body lists `{d.name}` `{d.old}` → `{d.new}`, not found in the diff")


# ---------- gate ----------

def commit_problems(commits: list[dict], head_sha: str) -> list[str]:
    """Every commit must be authored, committed and signed by svc_renovate.

    Uses the Gitea user Gitea matched to the author and committer emails, and
    the user that owns the verified signing key (verification.signer.username).
    """
    if not commits:
        return ["no commits found for the head SHA"]
    problems = []
    if head_sha not in [c.get("sha") for c in commits]:
        problems.append("head SHA is not among the PR commits")
    for c in commits:
        sha = clean(c.get("sha", ""), 8)
        parents = c.get("parents") if c.get("parents") is not None else (c.get("commit") or {}).get("parents")
        if not isinstance(parents, list) or len(parents) != 1:
            problems.append(f"commit {sha} is not a single-parent commit")
        v = (c.get("commit") or {}).get("verification") or {}
        who = {
            "author": (c.get("author") or {}).get("login"),
            "committer": (c.get("committer") or {}).get("login"),
            "signer": (v.get("signer") or {}).get("username"),
        }
        for role, login in who.items():
            if login != RENOVATE_LOGIN:
                problems.append(f"commit {sha} {role} is not {RENOVATE_LOGIN}")
        if v.get("verified") is not True:
            problems.append(f"commit {sha} signature is not verified")
    return problems


def gate(pr: dict, statuses: list[dict], commits: list[dict], repo: str, sha: str | None,
         run: dict | None, jobs: list[dict], total_commits: int | None = None) -> list[str]:
    """Reasons this PR must not be reviewed; empty means it passes."""
    problems = []
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    if str(base.get("ref", "")) != "master":
        problems.append("base branch is not master")
    if total_commits is not None and total_commits != len(commits):
        problems.append("compare is missing commits")
    if pr.get("state") != "open":
        problems.append("PR is not open")
    if pr.get("draft"):
        problems.append("PR is a draft")
    if (pr.get("user") or {}).get("login") != RENOVATE_LOGIN:
        problems.append(f"author is not {RENOVATE_LOGIN}")
    if not str(head.get("ref", "")).startswith(BRANCH_PREFIX):
        problems.append(f"head branch is not {BRANCH_PREFIX}*")
    head_repo = (head.get("repo") or {}).get("full_name")
    base_repo = (base.get("repo") or {}).get("full_name")
    if not head_repo or head_repo != base_repo or base_repo != repo:
        problems.append("head repo is not this repo")
    if sha and head.get("sha") != sha:
        problems.append("head SHA moved since the checks ran")
    if SKIP_LABEL in [lbl.get("name") for lbl in pr.get("labels") or []]:
        problems.append(f"label {SKIP_LABEL}")
    problems += commit_problems(commits, str(head.get("sha", "")))
    problems += check_problems(statuses)
    problems += run_problems(pr, statuses, repo, run, jobs)
    return problems


def latest_checks_run(runs: list[dict], number: int, head_sha: str) -> dict | None:
    """Newest pr-checks run for this PR's head ref on this SHA, whatever its result."""
    path = f"{CHECKS_WORKFLOW}@refs/pull/{number}/head"
    mine = [x for x in runs if x.get("path") == path and x.get("head_sha") == head_sha
            and x.get("event") == "pull_request"]
    return max(mine, key=lambda x: int(x.get("id") or 0), default=None)


def run_problems(pr: dict, statuses: list[dict], repo: str, run: dict | None, jobs: list[dict]) -> list[str]:
    """L2: statuses must come from a real Actions run, not the status API.

    Anyone with write access can post a `pr-checks / ...` status. Actions'
    own statuses have no creator and link to /{repo}/actions/runs/{run}/jobs/{job},
    so each `(pull_request)` status must link to a successful job of the same
    name in the newest pr-checks run on the head SHA, started by svc_renovate.
    """
    if not run:
        return [f"no `{CHECKS_WORKFLOW}` pull_request run on the head SHA"]
    problems = []
    rid = int(run.get("id") or 0)
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        problems.append(f"run {rid} is {clean(run.get('status'), 20)}/{clean(run.get('conclusion'), 20)}")
    for who in ("actor", "trigger_actor"):
        if (run.get(who) or {}).get("login") != RENOVATE_LOGIN:
            problems.append(f"run {rid} {who} is not {RENOVATE_LOGIN}")
    by_name = {j.get("name"): j for j in jobs if int(j.get("run_id") or 0) == rid}
    seen = set()
    for s in statuses:
        ctx = s.get("context", "")
        job, _, event = ctx.rpartition(" (")
        if not fnmatch.fnmatchcase(ctx, REQUIRED_CHECKS) or event != PR_EVENT:
            continue
        name = job.removeprefix("pr-checks / ")
        j = by_name.get(name)
        url = f"/{repo}/actions/runs/{rid}/jobs/{int(j.get('id') or 0)}" if j else None
        if not j or j.get("conclusion") != "success":
            problems.append(f"`{clean(ctx, 120)}` has no successful job in run {rid}")
        elif s.get("creator") or s.get("target_url") != url:
            problems.append(f"`{clean(ctx, 120)}` was not posted by run {rid}")
        seen.add(name)
    for name in sorted(set(by_name) - seen):
        problems.append(f"run {rid} job `{clean(name, 100)}` has no status")
    return problems


def check_problems(statuses: list[dict]) -> list[str]:
    """Every pr-checks job needs a successful `(pull_request)` status.

    Renovate's branch pushes leave skipped `(push)` statuses (pr-checks runs on
    push to master only); those are ignored. Any other `(push)` state, or any
    other event, must be success too, so a real push failure still blocks.
    """
    required = [s for s in statuses if fnmatch.fnmatchcase(s.get("context", ""), REQUIRED_CHECKS)]
    if not required:
        return [f"no `{REQUIRED_CHECKS}` status on the head SHA"]
    problems = []
    jobs, pr_jobs = set(), set()
    for s in required:
        ctx, state = s.get("context", ""), s.get("status")
        job, _, event = ctx.rpartition(" (")
        jobs.add(job or ctx)
        if event == PR_EVENT:
            pr_jobs.add(job)
        elif event == PUSH_EVENT and state == "skipped":
            continue
        if state != "success":
            problems.append(f"`{clean(ctx, 120)}` is {clean(state, 20)}")
    for job in sorted(jobs - pr_jobs):
        problems.append(f"no `{clean(job, 100)} ({PR_EVENT}` status")
    return problems


# ---------- comment ----------

def completion(files: list[str], deps: list[Dep]) -> str:
    steps = []
    if any(f.startswith("argo-apps/") for f in files):
        steps.append("pull master from Gitea and push it to GitHub; Argo CD syncs")
    if VERSIONS_FILE in files:
        if any(d.name in NFD_PINS for d in deps):
            steps.append("run `upgrade.yaml -e upgrade_optin=nvidia`")
        else:
            steps.append("run `upgrade.yaml` or `run.yaml` for the changed pins")
    return "; ".join(steps) or "nothing after merge"


def render(sha: str, rating: Rating, files: list[str], statuses: list[dict], commits: list[dict], run_id: str) -> str:
    checks = ", ".join(
        f"`{clean(s.get('context', ''), 120)}` {clean(s.get('status'), 20)}"
        for s in statuses if fnmatch.fnmatchcase(s.get("context", ""), REQUIRED_CHECKS)
    ) or "none"
    verified = [((c.get("commit") or {}).get("verification") or {}).get("verified") for c in commits]
    signed = "yes" if verified and all(verified) else "no"
    level = LEVELS[rating.level]
    marker = f"{MARKER} sha={clean(sha, 64)} run={clean(run_id, 20)} -->"
    lines = [
        f"### Review: {level.upper()}",
        f"Floor: {level} · Judgment: not enabled · Checks: {checks} · Signed: {signed}",
        "",
        "**What changes**",
    ]
    for dep, dep_level, why in rating.deps:
        change = f" `{dep.old}` → `{dep.new}`" if dep.old or dep.new else ""
        lines.append(f"- `{dep.name}`{change} ({dep.update}): {LEVELS[dep_level]}, {why}")
    if not rating.deps:
        lines.append("- could not read the update table")
    if rating.reasons:
        lines += ["", "**Floor reasons**"] + [f"- {x}" for x in rating.reasons]
    lines += [
        "",
        "**To merge**: BrianM approves (dry run: the bot does not approve)",
        f"**To complete the update**: {completion(files, [d for d, _, _ in rating.deps])}",
    ]
    return marker + "\n" + inert("\n".join(lines)) + "\n"


def same_review(old: str, new: str) -> bool:
    """True if two comments differ at most in the marker line (run id)."""
    def strip(b: str) -> list[str]:
        return [x for x in b.splitlines() if not x.startswith(MARKER)]
    return strip(old) == strip(new)


class ApproveError(Exception):
    """Approve refused, or a posted review is no longer valid."""


# ---------- API ----------

class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(req.full_url, code, "redirect to non-https refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Gitea:
    def __init__(self, url: str, repo: str, token: str):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise SystemExit("Gitea URL must be https")
        self.origin = url.rstrip("/")
        self.base = f"{self.origin}/api/v1/repos/{repo}"
        self.token = token
        self.opener = urllib.request.build_opener(HttpsOnlyRedirect)
        self._login: str | None = None

    def call(self, method: str, path: str, data: dict | None = None) -> object:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if self.token:
            # Not resent on redirects.
            req.add_unredirected_header("Authorization", f"token {self.token}")
        with self.opener.open(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def whoami(self) -> str:
        """Login of the token's user. Fail closed if the API doesn't name one."""
        if self._login:
            return self._login
        req = urllib.request.Request(
            f"{self.origin}/api/v1/user",
            headers={"Accept": "application/json"},
        )
        if self.token:
            req.add_unredirected_header("Authorization", f"token {self.token}")
        with self.opener.open(req, timeout=30) as resp:
            raw = resp.read()
        login = ((json.loads(raw) if raw else {}) or {}).get("login") or ""
        if not login:
            raise RuntimeError("token user is missing")
        self._login = login
        return login

    def text(self, path: str) -> str:
        req = urllib.request.Request(self.base + path, headers={"Accept": "text/plain"})
        if self.token:
            req.add_unredirected_header("Authorization", f"token {self.token}")
        with self.opener.open(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def pages(self, path: str) -> list:
        out: list = []
        sep = "&" if "?" in path else "?"
        for page in range(1, 51):
            chunk = self.call("GET", f"{path}{sep}limit=50&page={page}") or []
            out += chunk
            if len(chunk) < 50:
                break
        return out


def changed_files(commits: list[dict]) -> list[str]:
    """Union over the commits (covers files a later commit reverted too), with
    both paths of a rename."""
    return sorted({
        name for c in commits for f in c.get("files") or []
        for name in (f.get("filename"), f.get("previous_filename")) if name
    })


def review(api: Gitea, pr: dict, repo: str, sha: str | None, mode: str, dry_run: bool, pins: set[str], run_id: str) -> None:
    n = int(pr["number"])
    head_sha = str(pr["head"]["sha"])
    base_sha = str(pr.get("merge_base") or "")
    if not SHA_RE.fullmatch(head_sha) or not SHA_RE.fullmatch(base_sha):
        print(f"#{n} blocked: head or merge base is not a commit hash")
        return
    statuses = (api.call("GET", f"/commits/{head_sha}/status") or {}).get("statuses") or []
    # Pinned to the SHAs read above, so a later push can't change what is rated.
    compare = api.call("GET", f"/compare/{base_sha}...{head_sha}?verification=true&files=true") or {}
    commits = compare.get("commits") or []
    total = compare.get("total_commits") if isinstance(compare.get("total_commits"), int) else -1
    runs = (api.call("GET", f"/actions/runs?head_sha={head_sha}&event=pull_request&limit=50") or {}).get("workflow_runs") or []
    run = latest_checks_run(runs, n, head_sha)
    jobs = []
    if run:
        jobs = (api.call("GET", f"/actions/runs/{int(run['id'])}/jobs") or {}).get("jobs") or []
    problems = gate(pr, statuses, commits, repo, sha, run, jobs, total)
    files = changed_files(commits)
    # Each commit's own diff, by its SHA from the pinned compare.
    diff = "\n".join(
        api.text(f"/git/commits/{c['sha']}.diff") for c in commits if SHA_RE.fullmatch(str(c.get("sha", "")))
    )
    labels = [lbl.get("name", "") for lbl in pr.get("labels") or []]
    rating = rate(parse_deps(pr.get("body") or ""), files, labels, pins, diff)
    body = render(head_sha, rating, files, statuses, commits, run_id)
    print(f"#{n} {LEVELS[rating.level].upper()} gate: {'pass' if not problems else 'blocked: ' + '; '.join(problems)}")
    if mode == "approve":
        check_approver(api, pr, commits)
    if dry_run:
        print(body)
        if mode == "approve":
            maybe_approve(api, n, head_sha, rating, problems, True, pr, commits)
        return
    if mode == "approve":
        # Comment first, then approve. The commenter is svc_approver, not
        # the Actions comment job (svc_grokbot / SVC_GROKBOT_TOKEN).
        if not problems:
            post_sticky(api, n, body, APPROVER_LOGIN)
        maybe_approve(api, n, head_sha, rating, problems, False, pr, commits)
        return
    if problems or mode != "comment":
        return
    post_sticky(api, n, body, BOT_LOGIN)


def post_sticky(api: Gitea, number: int, body: str, login: str) -> None:
    mine = [
        c for c in api.pages(f"/issues/{number}/comments")
        if (c.get("user") or {}).get("login") == login and (c.get("body") or "").startswith(MARKER)
    ]
    if not mine:
        api.call("POST", f"/issues/{number}/comments", {"body": body})
        print(f"#{number} comment posted")
    elif same_review(mine[0]["body"], body):
        print(f"#{number} comment already current")
    else:
        api.call("PATCH", f"/issues/comments/{int(mine[0]['id'])}", {"body": body})
        print(f"#{number} comment updated")


def check_approver(api: Gitea, pr: dict, commits: list[dict]) -> str:
    """The token must be svc_approver, and must not have authored the PR."""
    login = api.whoami()
    n = pr.get("number")
    if login != APPROVER_LOGIN:
        raise ApproveError(f"#{n} refusing: token user is {clean(login, 40)}, not {APPROVER_LOGIN}")
    if (pr.get("user") or {}).get("login") == APPROVER_LOGIN:
        raise ApproveError(f"#{n} refusing: {APPROVER_LOGIN} is the PR author")
    for c in commits:
        sha = clean(c.get("sha", ""), 8)
        who = {
            "author": (c.get("author") or {}).get("login"),
            "committer": (c.get("committer") or {}).get("login"),
        }
        for role, name in who.items():
            if name == APPROVER_LOGIN:
                raise ApproveError(f"#{n} refusing: {APPROVER_LOGIN} is the {role} of commit {sha}")
    return login


def maybe_approve(api: Gitea, number: int, head_sha: str, rating: Rating,
                  problems: list[str], dry_run: bool, pr: dict, commits: list[dict]) -> None:
    """APPROVED review on a low PR whose gate passed. Never merges."""
    check_approver(api, pr, commits)
    if problems or rating.level != LOW:
        why = "blocked: " + "; ".join(problems) if problems else LEVELS[rating.level]
        print(f"#{number} not approved ({why})")
        return
    if dry_run:
        print(f"#{number} would approve {head_sha}")
        return
    fresh = api.call("GET", f"/pulls/{number}")
    got = str(((fresh or {}).get("head") or {}).get("sha") or "")
    if not SHA_RE.fullmatch(got):
        raise RuntimeError(f"#{number} refresh did not return a commit hash")
    if got != head_sha:
        print(f"#{number} head moved; not approving")
        return
    login = api.whoami()
    for r in api.pages(f"/pulls/{number}/reviews"):
        if ((r.get("user") or {}).get("login") == login
                and r.get("state") == "APPROVED"
                and r.get("commit_id") == head_sha):
            print(f"#{number} already approved {head_sha}")
            return
    posted = api.call("POST", f"/pulls/{number}/reviews", {
        "commit_id": head_sha,
        "event": "APPROVED",
        "body": "Low risk; approved by the review script.",
    }) or {}
    rid = posted.get("id")
    if not isinstance(rid, int):
        raise ApproveError(f"#{number} approve POST did not return a review id")
    print(f"#{number} approved {head_sha}")
    confirm_approval(api, number, head_sha, int(rid))


def confirm_approval(api: Gitea, number: int, head_sha: str, review_id: int) -> None:
    """If the head moved or the review is stale, dismiss it and fail closed."""
    fresh = api.call("GET", f"/pulls/{number}")
    got = str(((fresh or {}).get("head") or {}).get("sha") or "")
    rev = api.call("GET", f"/pulls/{number}/reviews/{review_id}") or {}
    stale = bool(rev.get("stale")) or str(rev.get("commit_id") or "") != got or got != head_sha
    if not stale:
        return
    why = "head moved" if got != head_sha else "review is stale"
    try:
        api.call("POST", f"/pulls/{number}/reviews/{review_id}/dismissals",
                 {"message": f"{why} after approve"})
        print(f"#{number} dismissed review {review_id} ({why})")
    except urllib.error.HTTPError as e:
        print(f"#{number} could not dismiss review {review_id}: {e.code} {clean(e.reason, 40)}",
              file=sys.stderr)
    raise ApproveError(f"#{number} {why} after approve")


def targets(api: Gitea, pr_arg: str, sha_arg: str, event: dict) -> tuple[list[dict], str | None]:
    """PRs to look at, and the SHA they must still have (None for sweeps)."""
    pr_number = pr_arg or str((event.get("inputs") or {}).get("pr") or "").strip()
    if pr_number:
        if not pr_number.isdigit():
            raise SystemExit("pr must be a number")
        return [api.call("GET", f"/pulls/{int(pr_number)}")], None
    sha = sha_arg or str((event.get("workflow_run") or {}).get("head_sha") or "")
    open_prs = api.pages("/pulls?state=open")
    if sha:
        if not SHA_RE.fullmatch(sha):
            raise SystemExit("head SHA is not a commit hash")
        return [p for p in open_prs if p["head"]["sha"] == sha], sha
    return [p for p in open_prs if str(p["head"].get("ref", "")).startswith(BRANCH_PREFIX)], None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rate Renovate PRs and comment or approve.")
    ap.add_argument("--dry-run", action="store_true", help="print the would-be comments or approvals; post nothing")
    ap.add_argument("--pr", default="", help="review one PR number")
    ap.add_argument("--sha", default="", help="review the open PR with this head SHA")
    ap.add_argument("--mode", default="", choices=["", "comment", "approve"],
                    help="approve posts an APPROVED review; default is comment")
    ap.add_argument("--token-file", help="read the API token from this file instead of GITEA_TOKEN_BOT")
    args = ap.parse_args(argv)

    env_mode = (os.environ.get("REVIEW_MODE") or "off").strip().lower()
    flag = (args.mode or "").strip().lower()
    if flag == "approve":
        # Explicit --mode is required. REVIEW_MODE=off remains the kill switch.
        if env_mode == "off" and not args.dry_run:
            print("REVIEW_MODE is off; doing nothing")
            return 0
        mode = "approve"
    else:
        if env_mode in ("approve", "fix"):
            print(f"REVIEW_MODE={env_mode} is not enabled yet; doing nothing")
            return 0
        if env_mode not in ("off", "comment"):
            print(f"unknown REVIEW_MODE {clean(env_mode, 20)!r}; doing nothing")
            return 0
        if env_mode == "off" and not args.dry_run:
            print("REVIEW_MODE is off; doing nothing")
            return 0
        mode = "comment"

    token = Path(args.token_file).read_text().strip() if args.token_file else os.environ.get("GITEA_TOKEN_BOT", "")
    if not token and not args.dry_run:
        print("GITEA_TOKEN_BOT is not set; doing nothing", file=sys.stderr)
        return 1
    url = os.environ.get("GITHUB_SERVER_URL") or "https://git.clt.milco.casa"
    repo = os.environ.get("GITHUB_REPOSITORY") or "homelab/bare-metal-kubernetes"
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH") or "/nonexistent")
    event = json.loads(event_path.read_text()) if event_path.is_file() else {}
    versions = Path(VERSIONS_FILE)
    pins = pin_names(versions.read_text()) if versions.is_file() else set()
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"

    api = Gitea(url, repo, token)
    prs, sha = targets(api, args.pr, args.sha, event)
    if not prs:
        print("no matching open PR")
        return 0
    rc = 0
    for pr in prs:
        try:
            review(api, pr, repo, sha, mode, args.dry_run, pins, run_id)
        except urllib.error.HTTPError as e:
            print(f"#{pr.get('number')} API error {e.code} {clean(e.reason, 40)}", file=sys.stderr)
            rc = 1
        except ApproveError as e:
            print(str(e), file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
