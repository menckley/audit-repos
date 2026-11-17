"""Traite les tâches validées (label `approuvé`) : PR de code, PR de spec ou analyse.

L'agent ne merge jamais. Les tokens GitHub restent dans ce script et ne sont jamais
transmis à Claude : c'est ce script qui pousse la branche et ouvre la PR.
"""

from __future__ import annotations

import os
from pathlib import Path

from common import (AGENT_MARK, CONFIG, MARKER_RE, OWNER, PUBLIC_REPO, SECURITY_REPO,
                    GitHub, QuotaExhausted, fresh_clone, render, run_claude, scrub, sh,
                    tmp_dir, truncate)

READ_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch",
              "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)"]
CODE_TOOLS = READ_TOOLS + [
    "Edit", "Write",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git mv:*)", "Bash(git rm:*)", "Bash(mkdir:*)",
    "Bash(pip install:*)", "Bash(python -m pip install:*)", "Bash(python -m pytest:*)",
    "Bash(pytest:*)", "Bash(python:*)", "Bash(ruff:*)",
]


class NoChange(Exception):
    """Claude n'a rien commité : il explique pourquoi (tâche ambiguë, infaisable…)."""


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


def run_branch_task(gh: GitHub, target: str, branch: str, prompt_name: str,
                    values: dict[str, str], issue_ref: str | None) -> dict:
    """Clone, laisse Claude travailler sur une branche, pousse et ouvre la PR."""
    meta = gh.repo(target)
    base = meta["default_branch"]
    work = tmp_dir("implement")
    repo_dir = fresh_clone(target, work / target)
    sh("git", "checkout", "-q", "-B", branch, cwd=repo_dir)
    sh("git", "config", "user.name", f"{OWNER} (agent)", cwd=repo_dir)
    sh("git", "config", "user.email", f"{OWNER}@users.noreply.github.com", cwd=repo_dir)

    report = work / f"{branch.replace('/', '-')}-pr.md"
    report.unlink(missing_ok=True)
    prompt = render(prompt_name, {**values, "PR_PATH": str(report), "BASE": base})
    run_claude(prompt, repo_dir, CODE_TOOLS, CONFIG["implement_max_turns"])

    title, body = read_report(report)
    commits = int(sh("git", "rev-list", "--count", f"origin/{base}..HEAD", cwd=repo_dir).strip())
    if commits == 0:
        raise NoChange(body or title or "Claude n'a produit aucun commit, sans explication.")
    if subprocess_dirty(repo_dir):
        print("  ⚠️ fichiers non commités ignorés")

    token = os.environ["GH_PAT"]
    sh("git", "push", "-q", "--force",
       f"https://x-access-token:{token}@github.com/{OWNER}/{target}.git",
       f"HEAD:refs/heads/{branch}", cwd=repo_dir)

    footer = ["", "---"]
    if issue_ref:
        footer.append(f"Closes {issue_ref}")
    footer.append("🤖 Préparée par l'agent d'audit (Claude Code). Relecture et merge : toi uniquement.")
    full_body = (body or "_(pas de description)_") + "\n" + "\n".join(footer)

    existing = gh.open_pr_for(target, branch)
    if existing:
        return existing
    return gh.create_pr(target, branch, base, title or f"Tâche d'audit {branch}", full_body)


def subprocess_dirty(repo_dir: Path) -> bool:
    return bool(sh("git", "status", "--porcelain", cwd=repo_dir).strip())


