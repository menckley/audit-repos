"""Outils partagés : configuration, API GitHub, appel à Claude Code, git."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
OWNER: str = CONFIG["owner"]
PUBLIC_REPO: str = CONFIG["public_repo"]
SECURITY_REPO: str = CONFIG["security_repo"]
DRY_RUN = os.environ.get("DRY_RUN") == "1"

API = "https://api.github.com"
MARKER_RE = re.compile(r"<!-- audit-finding: (?P<repo>[\w.-]+)/(?P<id>[\w.-]+) -->")
AGENT_MARK = "<!-- agent -->"

# Variables d'environnement à ne JAMAIS transmettre à Claude.
SECRET_ENV = ("GH_PAT", "GITHUB_TOKEN", "GH_TOKEN")

LABEL_COLORS = {
    "audit": "5319e7",
    "à-valider": "fbca04",
    "approuvé": "0e8a16",
    "refusé": "b60205",
    "en-cours": "1d76db",
    "pr-ouverte": "0052cc",
    "spec-en-revue": "c5def5",
    "analyse-livrée": "bfdadc",
    "à-préciser": "d93f0b",
    "échec": "b60205",
    "non-codable": "ededed",
    "récap": "000000",
}


def tmp_dir(*parts: str) -> Path:
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    path = base.joinpath("audit-agent", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def render(template_name: str, values: dict[str, str]) -> str:
    text = (ROOT / "agent" / "prompts" / template_name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[… tronqué, {len(text) - limit} caractères de plus]"


# --------------------------------------------------------------------------- GitHub


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHub:
    def __init__(self, token: str | None):
        self.token = token
        self._labels: dict[str, set[str]] = {}

    def _call(self, method: str, path: str, data=None, params=None):
        url = path if path.startswith("http") else API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "audit-repos-agent",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise GitHubError(exc.code, f"{method} {path} → {exc.code} {detail}") from None

    def get(self, path, **params):
        return self._call("GET", path, params=params or None)

    def write(self, method: str, path: str, data=None):
        if DRY_RUN:
            print(f"[dry-run] {method} {path} {json.dumps(data, ensure_ascii=False)[:300]}")
            return {"number": 0, "html_url": "https://example.invalid/dry-run"}
        return self._call(method, path, data=data)

    def paginate(self, path, **params):
        page = 1
        while True:
            items = self.get(path, per_page=100, page=page, **params) or []
            yield from items
            if len(items) < 100:
                return
            page += 1

    # --- repos
    def public_repos(self) -> list[dict]:
        repos = [
            r for r in self.paginate(f"/users/{OWNER}/repos", type="owner")
            if not r["private"] and not r["fork"] and not r["archived"]
            and r["name"] not in CONFIG["exclude"]
        ]
        return sorted(repos, key=lambda r: r["name"].lower())

    def repo(self, name: str) -> dict:
        return self.get(f"/repos/{OWNER}/{name}")

    def file_exists(self, repo: str, path: str, ref: str) -> bool:
        try:
            self.get(f"/repos/{OWNER}/{repo}/contents/{urllib.parse.quote(path)}", ref=ref)
            return True
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise

    # --- issues
    def issues(self, repo: str, state: str = "open", labels: str | None = None) -> list[dict]:
        params = {"state": state}
        if labels:
            params["labels"] = labels
        try:
            return [i for i in self.paginate(f"/repos/{OWNER}/{repo}/issues", **params)
                    if "pull_request" not in i]
        except GitHubError as exc:
            if exc.status == 404 and DRY_RUN:
                return []
            raise

    def comments(self, repo: str, number: int) -> list[dict]:
        return list(self.paginate(f"/repos/{OWNER}/{repo}/issues/{number}/comments"))

    def ensure_label(self, repo: str, name: str) -> None:
        known = self._labels.setdefault(repo, set())
        if name in known:
            return
        color = LABEL_COLORS.get(name) or LABEL_COLORS.get(name.split(":")[0], "cfd3d7")
        try:
            self.write("POST", f"/repos/{OWNER}/{repo}/labels", {"name": name, "color": color})
        except GitHubError as exc:
            if exc.status != 422:  # 422 = existe déjà
                raise
        known.add(name)

    def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict:
        for label in labels:
            self.ensure_label(repo, label)
        return self.write("POST", f"/repos/{OWNER}/{repo}/issues",
                          {"title": title[:250], "body": body, "labels": labels})

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        for label in labels:
            self.ensure_label(repo, label)
        self.write("POST", f"/repos/{OWNER}/{repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, repo: str, number: int, label: str) -> None:
        try:
            self.write("DELETE", f"/repos/{OWNER}/{repo}/issues/{number}/labels/"
                       + urllib.parse.quote(label, safe=""))
        except GitHubError as exc:
            if exc.status != 404:
                raise

    def comment(self, repo: str, number: int, body: str) -> None:
        self.write("POST", f"/repos/{OWNER}/{repo}/issues/{number}/comments",
                   {"body": f"{AGENT_MARK}\n{body}"})

    # --- pull requests
    def open_pr_for(self, repo: str, branch: str) -> dict | None:
        prs = self.get(f"/repos/{OWNER}/{repo}/pulls", head=f"{OWNER}:{branch}", state="open")
        return prs[0] if prs else None

    def create_pr(self, repo: str, branch: str, base: str, title: str, body: str) -> dict:
        return self.write("POST", f"/repos/{OWNER}/{repo}/pulls",
                          {"title": title[:250], "head": branch, "base": base, "body": body})


# --------------------------------------------------------------------------- Claude


class QuotaExhausted(Exception):
    """Le quota de l'abonnement est épuisé : on reprendra au prochain passage."""


