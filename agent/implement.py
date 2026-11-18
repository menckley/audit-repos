"""Agent d'exécution : lancé toutes les heures et dès qu'une case « Lancer » est cochée.

À chaque passage, dans l'ordre :
1. migre les anciennes issues vers le nouveau format (cases à cocher, mode auto) ;
2. fait avancer les PR en attente : merge si la CI est verte, une tentative de correction
   si elle est rouge, sinon renvoi vers le propriétaire ;
3. démarre de nouvelles tâches : maintenance et sécurité automatiquement, les autres
   quand la case « Lancer » est cochée (« Refuser » ferme l'issue) ;
4. merge les mises à jour Dependabot non majeures dont la CI est verte ;
5. surveille les repos après chaque merge et annule le changement si un workflow casse ;
6. publie un bilan détaillé (envoyé par e-mail) une fois tout le travail terminé.

L'agent pousse et merge avec le token GitHub, que Claude ne voit jamais.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from common import (AGENT_MARK, CONFIG, DECISION_START, LATER_RE, LAUNCH_RE, MARKER_RE, OWNER, PUBLIC_REPO, REFUSE_RE,
                    REPAIR_BACK, REPAIR_MARK,
                    ROOT, SECURITY_REPO, V2, GitHub, GitHubError, QuotaExhausted, commit_state,
                    compose_body, decision_block, footer, refresh_decision, fresh_clone, is_auto, render,
                    run_claude, scrub, set_decision, sh, tmp_dir, truncate)

READ_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch",
              "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"]
CODE_TOOLS = READ_TOOLS + [
    "Edit", "Write",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git mv:*)", "Bash(git rm:*)", "Bash(mkdir:*)", "Bash(git ls-remote:*)",
    "Bash(pip install:*)", "Bash(python -m pip install:*)", "Bash(python -m pytest:*)",
    "Bash(pytest:*)", "Bash(python:*)", "Bash(ruff:*)",
]
STATE_PATH = ROOT / "state" / "agent.json"
# Labels qui signifient « déjà pris en charge » ou « attend le propriétaire ».
BUSY = {"en-cours", "pr-ouverte"}
STOPPED = BUSY | {"à-relire", "échec", "à-préciser", "annulé", "analyse-livrée", "mergé"}
BAD_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
RELAUNCH = "l'agent recommence la tâche en tenant compte de tes commentaires"


class NoChange(Exception):
    """Claude n'a rien commité : il explique pourquoi (tâche ambiguë, infaisable…)."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- état


def load_state() -> dict:
    state = {"pending": [], "watch": [], "log": [], "last_bilan": None, "seen": []}
    if STATE_PATH.exists():
        state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    return state


SECURITY_TITLE = "Correctif de sécurité"


def sanitize(entry: dict) -> None:
    """Ce fichier d'état est public : aucun détail d'une faille ne doit y figurer."""
    if not entry.get("security"):
        return
    entry["title"] = SECURITY_TITLE
    for key in ("detail", "human"):
        if entry.get(key):
            entry[key] = "Voir l'issue privée."


def save_state(state: dict) -> None:
    for entry in state["pending"] + state["watch"] + state["log"]:
        sanitize(entry)
    limit = iso(now() - timedelta(days=90))
    state["log"] = [e for e in state["log"] if not e.get("reported") or e["at"] >= limit]
    state["seen"] = state["seen"][-500:]
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    commit_state([STATE_PATH], "chore(agent): état")


def log(state: dict, kind: str, **fields) -> None:
    entry = {"type": kind, "at": iso(now()), "reported": False, **fields}
    sanitize(entry)
    state["log"].append(entry)


def once(state: dict, key: str) -> bool:
    """Vrai la première fois qu'une clé est vue (évite de répéter un message)."""
    if key in state["seen"]:
        return False
    state["seen"].append(key)
    return True


# --------------------------------------------------------------------------- issues


def labels_of(issue: dict) -> set[str]:
    return {label["name"] for label in issue["labels"]}


def kind_of(labels: set[str]) -> str:
    return next((n.split(":", 1)[1] for n in labels if n.startswith("type:")), "")


def cat_of(labels: set[str]) -> str:
    return next((n.split(":", 1)[1] for n in labels if n.startswith("cat:")), "")


def issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{OWNER}/{repo}/issues/{number}"


def relabel(gh: GitHub, repo: str, number: int, remove: list[str], add: list[str]) -> None:
    for label in remove:
        gh.remove_label(repo, number, label)
    if add:
        gh.add_labels(repo, number, add)


def reopen_decision(gh: GitHub, repo: str, issue: dict) -> None:
    """Remet les cases à cocher pour que le propriétaire puisse relancer en un clic."""
    body = set_decision(issue.get("body") or "", decision_block(RELAUNCH))
    gh.update_issue(repo, issue["number"], body=body)
    issue["body"] = body


def migrate_backlog(gh: GitHub, issues: dict[str, list[dict]]) -> None:
    """Passe les anciennes issues au format actuel (une seule fois) : cases à cocher, mode
    auto, puis case « Plus tard » ajoutée aux blocs de décision qui ne l'ont pas."""
    for repo, items in issues.items():
        for issue in items:
            body = issue.get("body") or ""
            if V2 in body and DECISION_START in body and "⏸ **Plus tard**" not in body \
                    and not LAUNCH_RE.search(body) and issue["user"]["login"].lower() == OWNER.lower():
                body = refresh_decision(body)
                gh.update_issue(repo, issue["number"], body=body)
                issue["body"] = body
                print(f"  case « Plus tard » ajoutée : {repo}#{issue['number']}")
                continue
            marker = MARKER_RE.search(body)
            if V2 in body or not marker or issue["user"]["login"].lower() != OWNER.lower():
                continue
            labels = labels_of(issue)
            effort = re.search(r"\*\*Effort :\*\* (\w+)", body)
            codable = "non-codable" not in labels
            auto = codable and is_auto(cat_of(labels), effort[1] if effort else "M", False)
            content = re.split(r"\n\n---\n\n✅", body, maxsplit=1)[0].strip()
            new_body = compose_body(content, marker["repo"], marker["id"],
                                    footer(auto, kind_of(labels), codable))
            gh.update_issue(repo, issue["number"], body=new_body)
            issue["body"] = new_body
            if auto:
                relabel(gh, repo, issue["number"], ["à-valider"], ["auto"])
                issue["labels"].append({"name": "auto"})
            print(f"  migrée : {repo}#{issue['number']} ({'auto' if auto else 'à décider'})")


