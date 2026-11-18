"""Audit mensuel des repos publics.

Lancé plusieurs fois du 1er au 4 du mois : chaque passage reprend là où le précédent
s'est arrêté (quota Pro épuisé), puis publie le récap quand tout est audité.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from common import (CONFIG, MARKER_RE, OWNER, PUBLIC_REPO, ROOT, SECURITY_REPO, GitHub,
                    QuotaExhausted, commit_state, compose_body, footer, fresh_clone, is_auto,
                    render, run_claude, sh, tmp_dir, truncate)

CATEGORIES = {"securite", "maintenance", "amelioration", "operationnel"}
TYPES = {"correctif", "refactoring", "tests", "documentation", "outillage", "dependances",
         "performance", "fonctionnalite", "veille", "methode", "marche"}
PRIORITIES = ["critique", "haute", "moyenne", "basse"]
EFFORTS = {"XS", "S", "M", "L", "XL"}
CAT_LABEL = {"securite": "Sécurité", "maintenance": "Maintenance",
             "amelioration": "Amélioration", "operationnel": "Opérationnel"}
MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]

AUDIT_TOOLS = [
    "Read", "Glob", "Grep", "Write", "WebSearch", "WebFetch",
    "Bash(git log:*)", "Bash(git show:*)", "Bash(ls:*)", "Bash(wc:*)",
]


# --------------------------------------------------------------------------- état


def state_path(month: str) -> Path:
    return ROOT / "state" / f"{month}.json"


def load_state(month: str) -> dict:
    path = state_path(month)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"month": month, "repos": {}, "recap_issue": None}


def save_state(state: dict, persist: bool) -> None:
    if not persist:  # audit de test : aucun état enregistré
        return
    path = state_path(state["month"])
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    commit_state([path], f"chore(audit): état {state['month']}")


# --------------------------------------------------------------------------- analyse statique


def run_tool(args: list[str], cwd: Path, limit: int, timeout: int = 600) -> str:
    try:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return f"(outil {args[0]} indisponible)"
    except subprocess.TimeoutExpired:
        return f"(outil {args[0]} : délai dépassé)"
    return truncate(proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else ""), limit) or "(rien)"


def static_analysis(repo_dir: Path) -> str:
    excluded = ".venv,venv,env,.tox,build,dist,node_modules"
    sections = [
        ("bandit (sécurité Python, sévérité moyenne et plus)",
         run_tool(["bandit", "-r", ".", "-q", "-ll", "-f", "txt", "-x", excluded], repo_dir, 6000)),
        ("ruff (qualité et modernisation)",
         run_tool(["ruff", "check", ".", "--statistics", "--exit-zero", "--no-cache",
                   "--select", "E,F,W,B,UP,S,SIM,PL,PERF"], repo_dir, 3000)),
    ]
    reqs = sorted(p for p in repo_dir.rglob("requirements*.txt") if ".venv" not in p.parts)[:3]
    for req in reqs:
        sections.append((f"pip-audit ({req.relative_to(repo_dir)})",
                         run_tool(["pip-audit", "-r", str(req), "--progress-spinner", "off",
                                   "--desc", "off"], repo_dir, 3000, timeout=900)))
    if not reqs:
        sections.append(("pip-audit", "(aucun requirements*.txt : vérifie pyproject.toml / setup.py)"))
    sections.append(("gitleaks (secrets dans l'historique, valeurs masquées)", gitleaks(repo_dir)))
    return "\n\n".join(f"### {title}\n```\n{body}\n```" for title, body in sections)


def gitleaks(repo_dir: Path) -> str:
    report = repo_dir.parent / f"{repo_dir.name}-gitleaks.json"
    result = run_tool(["gitleaks", "detect", "--source", ".", "--no-banner", "--redact",
                       "--exit-code", "0", "-f", "json", "-r", str(report)], repo_dir, 500)
    if not report.exists():
        return result
    leaks = json.loads(report.read_text() or "[]")
    if not leaks:
        return "aucun secret détecté"
    lines = [f"- {leak.get('RuleID')} : {leak.get('File')}:{leak.get('StartLine')} "
             f"(commit {str(leak.get('Commit'))[:8]})" for leak in leaks[:30]]
    return f"{len(leaks)} détection(s) :\n" + "\n".join(lines)


def repo_facts(repo_dir: Path, meta: dict) -> str:
    files = [p for p in repo_dir.rglob("*") if p.is_file() and ".git" not in p.parts]
    py = [p for p in files if p.suffix == ".py"]
    tests = [p for p in py if "test" in p.name.lower() or "tests" in p.parts]
    workflows = sorted(p.name for p in (repo_dir / ".github" / "workflows").glob("*.y*ml"))
    markers = [n for n in ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg",
                           "Pipfile", "poetry.lock", "uv.lock", ".python-version",
                           "Dockerfile", "README.md", "LICENSE", ".pre-commit-config.yaml")
               if (repo_dir / n).exists()]
    log = sh("git", "log", "-15", "--date=short", "--format=%ad %s", cwd=repo_dir, check=False)
    return "\n".join([
        f"- Description GitHub : {meta.get('description') or '(aucune)'}",
        f"- Sujets : {', '.join(meta.get('topics') or []) or '(aucun)'}",
        f"- Fichiers : {len(files)} dont {len(py)} Python, {len(tests)} fichiers de test",
        f"- Fichiers de projet présents : {', '.join(markers) or '(aucun)'}",
        f"- Workflows GitHub Actions : {', '.join(workflows) or '(aucun)'}",
        "- 15 derniers commits :",
        "```", truncate(log, 2500), "```",
    ])


# --------------------------------------------------------------------------- constats


def slug(text: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text[:60] or "constat"


def validate(raw, repo: str, known_ids: set[str]) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("constats") or raw.get("findings") or []
    findings, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not item.get("titre") or not item.get("proposition"):
            continue
        cat = str(item.get("categorie", "")).lower().replace("é", "e")
        effort = str(item.get("effort", "M")).upper()
        f = {
            "id": slug(str(item.get("id") or item["titre"])),
            "categorie": cat if cat in CATEGORIES else "amelioration",
            "type": item.get("type") if item.get("type") in TYPES else "refactoring",
            "priorite": item.get("priorite") if item.get("priorite") in PRIORITIES else "moyenne",
            "effort": effort if effort in EFFORTS else "M",
            "titre": str(item["titre"]).strip(),
            "constat": str(item.get("constat", "")).strip(),
            "proposition": str(item["proposition"]).strip(),
            "risques": str(item.get("risques", "")).strip(),
            "fichiers": [str(x) for x in item.get("fichiers") or []][:15],
            "sources": [str(x) for x in item.get("sources") or []][:10],
            "codable": bool(item.get("codable", True)) and item.get("type") != "marche",
            "majeur": bool(item.get("majeur", False)),
            "action_humaine": str(item.get("action_humaine") or "").strip(),
        }
        f["auto"] = f["codable"] and is_auto(f["categorie"], f["effort"], f["majeur"])
        if f["id"] in known_ids or f["id"] in seen:
            continue
        seen.add(f["id"])
        findings.append(f)
    findings.sort(key=lambda f: PRIORITIES.index(f["priorite"]))
    return findings[: CONFIG["max_findings_per_repo"]]


def issue_body(repo: str, f: dict) -> str:
    parts = [
        f"**Repo :** [{OWNER}/{repo}](https://github.com/{OWNER}/{repo}) · "
        f"**Catégorie :** {CAT_LABEL[f['categorie']]} · **Type :** {f['type']} · "
        f"**Priorité :** {f['priorite']} · **Effort :** {f['effort']}",
        "### Constat", f["constat"] or "_(non précisé)_",
        "### Proposition", f["proposition"],
    ]
    if f["risques"]:
        parts += ["### Risques et impacts", f["risques"]]
    if f["fichiers"]:
        parts += ["### Fichiers concernés", "\n".join(f"- `{x}`" for x in f["fichiers"])]
    if f["sources"]:
        parts += ["### Sources", "\n".join(f"- {x}" for x in f["sources"])]
    if f["action_humaine"]:
        parts += ["### 👤 Action de ta part", f["action_humaine"]]
    if f["majeur"] and f["categorie"] in CONFIG["auto_categories"]:
        parts += ["_Changement majeur : soumis à ta décision malgré sa catégorie._"]
    return compose_body("\n\n".join(parts), repo, f["id"],
                        footer(f["auto"], f["type"], f["codable"]))


def existing_findings(gh: GitHub) -> dict[str, dict[str, list[str]]]:
    """Titres des constats déjà connus, par repo : ouverts et clos (traités ou refusés)."""
    known: dict[str, dict[str, list[str]]] = {}
    for target in (PUBLIC_REPO, SECURITY_REPO):
        for issue in gh.issues(target, state="all", labels="audit"):
            m = MARKER_RE.search(issue.get("body") or "")
            if not m:
                continue
            entry = known.setdefault(m["repo"], {"ids": [], "open": [], "closed": []})
            entry["ids"].append(m["id"])
            entry["open" if issue["state"] == "open" else "closed"].append(
                f"{m['id']} — {issue['title']}")
    return known


# --------------------------------------------------------------------------- audit d'un repo


def audit_repo(gh: GitHub, meta: dict, known: dict, month: str) -> list[dict]:
    repo = meta["name"]
    work = tmp_dir("audit")
    repo_dir = fresh_clone(repo, work / repo)
    findings_path = work / f"{repo}-findings.json"
    findings_path.unlink(missing_ok=True)
    entry = known.get(repo, {"ids": [], "open": [], "closed": []})
    prompt = render("audit.md", {
        "OWNER": OWNER,
        "REPO": repo,
        "MONTH": month,
        "TODAY": datetime.now(timezone.utc).date().isoformat(),
        "MAX": str(CONFIG["max_findings_per_repo"]),
        "FINDINGS_PATH": str(findings_path),
        "FACTS": repo_facts(repo_dir, meta),
        "STATIC": static_analysis(repo_dir),
        "OPEN": "\n".join(f"- {t}" for t in entry["open"]) or "(aucun)",
        "CLOSED": "\n".join(f"- {t}" for t in entry["closed"][-60:]) or "(aucun)",
    })
    run_claude(prompt, repo_dir, AUDIT_TOOLS, CONFIG["audit_max_turns"])
    if not findings_path.exists():
        raise RuntimeError("Claude n'a pas produit le fichier de constats")
    raw = json.loads(findings_path.read_text(encoding="utf-8"))
    return validate(raw, repo, set(entry["ids"]))


def publish(gh: GitHub, repo: str, findings: list[dict], month: str) -> list[dict]:
    created = []
    for f in findings:
        target = SECURITY_REPO if f["categorie"] == "securite" else PUBLIC_REPO
        labels = ["audit", "auto" if f["auto"] else "à-valider", f"audit-{month}",
                  f"repo:{repo}", f"cat:{f['categorie']}", f"type:{f['type']}",
                  f"priorité:{f['priorite']}"]
        if not f["codable"]:
            labels.append("non-codable")
        issue = gh.create_issue(target, f"[{repo}] {f['titre']}", issue_body(repo, f), labels)
        created.append({"repo": target, "number": issue["number"], "url": issue["html_url"],
                        "title": f["titre"], "categorie": f["categorie"], "auto": f["auto"],
                        "priorite": f["priorite"], "effort": f["effort"]})
        print(f"  + {target}#{issue['number']} [{f['categorie']}] {f['titre']}")
    return created


# --------------------------------------------------------------------------- récap


def recap_body(state: dict, test: bool) -> str:
    year, month_num = state["month"].split("-")
    month_name = f"{MONTHS_FR[int(month_num) - 1]} {year}"
    repos = state["repos"]
    items = [i for r in repos.values() for i in r.get("created", [])]
    public = [i for i in items if i["categorie"] != "securite"]
    security = [i for i in items if i["categorie"] == "securite"]
    done = [n for n, r in repos.items() if r["status"] == "done"]
    missing = [n for n, r in repos.items() if r["status"] != "done"]

    lines = [f"@{OWNER} voici le récap de l'audit de **{month_name}**{' (test)' if test else ''}.",
             "",
             f"**{len(done)} repo(s) audité(s)** · **{len(items)} nouvelle(s) proposition(s)** "
             f"dont {len(security)} de sécurité.", "",
             "| Repo | Sécurité | Maintenance | Amélioration | Opérationnel |",
             "|---|:-:|:-:|:-:|:-:|"]
    for name in sorted(repos):
        created = repos[name].get("created", [])
        counts = [sum(1 for i in created if i["categorie"] == c)
                  for c in ("securite", "maintenance", "amelioration", "operationnel")]
        status = "" if repos[name]["status"] == "done" else " ⚠️ non audité"
        lines.append(f"| `{name}`{status} | " + " | ".join(str(c) for c in counts) + " |")

    lines += ["", "## 🔒 Sécurité"]
    if security:
        by_prio = {p: sum(1 for i in security if i["priorite"] == p) for p in PRIORITIES}
        detail = ", ".join(f"{n} {p}" for p, n in by_prio.items() if n)
        auto_sec = sum(1 for i in security if i.get("auto"))
        lines.append(f"{len(security)} constat(s) ({detail}), dont {auto_sec} pris en charge "
                     "automatiquement. Détails visibles uniquement par toi : "
                     f"[ouvrir la liste](https://github.com/{OWNER}/{SECURITY_REPO}/issues?q="
                     f"is%3Aopen+label%3Aaudit-{state['month']}).")
    else:
        lines.append("Aucun nouveau constat de sécurité.")

    auto = [i for i in public if i.get("auto")]
    manual = [i for i in public if not i.get("auto")]
    lines += ["", f"## 🤖 Traité automatiquement ({len(auto)})",
              "L'agent s'en occupe seul : PR, merge si la CI est verte, annulation si un "
              "workflow casse. Tu recevras un **bilan détaillé** une fois tout terminé."]
    lines += [f"- #{i['number']} {i['title']}" for i in auto] or ["- (rien ce mois-ci)"]

    lines += ["", f"## ☑️ À décider ({len(manual)})",
              "Ouvre l'issue et coche **Lancer** ou **Refuser**. Un commentaire avant de cocher "
              "permet d'ajouter des précisions."]
    if not manual:
        lines.append("Aucune nouvelle proposition ce mois-ci.")
    for cat in ("maintenance", "amelioration", "operationnel"):
        group = [i for i in manual if i["categorie"] == cat]
        if group:
            lines += ["", f"### {CAT_LABEL[cat]}"]
            lines += [f"- #{i['number']} {i['title']} — priorité {i['priorite']}, effort {i['effort']}"
                      for i in group]

    if missing:
        lines += ["", "## ⚠️ Repos non audités ce mois-ci"]
        lines += [f"- `{n}` : {repos[n].get('error') or 'quota épuisé avant la fin'}" for n in missing]

    lines += ["", "_Récap généré automatiquement par l'agent d'audit (Claude Code)._"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- main


def main() -> None:
    gh = GitHub(os.environ.get("GH_PAT"))
    bot = GitHub(os.environ.get("GITHUB_TOKEN"))  # issue récap publiée par le bot → e-mail
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    only = [r.strip() for r in os.environ.get("INPUT_REPOS", "").split(",") if r.strip()]
    force = os.environ.get("INPUT_FORCE") == "true"
    test = bool(only)

    scheduled = os.environ.get("GITHUB_EVENT_NAME") == "schedule"
    if scheduled and not state_path(month).exists() and now.day > CONFIG["recap_deadline_day"]:
        # Passage quotidien de reprise : rien n'a été lancé ce mois-ci, rien à reprendre.
        print(f"Aucun audit {month} en cours.")
        return

    state = {"month": month, "repos": {}, "recap_issue": None} if test else load_state(month)
    if state.get("recap_issue") is not None and not force:
        print(f"Audit {month} déjà terminé (récap #{state['recap_issue']}).")
        return
    if force and not test:
        state = {"month": month, "repos": {}, "recap_issue": None}
    state.setdefault("started", now.isoformat(timespec="seconds"))

    repos = gh.public_repos()
    if only:
        unknown = set(only) - {r["name"] for r in repos}
        if unknown:
            raise SystemExit(f"Repos introuvables ou non publics : {', '.join(sorted(unknown))}")
        repos = [r for r in repos if r["name"] in only]
    for meta in repos:
        state["repos"].setdefault(meta["name"], {"status": "pending", "attempts": 0})
    print(f"Audit {month} : {len(repos)} repo(s) — {', '.join(r['name'] for r in repos)}")

    # Les labels de validation doivent exister pour que je puisse les poser moi-même.
    for target in (PUBLIC_REPO, SECURITY_REPO):
        for label in ("approuvé", "refusé"):
            gh.ensure_label(target, label)

    known = existing_findings(gh)
    quota_hit = False
    for meta in repos:
        name = meta["name"]
        entry = state["repos"][name]
        if entry["status"] in ("done", "error"):
            continue
        print(f"\n▶ {name}")
        try:
            findings = audit_repo(gh, meta, known, month)
            entry.update(status="done", created=publish(gh, name, findings, month),
                         at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            entry.pop("error", None)
        except QuotaExhausted:
            print("  ⏸ quota Pro épuisé : reprise au prochain passage.")
            quota_hit = True
        except Exception as exc:  # noqa: BLE001 — un repo en échec ne bloque pas les autres
            entry["attempts"] = entry.get("attempts", 0) + 1
            entry["error"] = truncate(str(exc), 300)
            entry["status"] = "error" if entry["attempts"] >= 3 else "pending"
            print(f"  ✗ {entry['error']}")
        save_state(state, persist=not test)
        if quota_hit:
            break

    finished = all(r["status"] in ("done", "error") for r in state["repos"].values())
    # Si le quota bloque encore après ~3 jours et demi, on envoie un récap partiel.
    started = datetime.fromisoformat(state["started"])
    deadline = (now - started).total_seconds() >= (CONFIG["recap_deadline_day"] - 0.5) * 86400
    if test or finished or deadline:
        title = f"📋 Récap audit {month}" + (" (test)" if test else "")
        issue = bot.create_issue(PUBLIC_REPO, title, recap_body(state, test), ["récap", "audit-récap"])
        state["recap_issue"] = issue["number"]
        save_state(state, persist=not test)
        print(f"\nRécap publié : {issue['html_url']}")
    else:
        pending = [n for n, r in state["repos"].items() if r["status"] == "pending"]
        print(f"\nEn attente : {', '.join(pending)}")


if __name__ == "__main__":
    main()
