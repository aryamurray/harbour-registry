#!/usr/bin/env python3
"""What happens when the PR gate finishes on a bump PR: the seam for the repair step.

    bump_triage.py ensure-labels --repo OWNER/REPO
    bump_triage.py triage --repo OWNER/REPO --run-id ID --head-branch BRANCH --head-sha SHA \\
                          --conclusion success|failure|... [--budget N] [--repair-enabled true|false] \\
                          [--out-dir DIR]

Run by the `triage` job of .github/workflows/watch.yml, which is triggered by `workflow_run` of the PR
workflow. THAT JOB RUNS IN THE BASE REPOSITORY'S CONTEXT WITH A WRITE TOKEN, so this script obeys one rule:

    it never checks out, imports, builds or executes anything that came from the pull request.

It runs from a checkout of the DEFAULT branch, and treats every string that originates in the PR (the
branch name, job names, step names, log text, upstream tag text) as data: validated against a strict
pattern, or printed inside a code fence the content cannot break out of. It reads run metadata and job
logs through the API and writes labels, ONE comment per PR (edited in place) and, when it gives up, ONE
issue. Nothing else.

The state machine (decide()):

    gate green                       -> drop `needs-repair` / `needs-human` (the PR is fine now)
    gate red, PR already needs-human -> nothing (a human has it)
    gate red, repair enabled, attempts used < budget
                                     -> label `needs-repair` + `repair-attempt-<n+1>`, comment the failure
                                        summary, output repair=true (the `repair` job takes it from here)
    gate red otherwise               -> label `needs-human`, drop `needs-repair`, open the needs-human issue

The attempt counter IS the `repair-attempt-<n>` label, incremented here BEFORE the repair job runs, so a
repair that crashes or never pushes still consumes its attempt and the loop is bounded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import regtools as rt
import watch

MARKER = "<!-- registry-watch:failure-summary -->"
LABELS = {
    "bot": ("1d76db", "Opened by automation: the automated-PR guard applies"),
    "bump": ("0e8a16", "An upstream version bump written by tools/watch.py"),
    "needs-repair": ("fbca04", "The gate failed on a bump; the repair step may propose a manifest rewrite"),
    "needs-human": ("d93f0b", "Automation has given up on this bump; a person must look"),
    watch.LICENSE_CHANGED_LABEL: ("b60205", "Upstream's license files changed in this bump; a person must check `license`"),
    watch.KEEP_LABEL: ("c5def5", "Applied by a person: the watcher must not close this bump PR as superseded"),
}
ATTEMPT_LABEL = re.compile(r"^repair-attempt-(\d+)$")
TAIL_LINES = 80
MAX_JOBS_SHOWN = 8
MAX_COMMENT_CHARS = 60_000
MAX_LOG_CHARS = 7_000
FAILED = ("failure", "timed_out")

Gh = Callable[..., str]


class TriageError(Exception):
    pass


def gh(*args: str, input: str | None = None, timeout: int = 120) -> str:
    p = subprocess.run(["gh", *args], input=input, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise TriageError(f"gh {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    action: str                       # ignore | clear | repair | needs-human
    attempt: int = 0
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    why: str = ""


def attempts_used(labels: list[str]) -> int:
    return max([int(m.group(1)) for l in labels if (m := ATTEMPT_LABEL.match(l))] + [0])


def decide(conclusion: str, labels: list[str], budget: int, repair_enabled: bool) -> Decision:
    have = set(labels)
    if conclusion == "success":
        drop = sorted(have & {"needs-repair", "needs-human"})
        return Decision("clear" if drop else "ignore", remove=drop, why="the gate is green")
    if conclusion not in FAILED:
        return Decision("ignore", why=f"conclusion `{conclusion}` is not a verdict")
    if "needs-human" in have:
        return Decision("ignore", why="already handed to a human")
    used = attempts_used(labels)
    if repair_enabled and used < budget:
        n = used + 1
        return Decision("repair", attempt=n, add=["needs-repair", f"repair-attempt-{n}"],
                        why=f"repair attempt {n} of {budget}")
    why = ("the repair step is not enabled" if not repair_enabled
           else f"all {budget} repair attempt(s) are used")
    return Decision("needs-human", attempt=used, add=["needs-human"], remove=["needs-repair"], why=why)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_STAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def tail(text: str, n: int = TAIL_LINES, max_chars: int = MAX_LOG_CHARS) -> str:
    """The last `n` lines of a job log that lead up to its failure, cleaned and capped in size.

    A job's raw log ends with the runner's post-job cleanup (dozens of lines of `git config` and
    `Post job cleanup.`), which would push the actual error out of a plain tail. So the window ENDS at the
    last `##[error]` annotation when there is one, and at the end of the log otherwise. Timestamps,
    colours and control characters are stripped.
    """
    lines = [_CTRL.sub("", _ANSI.sub("", _STAMP.sub("", l))).rstrip() for l in text.splitlines()]
    last_error = max((i for i, l in enumerate(lines) if l.startswith("##[error]")), default=None)
    if last_error is not None:
        lines = lines[:last_error + 1]
    out = "\n".join(lines[-n:])
    if len(out) > max_chars:
        out = "...(cut)\n" + out[-max_chars:]
    return out


def fence(text: str, lang: str = "") -> str:
    """A code block whose fence is longer than any run of backticks inside, so the text cannot close it."""
    longest = max([len(m) for m in re.findall(r"`+", text)] + [2])
    f = "`" * (longest + 1)
    return f"{f}{lang}\n{text}\n{f}"


_SAFE_NAME = re.compile(r"[^A-Za-z0-9 ()._+-]")
_MATRIX_JOB = re.compile(r"^([a-z][a-z0-9_-]*) \((linux|macos|windows)\)$")


def safe(text: str, limit: int = 80) -> str:
    return _SAFE_NAME.sub("?", text)[:limit]


def failed_jobs(jobs: list[dict]) -> list[dict]:
    out = []
    for j in jobs:
        if j.get("conclusion") in FAILED:
            m = _MATRIX_JOB.match(j.get("name", ""))
            steps = [s.get("name", "") for s in j.get("steps") or [] if s.get("conclusion") in FAILED]
            out.append({"id": j["id"], "name": j.get("name", ""), "package": m.group(1) if m else None,
                        "platform": m.group(2) if m else None, "steps": steps})
    return out


def diff_markdown(diff: dict | None, why: str = "") -> str:
    if not diff:
        return f"_Not available{': ' + why if why else ''}._"
    files = diff.get("files") or []
    add = sum(f.get("additions", 0) for f in files)
    dele = sum(f.get("deletions", 0) for f in files)
    out = [f"{len(files)} file(s) changed, +{add} -{dele}, {diff.get('total_commits', '?')} commit(s)"
           + (f" ({diff['html_url']})" if diff.get("html_url") else "")]
    for status in ("added", "removed", "renamed"):
        names = [f["filename"] for f in files if f.get("status") == status]
        if names:
            shown = names[:40]
            out.append(f"* {status} ({len(names)}): " + ", ".join(f"`{safe(n, 120)}`" for n in shown)
                       + (" ..." if len(names) > len(shown) else ""))
    if len(files) >= 300:
        out.append("_(GitHub lists at most 300 files; the real diff may be larger.)_")
    return "\n".join(out)


def summary_markdown(*, pkg: str, old: str, new: str, run_url: str, head_sha: str, d: Decision, budget: int,
                     jobs: list[dict], logs: dict[int, str], diff: dict | None, diff_why: str = "") -> str:
    if d.action == "repair":
        status = f"Repair attempt **{d.attempt} of {budget}** has been scheduled."
    else:
        status = f"**A human is needed**: {d.why}."
    out = [MARKER, f"### The gate failed on the bump of `{pkg}` {old} -> {new}", "",
           status, f"Run: {run_url} (commit `{head_sha[:12]}`)", ""]
    if not jobs:
        out += ["No failed job was found in the run (it may have been cancelled or its jobs expired).", ""]
    else:
        out += ["| job | package | platform | failed step |", "| --- | --- | --- | --- |"]
        for j in jobs[:MAX_JOBS_SHOWN]:
            out.append(f"| {safe(j['name'])} | {j['package'] or '-'} | {j['platform'] or '-'} | "
                       f"{safe(', '.join(j['steps']) or '-', 120)} |")
        if len(jobs) > MAX_JOBS_SHOWN:
            out.append(f"| ... and {len(jobs) - MAX_JOBS_SHOWN} more | | | |")
        out.append("")
        for j in jobs[:MAX_JOBS_SHOWN]:
            body = tail(logs[j["id"]]) if j["id"] in logs else "(log unavailable)"
            out += [f"<details><summary>{safe(j['name'])}: last {TAIL_LINES} log lines</summary>", "",
                    fence(body), "", "</details>", ""]
    out += ["**Upstream change between the two tags**", "", diff_markdown(diff, diff_why), "",
            "_Log and upstream text above is untrusted data quoted from the run; the repair step treats it the same way._"]
    text = "\n".join(out)
    if len(text) > MAX_COMMENT_CHARS:
        text = text[:MAX_COMMENT_CHARS - 200] + "\n\n_(comment truncated)_\n"
    return text


# ---------------------------------------------------------------------------
# GitHub I/O (every call goes through `run`, so tests can feed canned answers)
# ---------------------------------------------------------------------------

def ensure_labels(names: list[str], repo: str, run: Gh = gh) -> None:
    """Create each label that does not exist yet (an existing one is left exactly as it is)."""
    for n in names:
        color, desc = LABELS.get(n, ("ededed", "Repair attempts used on this bump (tools/bump_triage.py)"))
        try:
            run("api", "--method", "POST", f"repos/{repo}/labels", "-f", f"name={n}", "-f", f"color={color}",
                "-f", f"description={desc}")
        except TriageError as e:
            if "already_exists" not in str(e) and "422" not in str(e):
                raise


def add_labels(run: Gh, repo: str, pr: int, names: list[str]) -> None:
    if names:
        args = ["api", "--method", "POST", f"repos/{repo}/issues/{pr}/labels"]
        for n in names:
            args += ["-f", f"labels[]={n}"]
        run(*args)


def remove_label(run: Gh, repo: str, pr: int, name: str) -> None:
    try:
        run("api", "--method", "DELETE", f"repos/{repo}/issues/{pr}/labels/{urllib.parse.quote(name, safe='')}")
    except TriageError as e:
        if "404" not in str(e):
            raise


def find_pr(run: Gh, repo: str, branch: str) -> dict | None:
    owner = repo.split("/")[0]
    out = run("api", f"repos/{repo}/pulls?state=open&per_page=100&head={urllib.parse.quote(owner + ':' + branch, safe='')}",
              "--jq", ".[] | {number, html_url, ref: .head.ref, sha: .head.sha, labels: [.labels[].name]}")
    prs = [json.loads(l) for l in out.splitlines() if l.strip()]
    prs = [p for p in prs if p.get("ref") == branch]
    return prs[0] if prs else None


def run_jobs(run: Gh, repo: str, run_id: str) -> list[dict]:
    out = run("api", "--paginate", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", "--jq",
              ".jobs[] | {id, name, conclusion, steps: [.steps[] | {name, conclusion}]}")
    return [json.loads(l) for l in out.splitlines() if l.strip()]


def job_log(run: Gh, repo: str, job_id: int) -> str:
    try:
        return run("api", f"repos/{repo}/actions/jobs/{job_id}/logs")
    except (TriageError, subprocess.TimeoutExpired):
        return "(log unavailable: expired or not retrievable)"


def upstream_diff(run: Gh, pkg: rt.Package, version: str,
                  ls_remote: Callable[[str], str] = watch.real_ls_remote) -> tuple[dict | None, str]:
    """GitHub's compare of old tag...new tag, when the upstream is on GitHub. (diff, why-not)."""
    git_url = pkg.upstream.get("git", "")
    slug = watch.github_slug(git_url)
    old_tag = rt.release_tag(pkg, pkg.newest)
    if not slug:
        return None, "the upstream is not on GitHub"
    if not old_tag:
        return None, "no tag template"
    try:
        tags = watch.parse_ls_remote(ls_remote(git_url))
    except watch.WatchError as e:
        return None, str(e)
    pattern = watch.tag_pattern(pkg.upstream["tag"])
    new_tag = None
    for name in tags:
        verdict, c = watch.classify_tag(name, pattern, pkg.upstream["tag"], pkg.upstream.get("scheme", "semver"))
        if c and c.version == version:
            new_tag = name
            break
    if not new_tag:
        return None, f"no upstream tag for {version}"
    try:
        q = lambda t: urllib.parse.quote(t, safe="")  # noqa: E731
        out = run("api", f"repos/{slug}/compare/{q(old_tag)}...{q(new_tag)}", "--jq",
                  "{html_url, total_commits, files: [.files[]? | {filename, status, additions, deletions, previous_filename}]}",
                  timeout=90)
        return json.loads(out), ""
    except (TriageError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return None, f"the compare API failed ({str(e)[:100]})"


def upsert_comment(run: Gh, repo: str, pr: int, body: str) -> None:
    """ONE failure-summary comment per PR: edit ours in place if it exists. Only comments by the
    workflow's own identity count, so nobody can pre-empt it by quoting the marker."""
    out = run("api", "--paginate", f"repos/{repo}/issues/{pr}/comments?per_page=100", "--jq",
              f'.[] | select(.user.login == "github-actions[bot]" and (.body | contains("{MARKER}"))) | .id')
    ids = [l.strip() for l in out.splitlines() if l.strip()]
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "body.md"
        f.write_text(body, encoding="utf-8")
        if ids:
            run("api", "--method", "PATCH", f"repos/{repo}/issues/comments/{ids[-1]}", "-F", f"body=@{f}")
        else:
            run("api", "--method", "POST", f"repos/{repo}/issues/{pr}/comments", "-F", f"body=@{f}")


def ensure_issue(run: Gh, repo: str, pkg: str, version: str, pr_url: str, why: str) -> None:
    title = watch.NEEDS_HUMAN_TITLE.format(package=pkg, version=version)
    out = run("api", "--paginate", f"repos/{repo}/issues?state=open&labels=needs-human&per_page=100", "--jq",
              ".[] | select(.pull_request | not) | .title")
    if title in out.splitlines():
        return
    body = (f"Automation gave up on the bump of `{pkg}` to `{version}`: {why}.\n\nPull request: {pr_url}\n\n"
            "The watcher will not open another PR for this version while this issue is open. Fix the PR "
            "(if it is to stay automated it may only change `packages/*/Harbour.toml`, `overlay/` and "
            "`patches/`), or close the PR and this issue.")
    run("api", "--method", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}",
        "-f", "labels[]=needs-human")


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def triage(args, run: Gh = gh, ls_remote: Callable[[str], str] = watch.real_ls_remote) -> dict:
    result = {"repair": "false", "pr": "", "package": "", "version": "", "attempt": "0"}
    parsed = watch.parse_branch(args.head_branch)
    if not parsed:
        print(f"triage: `{safe(args.head_branch, 60)}` is not a bump branch; nothing to do")
        return result
    pkg_name, version = parsed
    if not (rt.PACKAGES / pkg_name / "package.toml").is_file():
        print(f"triage: no package `{pkg_name}` on the default branch; nothing to do")
        return result
    pr = find_pr(run, args.repo, args.head_branch)
    if not pr:
        print("triage: no open PR for this branch; nothing to do")
        return result
    if pr.get("sha") != args.head_sha:
        print("triage: the branch has moved on since this run; a newer run will report")
        return result
    labels = pr.get("labels", [])
    d = decide(args.conclusion, labels, args.budget, args.repair_enabled)
    print(f"triage: {pkg_name} {version}: {d.action} ({d.why})")
    if d.action == "ignore":
        return result
    ensure_labels(d.add, args.repo, run)
    if d.action == "clear":
        for l in d.remove:
            remove_label(run, args.repo, pr["number"], l)
        return result

    pkg = rt.load_package(pkg_name)   # the DEFAULT branch's copy: the old release, the upstream settings
    run_url = f"https://github.com/{args.repo}/actions/runs/{args.run_id}"
    jobs = failed_jobs(run_jobs(run, args.repo, args.run_id))
    logs = {j["id"]: job_log(run, args.repo, j["id"]) for j in jobs[:MAX_JOBS_SHOWN]}
    diff, diff_why = upstream_diff(run, pkg, version, ls_remote)
    body = summary_markdown(pkg=pkg_name, old=pkg.newest.version, new=version, run_url=run_url,
                            head_sha=args.head_sha, d=d, budget=args.budget, jobs=jobs, logs=logs,
                            diff=diff, diff_why=diff_why)
    upsert_comment(run, args.repo, pr["number"], body)
    add_labels(run, args.repo, pr["number"], d.add)
    for l in d.remove:
        remove_label(run, args.repo, pr["number"], l)
    if d.action == "needs-human":
        ensure_issue(run, args.repo, pkg_name, version, pr["html_url"], d.why)
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.md").write_text(body, encoding="utf-8")
        (out / "upstream-diff.json").write_text(json.dumps(diff or {"unavailable": diff_why}, indent=2), encoding="utf-8")
        (out / "context.json").write_text(json.dumps({
            "package": pkg_name, "old_version": pkg.newest.version, "new_version": version,
            "branch": args.head_branch, "pr": pr["number"], "attempt": d.attempt, "budget": args.budget,
            "failed_jobs": jobs}, indent=2), encoding="utf-8")
    if d.action == "repair":
        result.update(repair="true", pr=str(pr["number"]), package=pkg_name, version=version, attempt=str(d.attempt))
    return result


def to_bool(text: str) -> bool:
    return text.strip().lower() == "true"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ensure-labels", help="create the bot/bump/needs-repair/needs-human labels if missing")
    p.add_argument("--repo", required=True)
    p = sub.add_parser("triage")
    p.add_argument("--repo", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--head-branch", required=True)
    p.add_argument("--head-sha", required=True)
    p.add_argument("--conclusion", required=True)
    p.add_argument("--budget", type=int, default=2)
    p.add_argument("--repair-enabled", type=to_bool, default=False)
    p.add_argument("--out-dir")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ensure-labels":
            ensure_labels(list(LABELS), args.repo)
            return 0
        if not 0 <= args.budget <= 10:
            raise TriageError("--budget must be between 0 and 10")
        if not re.fullmatch(r"[0-9]+", args.run_id) or not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
            raise TriageError("--run-id and --head-sha look wrong")
        result = triage(args)
    except (TriageError, watch.WatchError, rt.RegistryError) as e:
        print(f"bump_triage.py: {e}", file=sys.stderr)
        return 1
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in result.items():
                f.write(f"{k}={v}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