def handle(gh: GitHub, repo: str, issue: dict) -> None:
    number = issue["number"]
    body = issue.get("body") or ""
    marker = MARKER_RE.search(body)
    if not marker:
        raise NoChange("Issue sans marqueur d'audit : je ne sais pas à quel repo elle se rapporte.")
    target, finding_id = marker["repo"], marker["id"]
    labels = {label["name"] for label in issue["labels"]}
    kind = next((name.split(":", 1)[1] for name in labels if name.startswith("type:")), "")
    security = repo == SECURITY_REPO
    values = {
        "OWNER": OWNER, "REPO": target, "ID": finding_id,
        "TITLE": issue["title"], "BODY": body.split("\n---\n")[0],
        "NOTES": owner_comments(gh, repo, number),
        "SECURITY": "oui" if security else "non",
    }
    # Pour la sécurité, l'issue est privée : la PR publique n'y fait pas référence.
    issue_ref = None if security else f"https://github.com/{OWNER}/{repo}/issues/{number}"

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
        gh.remove_label(repo, number, "en-cours")
        gh.add_labels(repo, number, ["analyse-livrée"])
        return

    spec_path = f"docs/specs/{finding_id}.md"
    base = gh.repo(target)["default_branch"]
    if kind == "fonctionnalite" and not gh.file_exists(target, spec_path, base):
        pr = run_branch_task(gh, target, f"agent/spec-{finding_id}", "spec.md",
                             {**values, "SPEC_PATH": spec_path}, None)
        gh.comment(repo, number,
                   f"📐 Spécification proposée : {pr['html_url']}\n\n"
                   "Relis-la et merge-la si elle te convient, puis remets le label `approuvé` "
                   "sur cette issue pour lancer l'implémentation.")
        gh.remove_label(repo, number, "en-cours")
        gh.add_labels(repo, number, ["spec-en-revue"])
        return

    spec_note = (f"Une spécification validée existe : `{spec_path}`. Respecte-la."
                 if kind == "fonctionnalite" else "(pas de spécification)")
    pr = run_branch_task(gh, target, f"agent/{finding_id}", "implement.md",
                         {**values, "SPEC": spec_note}, issue_ref)
    gh.comment(repo, number, f"🔧 Pull Request ouverte : {pr['html_url']}\n\n"
               "Relis-la, vérifie que la CI passe, puis merge-la si elle te convient.")
    gh.remove_label(repo, number, "en-cours")
    gh.remove_label(repo, number, "spec-en-revue")
    gh.add_labels(repo, number, ["pr-ouverte"])


def main() -> None:
    gh = GitHub(os.environ["GH_PAT"])
    tasks = []
    for repo in (PUBLIC_REPO, SECURITY_REPO):
        for issue in gh.issues(repo, state="open", labels="approuvé"):
            # Seules les issues créées par l'agent (donc avec ton compte) sont traitées :
            # un inconnu ne peut pas faire exécuter une tâche à l'agent.
            if issue["user"]["login"].lower() != OWNER.lower():
                print(f"Ignorée (auteur {issue['user']['login']}) : {repo}#{issue['number']}")
                continue
            tasks.append((repo, issue))
    tasks.sort(key=lambda t: (t[0] != SECURITY_REPO, t[1]["number"]))  # sécurité d'abord
    print(f"{len(tasks)} tâche(s) validée(s) en attente.")

    for repo, issue in tasks[: CONFIG["max_tasks_per_run"]]:
        number = issue["number"]
        print(f"\n▶ {repo}#{number} {issue['title']}")
        gh.remove_label(repo, number, "approuvé")
        gh.remove_label(repo, number, "à-valider")
        gh.remove_label(repo, number, "à-préciser")
        gh.remove_label(repo, number, "échec")
        gh.add_labels(repo, number, ["en-cours"])
        try:
            handle(gh, repo, issue)
            print("  ✓ terminé")
        except QuotaExhausted:
            print("  ⏸ quota Pro épuisé : la tâche repart en file d'attente.")
            gh.remove_label(repo, number, "en-cours")
            gh.add_labels(repo, number, ["approuvé"])
            break
        except NoChange as exc:
            gh.comment(repo, number, "🤔 Je n'ai pas modifié le code :\n\n" + truncate(str(exc), 5000)
                       + "\n\nRéponds en commentaire avec des précisions, puis remets `approuvé`.")
            gh.remove_label(repo, number, "en-cours")
            gh.add_labels(repo, number, ["à-préciser"])
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {exc}")
            gh.comment(repo, number, "❌ Échec technique de l'agent :\n\n```\n"
                       + truncate(scrub(str(exc)), 1500) + "\n```\n\nRemets `approuvé` pour réessayer.")
            gh.remove_label(repo, number, "en-cours")
            gh.add_labels(repo, number, ["échec"])


if __name__ == "__main__":
    main()