# --------------------------------------------------------------------------- CI


def ci_status(gh: GitHub, repo: str, sha: str, since: datetime) -> tuple[str, list[dict]]:
    """« success », « failure », « wait » ou « none » (aucune CI n'a tourné)."""
    runs = gh.check_runs(repo, sha)
    if not runs:
        waited = now() - since > timedelta(minutes=CONFIG["ci_wait_minutes"])
        return ("none" if waited else "wait"), []
    if any(r["status"] != "completed" for r in runs):
        return "wait", runs
    failed = [r for r in runs if r["conclusion"] in BAD_CONCLUSIONS]
    return ("failure" if failed else "success"), failed


def failure_report(gh: GitHub, repo: str, failed: list[dict]) -> str:
    parts = []
    for run in failed[:5]:
        out = run.get("output") or {}
        notes = [f"- {a.get('path')}:{a.get('start_line')} {a.get('message')}"
                 for a in gh.annotations(repo, run["id"])[:20]]
        parts.append(f"### {run['name']} → {run['conclusion']}\n"
                     f"{truncate(out.get('summary') or '', 1500)}\n"
                     f"{truncate(out.get('text') or '', 3000)}\n" + "\n".join(notes))
    return "\n\n".join(parts) or "(aucun détail disponible)"


# --------------------------------------------------------------------------- travail Claude


def owner_comments(gh: GitHub, repo: str, number: int) -> str:
    notes = [c["body"] for c in gh.comments(repo, number)
             if c["user"]["login"].lower() == OWNER.lower() and AGENT_MARK not in c["body"]]
    return "\n\n---\n\n".join(notes) or "(aucun)"


def read_report(path: Path) -> tuple[str, str]:
    """Fichier écrit par Claude : 1re ligne = titre, le reste = description."""
    if not path.exists():
        return "", ""
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        return "", ""
    title = lines[0].removeprefix("TITRE:").removeprefix("TITLE:").strip(" #")
    return title, "\n".join(lines[1:]).strip()


def git_identity(repo_dir: Path) -> None:
    sh("git", "config", "user.name", f"{OWNER} (agent)", cwd=repo_dir)
    sh("git", "config", "user.email", f"{OWNER}@users.noreply.github.com", cwd=repo_dir)


def push(repo_dir: Path, target: str, branch: str, force: bool) -> None:
    token = os.environ["GH_PAT"]
    args = ["git", "push", "-q"] + (["--force"] if force else [])
    sh(*args, f"https://x-access-token:{token}@github.com/{OWNER}/{target}.git",
       f"HEAD:refs/heads/{branch}", cwd=repo_dir)


# --------------------------------------------------------------------------- hygiène

GENERATED_RE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.ruff_cache|\.mypy_cache|\.tox|\.venv|venv|"
                          r"[^/]+\.egg-info)(/|$)|\.py[co]$|(^|/)\.DS_Store$")
GITIGNORE_LINES = ["__pycache__/", "*.py[co]", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
                   ".tox/", ".venv/", "venv/", "*.egg-info/", ".DS_Store"]


def clean_generated(repo_dir: Path) -> bool:
    """Retire du suivi git les fichiers générés (caches Python, environnements virtuels…) et
    complète .gitignore. Vrai si un commit de nettoyage a été créé."""
    junk = [p for p in sh("git", "ls-files", cwd=repo_dir).splitlines() if GENERATED_RE.search(p)]
    if not junk:
        return False
    sh("git", "rm", "-r", "-q", "--cached", "--", *junk, cwd=repo_dir)
    gitignore = repo_dir / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if missing:
        prefix = "" if not existing or existing[-1] == "" else "\n"
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(prefix + "# Fichiers générés\n" + "\n".join(missing) + "\n")
        sh("git", "add", ".gitignore", cwd=repo_dir)
    sh("git", "commit", "-q", "-m", "chore: retirer les fichiers générés et compléter .gitignore\n\n"
       "Co-Authored-By: Claude <noreply@anthropic.com>", cwd=repo_dir)
    print(f"  🧹 {len(junk)} fichier(s) généré(s) retiré(s) du suivi git")
    return True


def code_task(gh: GitHub, target: str, branch: str, values: dict[str, str],
              issue_ref: str | None) -> dict:
    """Clone, laisse Claude travailler sur une branche, pousse et ouvre (ou réutilise) la PR."""
    base = gh.repo(target)["default_branch"]
    work = tmp_dir("implement")
    repo_dir = fresh_clone(target, work / target)
    sh("git", "checkout", "-q", "-B", branch, cwd=repo_dir)
    git_identity(repo_dir)

    report = work / f"{branch.replace('/', '-')}-pr.md"
    report.unlink(missing_ok=True)
    prompt = render("implement.md", {**values, "PR_PATH": str(report), "BASE": base})
    run_claude(prompt, repo_dir, CODE_TOOLS, CONFIG["implement_max_turns"])

    title, body = read_report(report)
    commits = int(sh("git", "rev-list", "--count", f"origin/{base}..HEAD", cwd=repo_dir).strip())
    if commits == 0:
        raise NoChange(body or title or "Claude n'a produit aucun commit, sans explication.")
    clean_generated(repo_dir)
    push(repo_dir, target, branch, force=True)

    footer_lines = ["", "---"]
    if issue_ref:
        footer_lines.append(f"Closes {issue_ref}")
    footer_lines.append("🛠️ Préparée par le Développeur (Claude Code). Mergée automatiquement "
                        "si la CI est verte.")
    full_body = (body or "_(pas de description)_") + "\n" + "\n".join(footer_lines)
    return gh.open_pr_for(target, branch) or gh.create_pr(
        target, branch, base, title or f"Tâche d'audit {branch}", full_body)


