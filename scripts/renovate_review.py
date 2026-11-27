#!/usr/bin/env python3
"""Rate Renovate PRs and post one sticky comment with the rating.

Runs from master (renovate-review.yaml, workflow_run on pr-checks). It never
checks out or runs PR code: everything comes from the Gitea API, plus
hosts/group_vars/all/versions.yaml from the master checkout. PR titles,
bodies and branch names are data only.

This script is the real gate, not the workflow's `if:`. A PR is reviewed only
if it is open, not a draft, authored by svc_renovate from a renovate/* branch
of this repo, and every `pr-checks / *` status on its head SHA is success.

The rating is the deterministic floor from the risk rubric: the highest match
wins, and anything not known to be low is at least medium.

REVIEW_MODE (repo Actions variable) is the kill switch:
  off (default)  do nothing
  comment        post or update the sticky comment
  approve, fix   not enabled yet; log and exit 0

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
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

RENOVATE_LOGIN = "svc_renovate"
BOT_LOGIN = "svc_grokbot"
BRANCH_PREFIX = "renovate/"
REQUIRED_CHECKS = "pr-checks / *"
SKIP_LABEL = "bot/skip"
MARKER = "<!-- grokbot-review"
VERSIONS_FILE = "hosts/group_vars/all/versions.yaml"

LOW, MEDIUM, HIGH = 0, 1, 2
LEVELS = ("low", "medium", "high")

# Minor and major bumps go through upgrade.yaml (dependencyDashboardApproval).
UPGRADE_COMPONENTS = {"kubernetes/kubernetes", "cri-o/cri-o", "ceph/ceph", "rook-ceph", "cilium"}
# Patches of these are medium.
CRITICAL_APPS = {
    "traefik", "argo-cd", "argocd", "openbao", "openbao/openbao", "external-secrets",
    "cert-manager", "keycloak", "lldap", "ghcr.io/kube-vip/kube-vip",
}
# versions.yaml pins whose patch bumps may be low. nvidia-device-plugin is
# low only once its NFD subchart is checked against nvidia_nfd_version, which
# this script does not do yet, so it stays medium for now.
LOW_PINS: set[str] = set()
NFD_PINS = {"nvidia-device-plugin"}
# Gitea Actions runner chart: CI infrastructure.
RUNNER_DEPS = {"actions"}

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
)
MEDIUM_PATHS = (
    ("requirements.txt", "Python packages"),
    ("requirements.yml", "Ansible collections"),
    ("argo-apps/apps/gitea/actions", "Gitea Actions runner"),
)
SENSITIVE_NAME = re.compile(r"secret|rbac|role|policy|ingress|serviceaccount|crd|certificate", re.I)
KNOWN_TARGETS = ("argo-apps/", VERSIONS_FILE, "requirements.txt", "requirements.yml", "images/")

UPDATE_LEVEL = {"major": HIGH, "minor": MEDIUM, "patch": LOW, "digest": LOW, "pindigest": LOW}
UNSAFE = re.compile(r"[^A-Za-z0-9 ()_./:+=@~-]")


def clean(text: str, limit: int = 80) -> str:
    """PR-supplied text reduced to inert characters for logs and comments."""
    return UNSAFE.sub("", str(text))[:limit]


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
        computed = compare_versions(old, new) if old and new else "unknown"
        update = declared or computed
        # Take the riskier of what Renovate says and what the versions say.
        if computed != "unknown" and UPDATE_LEVEL[computed] > UPDATE_LEVEL.get(update, MEDIUM):
            update = computed
        deps.append(Dep(clean(_link_text(row.get("package", ""))), clean(update or "unknown", 20), clean(old, 60), clean(new, 60)))
    return deps


def pin_names(versions_text: str) -> set[str]:
    return set(re.findall(r"#\s*renovate:.*?depName=(\S+)", versions_text))


# ---------- rating ----------

def file_kind(files: list[str]) -> str:
    if files and all(f.startswith("argo-apps/") for f in files):
        return "argo"
    if files and all(f == VERSIONS_FILE for f in files):
        return "pins"
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
    if dep.name in CRITICAL_APPS:
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
    return MEDIUM, "not an Argo CD app image/chart or allowlisted pin"


def rate(deps: list[Dep], files: list[str], labels: list[str], commits: list[dict], pins: set[str]) -> Rating:
    r = Rating()
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
    for name in labels:
        floor = name.split("/", 1)[1] if name.startswith("risk-floor/") else ""
        if floor in LEVELS:
            r.raise_to(LEVELS.index(floor), f"label `{clean(name)}`")
    for c in commits:
        sha = clean(c.get("sha", ""), 8)
        if ((c.get("author") or {}).get("login")) != RENOVATE_LOGIN:
            r.raise_to(MEDIUM, f"commit {sha} is not by {RENOVATE_LOGIN}")
        if not ((c.get("commit") or {}).get("verification") or {}).get("verified"):
            r.raise_to(HIGH, f"commit {sha} signature is not verified")
    return r


# ---------- gate ----------

def gate(pr: dict, statuses: list[dict], repo: str, sha: str | None) -> list[str]:
    """Reasons this PR must not be reviewed; empty means it passes."""
    problems = []
    head = pr.get("head") or {}
    base = pr.get("base") or {}
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
    required = [s for s in statuses if fnmatch.fnmatchcase(s.get("context", ""), REQUIRED_CHECKS)]
    if not required:
        problems.append(f"no `{REQUIRED_CHECKS}` status on the head SHA")
    for s in required:
        if s.get("status") != "success":
            problems.append(f"`{clean(s.get('context', ''), 120)}` is {clean(s.get('status'), 20)}")
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
    lines = [
        f"{MARKER} sha={clean(sha, 64)} run={clean(run_id, 20)} -->",
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
    return "\n".join(lines) + "\n"


def same_review(old: str, new: str) -> bool:
    """True if two comments differ at most in the marker line (run id)."""
    def strip(b: str) -> list[str]:
        return [x for x in b.splitlines() if not x.startswith(MARKER)]
    return strip(old) == strip(new)


# ---------- API ----------

class Gitea:
    def __init__(self, url: str, repo: str, token: str):
        self.base = f"{url.rstrip('/')}/api/v1/repos/{repo}"
        self.token = token

    def call(self, method: str, path: str, data: dict | None = None) -> object:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if self.token:
            req.add_header("Authorization", f"token {self.token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def pages(self, path: str) -> list:
        out: list = []
        sep = "&" if "?" in path else "?"
        for page in range(1, 51):
            chunk = self.call("GET", f"{path}{sep}limit=50&page={page}") or []
            out += chunk
            if len(chunk) < 50:
                break
        return out


def review(api: Gitea, pr: dict, repo: str, sha: str | None, mode: str, dry_run: bool, pins: set[str], run_id: str) -> None:
    n = int(pr["number"])
    head_sha = pr["head"]["sha"]
    statuses = (api.call("GET", f"/commits/{head_sha}/status") or {}).get("statuses") or []
    problems = gate(pr, statuses, repo, sha)
    files = [f["filename"] for f in api.pages(f"/pulls/{n}/files")]
    commits = api.pages(f"/pulls/{n}/commits?verification=true&files=false")
    labels = [lbl.get("name", "") for lbl in pr.get("labels") or []]
    rating = rate(parse_deps(pr.get("body") or ""), files, labels, commits, pins)
    body = render(head_sha, rating, files, statuses, commits, run_id)
    print(f"#{n} {LEVELS[rating.level].upper()} gate: {'pass' if not problems else 'blocked: ' + '; '.join(problems)}")
    if dry_run:
        print(body)
        return
    if problems or mode != "comment":
        return
    mine = [
        c for c in api.pages(f"/issues/{n}/comments")
        if (c.get("user") or {}).get("login") == BOT_LOGIN and (c.get("body") or "").startswith(MARKER)
    ]
    if not mine:
        api.call("POST", f"/issues/{n}/comments", {"body": body})
        print(f"#{n} comment posted")
    elif same_review(mine[0]["body"], body):
        print(f"#{n} comment already current")
    else:
        api.call("PATCH", f"/issues/comments/{int(mine[0]['id'])}", {"body": body})
        print(f"#{n} comment updated")


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
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise SystemExit("head SHA is not a commit hash")
        return [p for p in open_prs if p["head"]["sha"] == sha], sha
    return [p for p in open_prs if str(p["head"].get("ref", "")).startswith(BRANCH_PREFIX)], None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rate Renovate PRs and comment the rating.")
    ap.add_argument("--dry-run", action="store_true", help="print the would-be comments; post nothing")
    ap.add_argument("--pr", default="", help="review one PR number")
    ap.add_argument("--sha", default="", help="review the open PR with this head SHA")
    ap.add_argument("--token-file", help="read the API token from this file instead of GITEA_TOKEN_BOT")
    args = ap.parse_args(argv)

    mode = (os.environ.get("REVIEW_MODE") or "off").strip().lower()
    if mode in ("approve", "fix"):
        print(f"REVIEW_MODE={mode} is not enabled yet; doing nothing")
        return 0
    if mode not in ("off", "comment"):
        print(f"unknown REVIEW_MODE {clean(mode, 20)!r}; doing nothing")
        return 0
    if mode == "off" and not args.dry_run:
        print("REVIEW_MODE is off; doing nothing")
        return 0

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
    return rc


if __name__ == "__main__":
    sys.exit(main())
