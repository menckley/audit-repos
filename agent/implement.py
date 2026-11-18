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

from common import (AGENT_MARK, CONFIG, LAUNCH_RE, MARKER_RE, OWNER, PUBLIC_REPO, REFUSE_RE,
                    ROOT, SECURITY_REPO, V2, GitHub, GitHubError, QuotaExhausted, commit_state,
                    compose_body, decision_block, footer, fresh_clone, is_auto, render,
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
    """Passe les issues créées avant les cases à cocher au nouveau format (une seule fois)."""
    for repo, items in issues.items():
        for issue in items:
            body = issue.get("body") or ""
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
                log(state, "dependabot_merged", title=pr["title"], pr=pr["html_url"], repo=repo)
                print(f"  ✓ Dependabot mergée : {pr['html_url']}")
                merged_one = True


# --------------------------------------------------------------------------- surveillance


def watch(gh: GitHub, state: dict) -> None:
    """Annule un merge de l'agent si un workflow de la branche principale casse ensuite."""
    for entry in list(state["watch"]):
        merged = parse(entry["merged"])
        if now() - merged > timedelta(hours=CONFIG["watch_hours"]) or not entry.get("sha"):
            state["watch"].remove(entry)
            continue
        target = entry["target"]
        base = gh.repo(target)["default_branch"]
        after = [r for r in gh.workflow_runs(target, branch=base, created=f">={entry['merged']}")
                 if r["event"] != "pull_request" and r["status"] == "completed"]
        latest: dict[int, dict] = {}
        for run in sorted(after, key=lambda r: r["created_at"]):
            latest[run["workflow_id"]] = run
        for wf_id, run in latest.items():
            if run["conclusion"] not in BAD_CONCLUSIONS:
                continue
            before = [r for r in gh.get(f"/repos/{OWNER}/{target}/actions/workflows/{wf_id}/runs",
                                        branch=base, created=f"<{entry['merged']}",
                                        per_page=5)["workflow_runs"]
                      if r["status"] == "completed"]
            if before and before[0]["conclusion"] == "success":
                revert(gh, state, entry, run)
                break


def revert(gh: GitHub, state: dict, entry: dict, run: dict) -> None:
    state["watch"].remove(entry)
    target, sha = entry["target"], entry["sha"]
    reason = f"le workflow « {run['name']} » échoue depuis ce merge ({run['html_url']})"
    print(f"  ↩️ annulation de {entry['pr']} : {reason}")
    try:
        work = tmp_dir("revert")
        repo_dir = fresh_clone(target, work / target)
        git_identity(repo_dir)
        branch = f"agent/revert-{sha[:8]}"
        sh("git", "checkout", "-q", "-B", branch, cwd=repo_dir)
        sh("git", "revert", "--no-edit", sha, cwd=repo_dir)
        push(repo_dir, target, branch, force=True)
        base = gh.repo(target)["default_branch"]
        pr = gh.open_pr_for(target, branch) or gh.create_pr(
            target, branch, base, f"revert: {entry['title']}"[:250],
            f"Annulation automatique de {entry['pr']} : {reason}.\n\n"
            "🛠️ Le Développeur (Claude Code).")
        for attempt in range(6):  # GitHub met quelques secondes à rendre la PR mergeable
            try:
                gh.merge_pr(target, pr["number"], f"revert: {entry['title']}"[:200])
                break
            except GitHubError:
                if attempt == 5:
                    raise
                time.sleep(10)
        gh.delete_branch(target, branch)
    except Exception as exc:  # noqa: BLE001
        log(state, "revert_failed", title=entry["title"], pr=entry["pr"], repo=target,
            security=entry["security"], detail=f"{reason} ; annulation impossible : "
            f"{truncate(scrub(str(exc)), 200)}")
        return
    log(state, "reverted", title=entry["title"], pr=entry["pr"], repo=target,
        security=entry["security"], detail=reason)
    if entry.get("issue"):
        repo, number = entry["issue_repo"], entry["issue"]
        gh.update_issue(repo, number, state="open")
        gh.comment(repo, number, f"↩️ Changement annulé automatiquement : {reason}.\n\n"
                   "Commente si tu as une piste, puis coche **Lancer** pour que je recommence.")
        relabel(gh, repo, number, ["mergé"], ["annulé"])
        issue = {"number": number, "body": gh.get(f"/repos/{OWNER}/{repo}/issues/{number}")["body"]}
        reopen_decision(gh, repo, issue)


# --------------------------------------------------------------------------- bilan


def bilan_body(entries: list[dict]) -> str:
    def name(e: dict) -> str:
        if e.get("security"):
            return f"Correctif de sécurité ([détails privés]({e['issue']}))" if e.get("issue") \
                else "Correctif de sécurité"
        return e["title"]

    def line(e: dict, extra: str = "") -> str:
        links = " · ".join(x for x in (f"[PR]({e['pr']})" if e.get("pr") else "",
                                        f"[issue]({e['issue']})" if e.get("issue") and not e.get("security") else "") if x)
        return f"- {name(e)}{(' — ' + links) if links else ''}{extra}"

    by = lambda t: [e for e in entries if e["type"] == t]  # noqa: E731
    sections = [
        ("👤 À faire par toi (hors code)", by("human"), lambda e: line(e, f"\n  > {e['detail']}")),
        ("↩️ Annulé automatiquement", by("reverted"), lambda e: line(e, f" — {e['detail']}")),
        ("🚨 Annulation impossible : à vérifier vite", by("revert_failed"),
         lambda e: line(e, f" — {e['detail']}")),
        ("✅ Mergé automatiquement", by("merged"), line),
        ("📝 Analyses livrées", by("analysis"), line),
        ("⚠️ À relire (PR laissée ouverte)", by("review"), lambda e: line(e, f" — {e['detail']}")),
        ("🤔 Besoin de précisions", by("clarify"), line),
        ("❌ Échecs techniques", by("failed"), lambda e: line(e, f" — {e['detail']}")),
        ("📦 Dependabot : mises à jour mergées", by("dependabot_merged"), line),
        ("📦 Dependabot : versions majeures laissées à ta décision", by("dependabot_major"), line),
        ("📦 Dependabot : CI rouge, non mergé", by("dependabot_red"), line),
    ]
    merged = len(by("merged")) + len(by("dependabot_merged"))
    attention = len(by("human")) + len(by("reverted")) + len(by("revert_failed")) + len(by("review"))
    lines = [f"@{OWNER} ici le Développeur 🛠️ : voici le bilan de mon travail.", "",
             f"**{merged} changement(s) mergé(s)** · **{attention} point(s) demandant ton attention**."]
    for title, items, fmt in sections:
        if items:
            lines += ["", f"## {title}"] + [fmt(e) for e in items]
    lines += ["", "_— 🛠️ Le Développeur (Claude Code)_"]
    return "\n".join(lines)


def maybe_bilan(bot: GitHub, state: dict, busy: bool) -> None:
    entries = [e for e in state["log"] if not e["reported"]]
    if busy or not entries:
        return
    if state["last_bilan"] and now() - parse(state["last_bilan"]) < timedelta(
            hours=CONFIG["bilan_min_interval_hours"]):
        return
    issue = bot.create_issue(PUBLIC_REPO, f"🛠️ Bilan automatique du {now():%d/%m/%Y}",
                             bilan_body(entries), ["bilan"])
    for e in entries:
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
        print("\n— Surveillance après merge")
        watch(gh, state)

        busy = bool(state["pending"]) or started < len(queue)
        maybe_bilan(bot, state, busy)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