def fix_ci(gh: GitHub, entry: dict, failed: list[dict], pr_title: str) -> bool:
    """Une tentative de correction d'une CI rouge. Vrai si un correctif a été poussé."""
    target, branch = entry["target"], entry["branch"]
    work = tmp_dir("fix")
    repo_dir = fresh_clone(target, work / target)
    sh("git", "checkout", "-q", "-B", branch, f"origin/{branch}", cwd=repo_dir)
    git_identity(repo_dir)
    before = sh("git", "rev-parse", "HEAD", cwd=repo_dir).strip()
    prompt = render("fix_ci.md", {"OWNER": OWNER, "REPO": target, "TITLE": pr_title,
                                   "FAILURES": failure_report(gh, target, failed)})
    run_claude(prompt, repo_dir, CODE_TOOLS, CONFIG["implement_max_turns"])
    if sh("git", "rev-parse", "HEAD", cwd=repo_dir).strip() == before:
        return False
    clean_generated(repo_dir)
    push(repo_dir, target, branch, force=False)
    return True


# --------------------------------------------------------------------------- tâches


def start_task(gh: GitHub, state: dict, repo: str, issue: dict, manual: bool) -> None:
    number = issue["number"]
    body = issue.get("body") or ""
    marker = MARKER_RE.search(body)
    labels = labels_of(issue)
    security = repo == SECURITY_REPO
    title = issue["title"]
    print(f"\n▶ {repo}#{number} {title}")

    relabel(gh, repo, number, ["approuvé", "à-valider", "à-préciser", "échec", "à-relire",
                               "annulé"], ["en-cours"])
    if manual:
        stamp = f"✅ Lancée le {now():%d/%m/%Y à %H:%M} UTC."
        gh.update_issue(repo, number, body=set_decision(body, stamp))
        issue["body"] = set_decision(body, stamp)

    try:
        if not marker:
            raise NoChange("Issue sans marqueur d'audit : je ne sais pas à quel repo elle se rapporte.")
        target, finding_id = marker["repo"], marker["id"]
        kind = kind_of(labels)
        values = {
            "OWNER": OWNER, "REPO": target, "ID": finding_id, "TITLE": title,
            "BODY": body.split("\n---\n")[0], "NOTES": owner_comments(gh, repo, number),
            "SECURITY": "oui" if security else "non",
            "FEATURE": ("oui : rédige d'abord `docs/specs/" + finding_id + ".md`"
                        if kind == "fonctionnalite" else "non"),
        }

        if "non-codable" in labels or kind == "marche":
            work = tmp_dir("analyse")
            repo_dir = fresh_clone(target, work / target)
            out = work / f"{finding_id}-analyse.md"
            out.unlink(missing_ok=True)
            run_claude(render("analysis.md", {**values, "OUT_PATH": str(out)}), repo_dir,
                       READ_TOOLS + ["Write"], CONFIG["implement_max_turns"])
            if not out.exists():
                raise RuntimeError("Claude n'a pas produit l'analyse")
            gh.comment(repo, number, truncate(out.read_text(encoding="utf-8"), 60000))
            relabel(gh, repo, number, ["en-cours"], ["analyse-livrée"])
            log(state, "analysis", title=title, issue=issue_url(repo, number), security=security)
            return

        # Pour la sécurité, l'issue est privée : la PR publique n'y fait pas référence.
        ref = None if security else issue_url(repo, number)
        pr = code_task(gh, target, f"agent/{finding_id}", values, ref)
        state["pending"].append({
            "target": target, "number": pr["number"], "url": pr["html_url"],
            "branch": f"agent/{finding_id}", "title": title, "issue_repo": repo,
            "issue": number, "security": security, "opened": iso(now()),
            "fix_attempted": False, "branch_updated": False,
            "human": human_action(body),
        })
        sanitize(state["pending"][-1])
        gh.comment(repo, number, f"🔧 Pull Request ouverte : {pr['html_url']}\n\n"
                   "Elle sera mergée automatiquement dès que la CI sera verte.")
        relabel(gh, repo, number, ["en-cours"], ["pr-ouverte"])
    except QuotaExhausted:
        # La tâche repart en file d'attente telle quelle.
        relabel(gh, repo, number, ["en-cours"], ["approuvé"] if manual else [])
        raise
    except NoChange as exc:
        gh.comment(repo, number, "🤔 Je n'ai pas modifié le code :\n\n" + truncate(str(exc), 5000)
                   + "\n\nRéponds en commentaire, puis coche **Lancer** pour que je recommence.")
        relabel(gh, repo, number, ["en-cours"], ["à-préciser"])
        reopen_decision(gh, repo, issue)
        log(state, "clarify", title=title, issue=issue_url(repo, number), security=security,
            detail=truncate(str(exc), 300))
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {exc}")
        gh.comment(repo, number, "❌ Échec technique de l'agent :\n\n```\n"
                   + truncate(scrub(str(exc)), 1500) + "\n```\n\nCoche **Lancer** pour réessayer.")
        relabel(gh, repo, number, ["en-cours"], ["échec"])
        reopen_decision(gh, repo, issue)
        log(state, "failed", title=title, issue=issue_url(repo, number), security=security,
            detail=truncate(scrub(str(exc)), 300))


def human_action(body: str) -> str:
    m = re.search(r"### 👤 Action de ta part\n\n(.+?)(?:\n\n###|\n\n---|\Z)", body, re.S)
    return m[1].strip() if m else ""


# --------------------------------------------------------------------------- CI d'abord

CI_ID = "ci-pull-requests"
CI_TITLE_RE = re.compile(r"\bCI\b|intégration continue|GitHub Actions.*tests", re.I)


def has_pr_ci(gh: GitHub, repo: str, cache: dict[str, bool]) -> bool:
    """Vrai si un workflow du repo tourne sur les Pull Requests."""
    if repo not in cache:
        cache[repo] = False
        try:
            files = gh.get(f"/repos/{OWNER}/{repo}/contents/.github/workflows") or []
        except GitHubError:
            files = []
        for f in files:
            if f["name"].endswith((".yml", ".yaml")):
                content = gh.get(f"/repos/{OWNER}/{repo}/contents/{f['path']}")
                text = base64.b64decode(content.get("content", "")).decode("utf-8", "replace")
                if "pull_request" in text:
                    cache[repo] = True
                    break
    return cache[repo]