class ClaudeError(Exception):
    pass


QUOTA_SIGNS = ("usage limit", "limit reached", "rate limit", "rate_limit", "quota", "overloaded")


def run_claude(prompt: str, cwd: Path, allowed_tools: list[str], max_turns: int) -> str:
    """Lance Claude Code en mode non interactif. Les tokens GitHub ne lui sont pas transmis."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", CONFIG["model"],
        "--max-turns", str(max_turns),
        # Les fichiers de sortie (constats, description de PR) sont écrits à côté du clone.
        "--add-dir", str(cwd.parent),
        # Un argument par outil : certains motifs contiennent des espaces (« Bash(git log:*) »).
        "--allowedTools", *allowed_tools,
    ]
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=CONFIG["claude_timeout_minutes"] * 60)
    except subprocess.TimeoutExpired:
        raise ClaudeError("Claude a dépassé le temps imparti") from None
    out = proc.stdout.strip()
    data: dict = {}
    if out:
        try:
            data = json.loads(out.splitlines()[-1])
        except json.JSONDecodeError:
            data = {}
    failed = proc.returncode != 0 or data.get("is_error")
    if failed:
        blob = f"{out[-3000:]} {proc.stderr[-3000:]}".lower()
        if any(sign in blob for sign in QUOTA_SIGNS):
            raise QuotaExhausted(truncate(blob, 300))
        # Nombre de tours atteint : le travail fait reste exploitable, l'appelant vérifie.
        if data.get("subtype") == "error_max_turns":
            print("⚠️  Claude a atteint le nombre maximal de tours.")
            return data.get("result", "")
        raise ClaudeError(truncate(f"code {proc.returncode} : {proc.stderr or out}", 600))
    return data.get("result", "")


# --------------------------------------------------------------------------- git


def sh(*args: str, cwd: Path | None = None, check: bool = True, timeout: int = 600) -> str:
    proc = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(scrub(f"{args[0]} {args[1] if len(args) > 1 else ''} a échoué : "
                                 f"{truncate(proc.stderr or proc.stdout, 500)}"))
    return proc.stdout


def scrub(text: str) -> str:
    """Retire toute valeur de token d'un message avant de l'afficher ou de le publier."""
    for name in SECRET_ENV + ("CLAUDE_CODE_OAUTH_TOKEN",):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "***")
    return text


def fresh_clone(repo: str, dest: Path) -> Path:
    if dest.exists():
        shutil.rmtree(dest)
    sh("git", "clone", "--quiet", f"https://github.com/{OWNER}/{repo}.git", str(dest), timeout=900)
    return dest