def is_ci_task(issue: dict) -> bool:
    marker = MARKER_RE.search(issue.get("body") or "")
    return bool(marker and marker["id"] == CI_ID) or (
        "type:outillage" in labels_of(issue) and bool(CI_TITLE_RE.search(issue["title"])))


def ensure_ci_task(gh: GitHub, target: str, open_issues: list[dict]) -> dict:
    """Renvoie la tâche « ajouter une CI » du repo, en la créant au besoin."""
    for issue in open_issues:
        marker = MARKER_RE.search(issue.get("body") or "")
        if marker and marker["repo"] == target and is_ci_task(issue):
            return issue
    content = "\n\n".join([
        f"**Repo :** [{OWNER}/{target}](https://github.com/{OWNER}/{target}) · "
        "**Catégorie :** Maintenance · **Type :** outillage · **Priorité :** haute · **Effort :** S",
        "### Constat",
        "Aucun workflow ne lance les tests sur les Pull Requests. Sans cette vérification, "
        "l'agent ne peut merger automatiquement aucun changement dans ce repo.",
        "### Proposition",
        "- Ajouter `.github/workflows/ci.yml` déclenché sur `pull_request` et sur `push` vers la "
        "branche principale, avec `permissions: contents: read`.\n"
        "- Installer les dépendances du projet, lancer `ruff check` (règles de base) et `pytest`.\n"
        "- S'il n'existe aucun test, ajouter un test de fumée qui importe les modules principaux.\n"
        "- Ajouter `.github/dependabot.yml` (pip et github-actions, hebdomadaire).\n"
        "- Ne modifier aucun workflow existant (collecte, publication…).",
    ])
    issue = gh.create_issue(PUBLIC_REPO, f"[{target}] Ajouter une CI (tests sur les Pull Requests)",
                            compose_body(content, target, CI_ID, footer(True, "outillage", True)),
                            ["audit", "auto", f"repo:{target}", "cat:maintenance",
                             "type:outillage", "priorité:haute"])
    issue.setdefault("labels", [{"name": "auto"}, {"name": "type:outillage"}])
    issue.setdefault("user", {"login": OWNER})
    issue.setdefault("title", f"[{target}] Ajouter une CI")
    print(f"  + tâche CI créée pour {target}")
    return issue


def ci_first(gh: GitHub, state: dict, queue: list, open_issues: list[dict]) -> list:
    """Dans un repo sans CI, seule la tâche « ajouter une CI » peut démarrer."""
    cache: dict[str, bool] = {}
    result, blocked = [], set()
    for repo, issue, manual in queue:
        marker = MARKER_RE.search(issue.get("body") or "")
        target = marker["repo"] if marker else None
        codable = "non-codable" not in labels_of(issue) and kind_of(labels_of(issue)) != "marche"
        if not target or not codable or is_ci_task(issue) or has_pr_ci(gh, target, cache):
            result.append((repo, issue, manual))
            continue
        blocked.add(target)
        if manual and once(state, f"wait-ci:{repo}#{issue['number']}"):
            gh.comment(repo, issue["number"], f"⏳ `{target}` n'a pas encore de CI : je l'ajoute "
                       "d'abord, puis je lance cette tâche automatiquement.")
    for target in sorted(blocked):
        ci = ensure_ci_task(gh, target, open_issues)
        if not labels_of(ci) & STOPPED and all(t[1]["number"] != ci["number"] for t in result):
            result.insert(0, (PUBLIC_REPO, ci, False))
    return result


# --------------------------------------------------------------------------- PR en attente


def pull_with_mergeable(gh: GitHub, repo: str, number: int) -> dict:
    """GitHub calcule « mergeable » à la demande : on repose la question quelques fois."""
    pr = gh.pull(repo, number)
    for _ in range(6):
        if pr.get("mergeable") is not None or pr.get("state") != "open":
            break
        time.sleep(5)
        pr = gh.pull(repo, number)
    return pr


def advance_pending(gh: GitHub, state: dict, can_claude: bool) -> bool:
    """Fait avancer les PR ouvertes par l'agent. Renvoie False si le quota a été épuisé."""
    for entry in list(state["pending"]):
        target, number = entry["target"], entry["number"]
        pr = pull_with_mergeable(gh, target, number)
        if pr["merged"]:
            finish_merged(gh, state, entry, pr.get("merge_commit_sha"))
            continue
        if pr["state"] == "closed":  # fermée à la main par le propriétaire
            state["pending"].remove(entry)
            relabel(gh, entry["issue_repo"], entry["issue"], ["pr-ouverte"], [])
            continue
        status, failed = ci_status(gh, target, pr["head"]["sha"], parse(entry["opened"]))
        print(f"  PR {target}#{number} : CI {status}")
        if status == "wait":
            continue
        if status == "none":
            send_back(gh, state, entry, "aucune CI n'a tourné sur cette PR : impossible de "
                      "vérifier le changement automatiquement.")
        elif status == "failure":
            if entry["fix_attempted"] or not can_claude:
                if entry["fix_attempted"]:
                    send_back(gh, state, entry, "la CI reste rouge après une tentative de correction.")
                continue
            try:
                entry["fix_attempted"] = True
                entry["opened"] = iso(now())
                if not fix_ci(gh, entry, failed, pr["title"]):
                    send_back(gh, state, entry, "la CI est rouge et je n'ai pas trouvé de correction sûre.")
            except QuotaExhausted:
                entry["fix_attempted"] = False
                return False
            except Exception as exc:  # noqa: BLE001
                send_back(gh, state, entry, f"la correction de la CI a échoué ({truncate(scrub(str(exc)), 200)}).")
        elif pr["mergeable"] is None:
            continue  # GitHub calcule encore
        elif pr["mergeable"] is False:
            if not entry["branch_updated"]:
                entry["branch_updated"] = True
                entry["opened"] = iso(now())
                try:
                    gh.update_branch(target, number)
                    continue
                except GitHubError:
                    pass  # conflit réel : on refait la tâche
            if once(state, f"redo:{target}#{number}"):
                redo(gh, state, entry)
            else:
                send_back(gh, state, entry, "conflit avec la branche principale, même après "
                          "avoir refait la tâche sur la version à jour.")
        else:
            merged = gh.merge_pr(target, number, pr["title"])
            finish_merged(gh, state, entry, merged.get("sha"))
    return True


def redo(gh: GitHub, state: dict, entry: dict) -> None:
    """Conflit : la tâche repart en file et sera refaite à partir de la branche principale
    à jour (même branche, même PR)."""
    state["pending"].remove(entry)
    repo, number = entry["issue_repo"], entry["issue"]
    issue = gh.get(f"/repos/{OWNER}/{repo}/issues/{number}")
    requeue = [] if "auto" in labels_of(issue) else ["approuvé"]
    relabel(gh, repo, number, ["pr-ouverte"], requeue)
    gh.comment(repo, number, "🔁 Conflit avec des changements mergés entre-temps : je refais la "
               "tâche à partir de la version à jour.")
    print(f"  🔁 à refaire (conflit) : {entry['url']}")


def finish_merged(gh: GitHub, state: dict, entry: dict, sha: str | None) -> None:
    state["pending"].remove(entry)
    gh.delete_branch(entry["target"], entry["branch"])
    repo, number = entry["issue_repo"], entry["issue"]
    gh.comment(repo, number, f"✅ Mergée automatiquement (CI verte) : {entry['url']}")
    relabel(gh, repo, number, ["pr-ouverte"], ["mergé"])
    gh.close_issue(repo, number)
    state["watch"].append({"target": entry["target"], "sha": sha, "merged": iso(now()),
                           "pr": entry["url"], "title": entry["title"], "issue_repo": repo,
                           "issue": number, "security": entry["security"]})
    log(state, "merged", title=entry["title"], pr=entry["url"], issue=issue_url(repo, number),
        security=entry["security"], repo=entry["target"])
    if entry.get("human"):
        log(state, "human", title=entry["title"], issue=issue_url(repo, number),
            security=entry["security"], detail=entry["human"])
    print(f"  ✓ mergée : {entry['url']}")


def send_back(gh: GitHub, state: dict, entry: dict, reason: str) -> None:
    """Renvoie la tâche au propriétaire : PR laissée ouverte, cases à cocher remises."""
    state["pending"].remove(entry)
    repo, number = entry["issue_repo"], entry["issue"]
    gh.comment(repo, number, f"⚠️ Je ne merge pas automatiquement : {reason}\n\n"
               f"La PR reste ouverte : {entry['url']}. Tu peux la merger toi-même, ou "
               "commenter puis cocher **Lancer** pour que je recommence.")
    relabel(gh, repo, number, ["pr-ouverte"], ["à-relire"])
    issue = {"number": number, "body": gh.get(f"/repos/{OWNER}/{repo}/issues/{number}")["body"]}
    reopen_decision(gh, repo, issue)
    log(state, "review", title=entry["title"], pr=entry["url"], issue=issue_url(repo, number),
        security=entry["security"], detail=reason)


# --------------------------------------------------------------------------- Dependabot


VERSION_RE = re.compile(r"from \S*?(\d+)\S* to \S*?(\d+)\S*")


def is_major(pr: dict) -> bool:
    if pr["head"]["ref"].startswith("dependabot/github_actions/"):
        return False  # versions d'actions : la CI vérifie directement leur effet
    m = VERSION_RE.search(pr["title"])
    return not m or m[1] != m[2]


def dependabot(gh: GitHub, state: dict, repos: list[str]) -> None:
    for repo in repos:
        prs = sorted((p for p in gh.open_pulls(repo) if p["user"]["login"] == "dependabot[bot]"),
                     key=lambda p: p["number"])
        merged_one = False  # une par repo et par passage : les suivantes seront rebasées
        for pr in prs:
            key = f"{repo}#{pr['number']}"
            if is_major(pr):
                if once(state, f"major:{key}"):
                    log(state, "dependabot_major", title=pr["title"], pr=pr["html_url"], repo=repo)
                continue
            status, _ = ci_status(gh, repo, pr["head"]["sha"], parse(pr["created_at"]))
            if status == "failure":
                if once(state, f"red:{key}:{pr['head']['sha']}"):
                    log(state, "dependabot_red", title=pr["title"], pr=pr["html_url"], repo=repo)
                continue
            if status != "success" or merged_one:
                continue
            full = pull_with_mergeable(gh, repo, pr["number"])
            if full["mergeable"] is False:
                if once(state, f"rebase:{key}:{pr['head']['sha']}"):
                    gh.write("POST", f"/repos/{OWNER}/{repo}/issues/{pr['number']}/comments",
                             {"body": "@dependabot rebase"})
                continue
            if full["mergeable"]:
                merged = gh.merge_pr(repo, pr["number"], pr["title"])
                state["watch"].append({"target": repo, "sha": merged.get("sha"),
                                       "merged": iso(now()), "pr": pr["html_url"],
                                       "title": pr["title"], "issue_repo": None, "issue": None,
                                       "security": False})
                gh.delete_branch(repo, pr["head"]["ref"])
                log(state, "dependabot_merged", title=pr["title"], pr=pr["html_url"], repo=repo)
                print(f"  ✓ Dependabot mergée : {pr['html_url']}")
                merged_one = True


# --------------------------------------------------------------------------- surveillance


def watch(gh: GitHub, state: dict) -> None:
    """C'est le Réparateur qui annule un merge fautif. Ici, on reporte ses annulations sur
    les tâches de l'agent : issue rouverte avec les cases à cocher, et mention dans le bilan."""
    for entry in list(state["watch"]):
        expired = now() - parse(entry["merged"]) > timedelta(hours=CONFIG["watch_hours"])
        if expired or not entry.get("sha"):
            state["watch"].remove(entry)
            continue
        commits = gh.get(f"/repos/{OWNER}/{entry['target']}/commits", since=entry["merged"],
                         per_page=100) or []
        messages = [c["commit"]["message"] for c in commits]
        mark = f"{REPAIR_MARK} {entry['sha']}"
        if not any(mark in m for m in messages) or any(REPAIR_BACK in m and mark in m for m in messages):
            continue
        state["watch"].remove(entry)
        log(state, "reverted", title=entry["title"], pr=entry["pr"], repo=entry["target"],
            security=entry["security"], detail="annulé par le Réparateur : un workflow échouait "
            "depuis ce merge")
        if entry.get("issue"):
            repo, number = entry["issue_repo"], entry["issue"]
            gh.update_issue(repo, number, state="open")
            gh.comment(repo, number, "↩️ Le Réparateur a annulé ce changement : un workflow du repo "
                       "échouait depuis son merge. Commente si tu as une piste, puis coche **Lancer** "
                       "pour que je recommence.")
            relabel(gh, repo, number, ["mergé"], ["annulé"])
            issue = {"number": number, "body": gh.get(f"/repos/{OWNER}/{repo}/issues/{number}")["body"]}
            reopen_decision(gh, repo, issue)
        print(f"  ↩️ annulé par le Réparateur : {entry['pr']}")


# --------------------------------------------------------------------------- alerte Réparateur

ALERT_PATH = ".github/workflows/alerte.yml"
ALERT_BRANCH = "agent/alerte-reparateur"
NAME_RE = re.compile(r"^name:\s*(.+?)\s*$", re.M)


def alert_yaml(names: list[str]) -> str:
    listed = ", ".join(json.dumps(n, ensure_ascii=False) for n in names)
    return f"""# Fichier géré automatiquement par le Développeur (menckley/audit-repos) : ne pas modifier.
# Dès qu'un workflow se termine en échec sur la branche principale, le Réparateur est appelé :
# il relance, cherche le merge fautif, l'annule si besoin, relance et te prévient.
name: 🚨 Alerte Réparateur

on:
  workflow_run:
    workflows: [{listed}]
    types: [completed]

permissions:
  contents: write
  actions: write
  issues: write
  pull-requests: read

concurrency:
  group: reparateur
  cancel-in-progress: false

jobs:
  reparer:
    if: >-
      github.event.workflow_run.conclusion == 'failure' ||
      github.event.workflow_run.conclusion == 'timed_out' ||
      contains(github.event.workflow_run.head_commit.message, '{REPAIR_MARK}')
    uses: menckley/audit-repos/.github/workflows/reparateur.yml@main
"""


def workflow_files(gh: GitHub, repo: str) -> list[tuple[str, str, str]]:
    try:
        listing = gh.get(f"/repos/{OWNER}/{repo}/contents/.github/workflows") or []
    except GitHubError:
        return []
    files = []
    for f in listing:
        if f["name"].endswith((".yml", ".yaml")):
            content = gh.get(f"/repos/{OWNER}/{repo}/contents/{f['path']}")
            text = base64.b64decode(content.get("content", "")).decode("utf-8", "replace")
            files.append((f["path"], text, content["sha"]))
    return files


def alert_changes(files: list[tuple[str, str, str]]) -> dict[str, str]:
    """Fichiers à créer ou modifier : l'alerte (à jour de tous les workflows) et le
    déclencheur manuel des workflows qui n'en ont pas (pour pouvoir les relancer)."""
    names, changes = [], {}
    for path, text, _ in files:
        if path == ALERT_PATH:
            continue
        found = NAME_RE.search(text)
        names.append(found[1].strip("\"'") if found else path)
        if "workflow_dispatch" not in text and re.search(r"^on:[ \t]*\n", text, re.M):
            changes[path] = re.sub(r"^on:[ \t]*\n", "on:\n  workflow_dispatch:\n", text, count=1, flags=re.M)
    if not names:
        return {}
    alert = alert_yaml(sorted(set(names)))
    current = next((text for path, text, _ in files if path == ALERT_PATH), None)
    if current != alert:
        changes[ALERT_PATH] = alert
    return changes


def advance_infra_pr(gh: GitHub, state: dict, repo: str, branch: str, label: str) -> bool:
    """Fait avancer une PR technique de l'agent : merge si la CI est verte.
    Vrai si une PR était ouverte sur cette branche."""
    pr = gh.open_pr_for(repo, branch)
    if not pr:
        return False
    status, _ = ci_status(gh, repo, pr["head"]["sha"], parse(pr["created_at"]))
    full = pull_with_mergeable(gh, repo, pr["number"])
    if status == "success" and full.get("mergeable"):
        gh.merge_pr(repo, pr["number"], pr["title"])
        gh.delete_branch(repo, branch)
        log(state, "merged", title=f"[{repo}] {label}", pr=pr["html_url"], repo=repo, security=False)
        print(f"  ✓ {label} : {repo}")
    elif status in ("failure", "none") and once(state, f"infra-ko:{repo}:{pr['head']['sha']}"):
        log(state, "review", title=f"[{repo}] {label}", pr=pr["html_url"], repo=repo, security=False,
            detail="la CI de cette PR technique n'est pas verte.")
    return True


def ensure_alerts(gh: GitHub, state: dict, repos: list[str]) -> None:
    for repo in repos:
        if advance_infra_pr(gh, state, repo, ALERT_BRANCH, "Alerte du Réparateur installée"):
            continue
        changes = alert_changes(workflow_files(gh, repo))
        if not changes:
            continue
        base = gh.repo(repo)["default_branch"]
        base_sha = gh.get(f"/repos/{OWNER}/{repo}/git/ref/heads/{base}")["object"]["sha"]
        gh.delete_branch(repo, ALERT_BRANCH)
        gh.write("POST", f"/repos/{OWNER}/{repo}/git/refs",
                 {"ref": f"refs/heads/{ALERT_BRANCH}", "sha": base_sha})
        shas = {path: sha for path, _, sha in workflow_files(gh, repo)}
        for path, text in changes.items():
            data = {"message": f"ci: {'alerte du Réparateur' if path == ALERT_PATH else 'relance manuelle possible'}"
                               f" ({path.rsplit('/', 1)[-1]})",
                    "content": base64.b64encode(text.encode()).decode(), "branch": ALERT_BRANCH}
            if path in shas:
                data["sha"] = shas[path]
            gh.write("PUT", f"/repos/{OWNER}/{repo}/contents/{path}", data)
        gh.create_pr(repo, ALERT_BRANCH, base, "ci: alerte du Réparateur (relance et annulation automatiques)",
                     "Installe ou met à jour `.github/workflows/alerte.yml` : dès qu'un workflow échoue sur "
                     "la branche principale, le 🚑 Réparateur (menckley/audit-repos) le relance, annule le "
                     "merge fautif si besoin, relance et prévient par une issue.\n\n"
                     "Ajoute aussi `workflow_dispatch` aux workflows qui n'en avaient pas, pour qu'ils "
                     "puissent être relancés après une annulation.\n\n"
                     "🛠️ Préparée par le Développeur (Claude Code). Mergée automatiquement si la CI est verte.")
        print(f"  + PR d'alerte ouverte : {repo}")


HYGIENE_BRANCH = "agent/hygiene"


def ensure_hygiene(gh: GitHub, state: dict, repos: list[str]) -> None:
    """Retire les fichiers générés déjà commités (PR technique, mergée si la CI est verte)."""
    for repo in repos:
        if advance_infra_pr(gh, state, repo, HYGIENE_BRANCH, "Fichiers générés retirés du suivi git"):
            continue
        base = gh.repo(repo)["default_branch"]
        tree = gh.get(f"/repos/{OWNER}/{repo}/git/trees/{base}", recursive=1) or {}
        if not any(GENERATED_RE.search(item["path"]) for item in tree.get("tree", [])):
            continue
        repo_dir = fresh_clone(repo, tmp_dir("hygiene") / repo)
        sh("git", "checkout", "-q", "-B", HYGIENE_BRANCH, cwd=repo_dir)
        git_identity(repo_dir)
        if not clean_generated(repo_dir):
            continue
        push(repo_dir, repo, HYGIENE_BRANCH, force=True)
        gh.create_pr(repo, HYGIENE_BRANCH, base, "chore: retirer les fichiers générés et compléter .gitignore",
                     "Des fichiers générés automatiquement (caches Python, environnements virtuels…) étaient "
                     "suivis par git. Cette PR les retire du suivi, sans les supprimer du disque, et complète "
                     "`.gitignore` pour qu'ils ne reviennent pas.\n\n"
                     "🛠️ Préparée par le Développeur (Claude Code). Mergée automatiquement si la CI est verte.")
        print(f"  + PR d'hygiène ouverte : {repo}")


def sweep_branches(gh: GitHub, repos: list[str]) -> None:
    """Supprime les branches de l'agent et de Dependabot dont toutes les PR sont closes.
    Les autres branches (les tiennes) ne sont jamais touchées."""
    for repo in repos:
        base = gh.repo(repo)["default_branch"]
        for branch in gh.get(f"/repos/{OWNER}/{repo}/branches", per_page=100) or []:
            name = branch["name"]
            if name == base or not name.startswith(("agent/", "dependabot/")):
                continue
            prs = gh.get(f"/repos/{OWNER}/{repo}/pulls", head=f"{OWNER}:{name}", state="all") or []
            if prs and all(p["state"] == "closed" for p in prs):
                gh.delete_branch(repo, name)
                print(f"  🧹 branche supprimée : {repo}/{name}")


# --------------------------------------------------------------------------- bilan


ATTENTION_TYPES = ("human", "revert_failed", "reverted", "review", "clarify", "failed",
                   "dependabot_major", "dependabot_red")
PR_URL_RE = re.compile(r"github\.com/[^/]+/([^/]+)/pull/(\d+)")
ISSUE_URL_RE = re.compile(r"github\.com/[^/]+/([^/]+)/issues/(\d+)")


def still_waiting(gh: GitHub, e: dict) -> bool:
    """Vrai si ce point attend encore le propriétaire au moment du bilan (il a pu se
    régler depuis : PR mergée ou fermée, tâche relancée, issue fermée)."""
    if e["type"] in ("human", "revert_failed"):
        return True
    try:
        pr_ref = PR_URL_RE.search(e.get("pr") or "")
        if pr_ref:
            pr = gh.pull(pr_ref[1], int(pr_ref[2]))
            if pr.get("merged") or pr.get("state") == "closed":
                return False
        issue_ref = ISSUE_URL_RE.search(e.get("issue") or "")
        if issue_ref:
            issue = gh.get(f"/repos/{OWNER}/{issue_ref[1]}/issues/{issue_ref[2]}")
            labels = labels_of(issue)
            if issue["state"] == "closed" or labels & {"en-cours", "pr-ouverte", "mergé", "approuvé"} \
                    or LAUNCH_RE.search(issue.get("body") or ""):
                return False
    except GitHubError:
        pass  # dans le doute, on le signale
    return True


def dedupe(entries: list[dict]) -> list[dict]:
    """Garde le dernier événement de chaque sujet."""
    last: dict[tuple, dict] = {}
    for e in entries:
        last[(e["type"], e.get("pr") or e.get("issue") or e.get("title"))] = e
    return list(last.values())


def bilan_body(done: list[dict], waiting: list[dict]) -> str:
    def name(e: dict) -> str:
        if e.get("security"):
            return f"Correctif de sécurité ([détails privés]({e['issue']}))" if e.get("issue") \
                else "Correctif de sécurité"
        return e["title"]

    def line(e: dict, extra: str = "") -> str:
        links = " · ".join(x for x in (f"[PR]({e['pr']})" if e.get("pr") else "",
                                        f"[issue]({e['issue']})" if e.get("issue") and not e.get("security") else "") if x)
        return f"- {name(e)}{(' — ' + links) if links else ''}{extra}"

    def detail(e: dict) -> str:
        return f" — {e['detail']}" if e.get("detail") else ""

    def of(items: list[dict], kind: str) -> list[dict]:
        return [e for e in items if e["type"] == kind]

    merged, deps = of(done, "merged"), of(done, "dependabot_merged")
    security = sum(1 for e in merged if e.get("security"))
    todo = [
        ("👤 À faire par toi (hors code)", of(waiting, "human"),
         lambda e: line(e, f"\n  > {e['detail']}")),
        ("🚨 Annulation impossible : à vérifier vite", of(waiting, "revert_failed"),
         lambda e: line(e, detail(e))),
        ("↩️ Annulé automatiquement : commente puis coche **Lancer** pour réessayer",
         of(waiting, "reverted"), lambda e: line(e, detail(e))),
        ("⚠️ PR laissée ouverte : merge-la toi-même, ou coche **Lancer** pour que je recommence",
         of(waiting, "review"), lambda e: line(e, detail(e))),
        ("🤔 Besoin de précisions : commente puis coche **Lancer**", of(waiting, "clarify"), line),
        ("❌ Échec technique : coche **Lancer** pour réessayer", of(waiting, "failed"),
         lambda e: line(e, detail(e))),
        ("📦 Dependabot, version majeure : merge la PR si elle te convient, sinon ferme-la",
         of(waiting, "dependabot_major"), line),
        ("📦 Dependabot, CI rouge : à regarder", of(waiting, "dependabot_red"), line),
    ]
    finished = [
        ("Mergé automatiquement", merged, line),
        ("Analyses livrées", of(done, "analysis"), line),
        ("Mises à jour Dependabot mergées", deps, line),
    ]
    count = sum(len(items) for _, items, _ in todo)
    lines = [f"@{OWNER} ici le Développeur 🛠️ : j'ai fini tout ce que je pouvais faire seul.", "",
             f"✅ **Terminé : {len(merged) + len(deps)} changement(s) mergé(s)**"
             f" (dont {security} correctif(s) de sécurité et {len(deps)} mise(s) à jour Dependabot)",
             f"⏳ **T'attend : {count} point(s)**" if count else "⏳ **Rien ne t'attend.** 🎉"]
    if count:
        lines += ["", "# ⏳ Ce qui t'attend"]
        for title, items, fmt in todo:
            if items:
                lines += ["", f"### {title}"] + [fmt(e) for e in items]
    lines += ["", "# ✅ Ce qui est fait"]
    for title, items, fmt in finished:
        if items:
            lines += ["", f"### {title}"] + [fmt(e) for e in items]
    lines += ["", "_Les merges restent surveillés 48 h : si un workflow casse, le changement "
              "est annulé automatiquement et tu en es informé._", "",
              "_— 🛠️ Le Développeur (Claude Code)_"]
    return "\n".join(lines)


def maybe_bilan(gh: GitHub, bot: GitHub, state: dict, busy: bool) -> None:
    entries = [e for e in state["log"] if not e["reported"]]
    if busy or not entries:
        return
    if state["last_bilan"] and now() - parse(state["last_bilan"]) < timedelta(
            hours=CONFIG["bilan_min_interval_hours"]):
        return
    entries = dedupe(entries)
    done = [e for e in entries if e["type"] not in ATTENTION_TYPES]
    waiting = [e for e in entries if e["type"] in ATTENTION_TYPES and still_waiting(gh, e)]
    issue = bot.create_issue(PUBLIC_REPO, f"🛠️ Bilan automatique du {now():%d/%m/%Y}",
                             bilan_body(done, waiting), ["bilan"])
    for e in state["log"]:
        e["reported"] = True
    state["last_bilan"] = iso(now())
    print(f"\nBilan publié : {issue['html_url']}")


# --------------------------------------------------------------------------- main


def main() -> None:
    gh = GitHub(os.environ["GH_PAT"])
    bot = GitHub(os.environ.get("GITHUB_TOKEN"))  # le bilan publié par le bot → e-mail
    state = load_state()
    can_claude = True
    try:
        issues = {repo: gh.issues(repo, state="open", labels="audit")
                  for repo in (PUBLIC_REPO, SECURITY_REPO)}
        migrate_backlog(gh, issues)

        print("\n— PR en attente")
        can_claude = advance_pending(gh, state, can_claude)

        queue = []
        for repo, items in issues.items():
            for issue in items:
                if issue["user"]["login"].lower() != OWNER.lower():
                    continue  # un inconnu ne peut pas faire travailler l'agent
                labels, body = labels_of(issue), issue.get("body") or ""
                if labels & BUSY:
                    continue
                if REFUSE_RE.search(body):
                    gh.update_issue(repo, issue["number"],
                                    body=set_decision(body, f"❌ Refusée le {now():%d/%m/%Y}."))
                    relabel(gh, repo, issue["number"], ["à-valider", "auto"], ["refusé"])
                    gh.close_issue(repo, issue["number"], "not_planned")
                    print(f"  refusée : {repo}#{issue['number']}")
                elif LATER_RE.search(body):
                    gh.update_issue(repo, issue["number"], body=set_decision(
                        body, f"⏸ Mise de côté le {now():%d/%m/%Y} : reviendra au prochain audit."))
                    relabel(gh, repo, issue["number"], ["à-valider"], ["plus-tard"])
                    gh.close_issue(repo, issue["number"], "not_planned")
                    print(f"  plus tard : {repo}#{issue['number']}")
                elif LAUNCH_RE.search(body) or "approuvé" in labels:
                    queue.append((repo, issue, True))
                elif ("auto" in labels or cat_of(labels) in CONFIG.get("always_auto_categories", [])) \
                        and "non-codable" not in labels and not labels & STOPPED:
                    queue.append((repo, issue, False))
        # CI d'abord, puis sécurité, puis les décisions du propriétaire, puis le reste.
        queue.sort(key=lambda t: (t[0] != SECURITY_REPO, not t[2], t[1]["number"]))
        queue = ci_first(gh, state, queue, issues[PUBLIC_REPO])
        print(f"\n— {len(queue)} tâche(s) à démarrer")
        started = 0
        for repo, issue, manual in queue:
            if not can_claude or started >= CONFIG["max_tasks_per_run"]:
                break
            try:
                start_task(gh, state, repo, issue, manual)
                started += 1
            except QuotaExhausted:
                print("  ⏸ quota Pro épuisé : reprise au prochain passage.")
                can_claude = False
            save_state(state)

        public = [r["name"] for r in gh.public_repos()]
        if CONFIG["dependabot_automerge"]:
            print("\n— Dependabot")
            dependabot(gh, state, public)
        print("\n— Alerte du Réparateur dans chaque repo")
        ensure_alerts(gh, state, public)
        print("\n— Hygiène : fichiers générés et branches mergées")
        ensure_hygiene(gh, state, public)
        sweep_branches(gh, public)
        print("\n— Annulations faites par le Réparateur")
        watch(gh, state)

        busy = bool(state["pending"]) or started < len(queue)
        maybe_bilan(gh, bot, state, busy)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
