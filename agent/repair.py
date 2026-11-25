"""🚑 Le Réparateur : appelé par le fichier d'alerte d'un repo dès qu'un de ses workflows
se termine sur la branche principale.

1. Échec au premier essai          → relance du workflow tel quel (souvent une panne externe).
2. Échec au second essai           → recherche des coupables : tous les merges de PR
                                     arrivés entre le dernier run réussi et le premier run
                                     en échec (plusieurs si des merges rapprochés ont fait
                                     annuler les runs intermédiaires). Ils sont annulés
                                     ensemble, puis le workflow est relancé.
3. Succès après une annulation     → « réparé ».
4. Échec après une annulation      → le code n'était pas en cause : le changement est
                                     rétabli et le propriétaire alerté.

5. Autre workflow en échec sur du
   code ancien après une réparation → relancé sur le code réparé, sans nouvelle annulation.
6. Panne non résolue, ou réparation qui a annulé des changements utiles (Dependabot, tes
   PR) → passe la main au Développeur (label `à-développer`), qui corrige pour de bon.

Un même commit n'est jamais annulé deux fois. Un commit poussé directement (sans PR) n'est
jamais annulé. Chaque incident fait l'objet d'une issue qui mentionne le propriétaire
(donc un e-mail), puis de commentaires à chaque étape.

Tourne avec le GITHUB_TOKEN du repo concerné (aucun secret supplémentaire).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from common import (BROKEN, OWNER, REPAIR_BACK, REPAIR_MARK, GitHub, GitHubError,
                    failure_window, sh)

MARK = REPAIR_MARK
BACK = REPAIR_BACK
LABEL = "réparation"
SHA_RE = re.compile(re.escape(MARK) + r" ([0-9a-f]{40})")
REVERTED_RE = re.compile(r"^annulés: ([0-9a-f ]+)$", re.M)
WORKFLOW_RE = re.compile(r"^workflow: (\d+)$", re.M)
ESCALATE = "à-développer"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
INFRA_PREFIXES = ("agent/alerte", "agent/hygiene")  # installation de l'alerte, hygiène : jamais annulées


def error_excerpt(raw: str, lines: int = 60) -> str:
    """Partie utile d'un journal de job : la sortie de l'étape en échec jusqu'à son erreur
    (« ##[error] »), sans le nettoyage final de GitHub qui suit."""
    clean = [ANSI_RE.sub("", TIMESTAMP_RE.sub("", line)) for line in raw.splitlines()]
    error = next((i for i, line in enumerate(clean) if line.startswith("##[error]")), None)
    if error is None:  # pas de marqueur : on cherche une trace Python ou un message d'erreur
        error = next((i for i in range(len(clean) - 1, -1, -1)
                      if re.search(r"Traceback|Error|Exception|error:", clean[i])), len(clean) - 1)
    start = max((i for i in range(error) if clean[i].startswith("##[group]Run ")), default=0)
    start = max(start, error - lines)
    section = [line for line in clean[start:error + 1] if not line.startswith(("##[group]", "##[endgroup]"))]
    return "\n".join(section)[-4000:]


class Repairer:
    def __init__(self, gh: GitHub, repo: str, repo_dir: Path):
        self.gh, self.repo, self.dir = gh, repo, repo_dir
        self.created: dict[str, dict] = {}
        self.api = f"/repos/{OWNER}/{repo}"

    # ----------------------------------------------------------------- notifications
    def incident(self, key: str) -> dict | None:
        # Une issue tout juste créée n'apparaît pas tout de suite dans la liste de GitHub :
        # on garde celles de ce passage en mémoire.
        if key in self.created:
            return self.created[key]
        for issue in self.gh.issues(self.repo, state="open", labels=LABEL):
            if f"<!-- incident: {key} -->" in (issue.get("body") or ""):
                return issue
        return None

    def follow_up(self, key: str, text: str, close: bool = False) -> None:
        """Commentaire sur l'incident ouvert (s'il n'y en a plus, rien n'est envoyé)."""
        issue = self.incident(key)
        if issue:
            self.gh.write("POST", f"{self.api}/issues/{issue['number']}/comments", {"body": text})
            if close:
                self.gh.close_issue(self.repo, issue["number"])

    def notify(self, key: str, title: str, text: str, close: bool = False) -> None:
        issue = self.incident(key)
        if issue:
            self.gh.write("POST", f"{self.api}/issues/{issue['number']}/comments", {"body": text})
            number = issue["number"]
        else:
            body = f"@{OWNER} {text}\n\n_— 🚑 Le Réparateur (audit-repos)_\n<!-- incident: {key} -->"
            issue = self.gh.create_issue(self.repo, title, body, [LABEL])
            issue.setdefault("labels", [{"name": LABEL}])
            self.created[key] = issue
            number = issue["number"]
        if close:
            self.gh.close_issue(self.repo, number)

    # ----------------------------------------------------------------- actions
    def rerun(self, run: dict) -> None:
        self.gh.write("POST", f"{self.api}/actions/runs/{run['id']}/rerun-failed-jobs")

    def dispatch(self, workflow_id: int, base: str) -> bool:
        try:
            self.gh.write("POST", f"{self.api}/actions/workflows/{workflow_id}/dispatches", {"ref": base})
            return True
        except GitHubError:
            return False  # le workflow n'a pas de déclencheur manuel

    def already_tried(self, sha: str) -> bool:
        commits = self.gh.get(f"{self.api}/commits", per_page=100) or []
        return any(f"{MARK} {sha}" in c["commit"]["message"] for c in commits)

    def repair_commit(self, key: str) -> str | None:
        commits = self.gh.get(f"{self.api}/commits", per_page=100) or []
        if any(BACK in c["commit"]["message"] and key in c["commit"]["message"] for c in commits):
            return None  # déjà rétabli
        return next((c["sha"] for c in commits if f"{MARK} {key}" in c["commit"]["message"]), None)

    def merged_pr(self, sha: str) -> dict | None:
        try:
            pulls = self.gh.get(f"{self.api}/commits/{sha}/pulls") or []
        except GitHubError:
            return None
        return next((p for p in pulls if p.get("merged_at") and p.get("merge_commit_sha") == sha), None)

    def escalate(self, key: str, why: str) -> None:
        """Passe la main au Développeur sur l'incident (il corrigera avec une PR)."""
        issue = self.incident(key)
        if not issue or "à-développer" in {l["name"] for l in issue.get("labels", [])}:
            return
        self.gh.add_labels(self.repo, issue["number"], [ESCALATE])
        issue.setdefault("labels", []).append({"name": ESCALATE})
        self.gh.write("POST", f"{self.api}/issues/{issue['number']}/comments",
                      {"body": f"🛠️ Je passe la main au **Développeur** : {why} Il va analyser la panne et "
                               "proposer une vraie correction (PR mergée seulement si la CI est verte)."})

    def repairs(self) -> list[dict]:
        """Réparations encore en place (non rétablies), des plus récentes aux plus anciennes."""
        commits = self.gh.get(f"{self.api}/commits", per_page=100) or []
        restored = {m[1] for c in commits if BACK in c["commit"]["message"]
                    for m in [SHA_RE.search(c["commit"]["message"])] if m}
        found = []
        for c in commits:
            msg = c["commit"]["message"]
            key = SHA_RE.search(msg)
            if key and BACK not in msg and key[1] not in restored:
                shas = REVERTED_RE.search(msg)
                wf = WORKFLOW_RE.search(msg)
                found.append({"sha": c["sha"], "key": key[1], "reverted": shas[1].split() if shas else [],
                              "workflow": int(wf[1]) if wf else None, "dev": "développeur: oui" in msg})
        return found

    def repaired_since(self, sha: str, base: str) -> bool:
        """Vrai si une réparation a été poussée sur la branche principale après ce commit."""
        try:
            log = sh("git", "log", "--format=%B", f"{sha}..origin/{base}", cwd=self.dir)
        except RuntimeError:
            return False
        return MARK in log

    def log_excerpt(self, run: dict, lines: int = 60) -> str:
        """Fin du journal du premier job en échec (visible par le propriétaire et le Développeur)."""
        try:
            jobs = self.gh.get(f"{self.api}/actions/runs/{run['id']}/jobs") or {}
            job = next((j for j in jobs.get("jobs", []) if j.get("conclusion") in BROKEN), None)
            if not job:
                return ""
            step = next((s["name"] for s in job.get("steps", []) if s.get("conclusion") in BROKEN), "?")
            raw = self.gh.text(f"{self.api}/actions/jobs/{job['id']}/logs")
        except Exception:  # noqa: BLE001 — l'extrait est un bonus, jamais bloquant
            return ""
        excerpt = error_excerpt(raw, lines)
        return (f"\n\n**Étape en échec :** {step}\n\n<details><summary>Journal de l'erreur</summary>\n\n"
                f"```\n{excerpt}\n```\n</details>")

    def merges_since(self, good: str, bad: str) -> list[str]:
        """Commits arrivés sur la branche principale après `good`, jusqu'à `bad` inclus,
        du plus récent au plus ancien."""
        if good == bad:
            return []
        try:
            out = sh("git", "rev-list", "--first-parent", f"{good}..{bad}", cwd=self.dir)
        except RuntimeError:
            return []
        return out.split()

    def revert(self, shas: list[str], message: str, base: str) -> str:
        """Annule les commits donnés (du plus récent au plus ancien) en un seul commit."""
        sh("git", "config", "user.name", "github-actions[bot]", cwd=self.dir)
        sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com",
           cwd=self.dir)
        sh("git", "fetch", "-q", "origin", base, cwd=self.dir)
        sh("git", "checkout", "-q", "-B", base, f"origin/{base}", cwd=self.dir)
        for sha in shas:
            parents = sh("git", "rev-list", "--parents", "-n", "1", sha, cwd=self.dir).split()
            mainline = ["-m", "1"] if len(parents) > 2 else []
            try:
                sh("git", "revert", "--no-commit", *mainline, sha, cwd=self.dir)
            except RuntimeError:
                sh("git", "revert", "--abort", cwd=self.dir, check=False)
                raise
        sh("git", "commit", "-q", "-m", message, cwd=self.dir)
        sh("git", "push", "-q", "origin", f"HEAD:{base}", cwd=self.dir)
        return sh("git", "rev-parse", "HEAD", cwd=self.dir).strip()

    # ----------------------------------------------------------------- logique
    def handle(self, run: dict) -> str:
        base = self.gh.repo(self.repo)["default_branch"]
        if run.get("head_branch") != base or run.get("event") == "pull_request":
            return "ignoré : pas la branche principale"
        name, wf_id = run["name"], run["workflow_id"]
        message = (run.get("head_commit") or {}).get("message", "")
        repaired = SHA_RE.search(message)
        if repaired and BACK in message:
            repaired = None  # réparation rétablie : ce code est traité comme du code ordinaire
        info = {}
        if repaired:
            info = {"key": repaired[1], "dev": "développeur: oui" in message,
                    "workflow": int(WORKFLOW_RE.search(message)[1]) if WORKFLOW_RE.search(message) else wf_id}

        if run["conclusion"] == "success":
            if not repaired or BACK in message:
                return "rien à faire"
            key = info["key"]
            if wf_id != info["workflow"]:
                self.follow_up(key, f"✅ « {name} » repasse aussi au vert ([run]({run['html_url']})).")
                return "autre workflow réparé"
            if info["dev"]:
                self.follow_up(key, f"✅ **Réparé** : « {name} » repasse au vert ([run]({run['html_url']})).")
                self.escalate(key, "les changements annulés contenaient des mises à jour ou des PR utiles, "
                                   "à réintégrer proprement sans la cause de la panne.")
                return "réparé, Développeur prévenu"
            self.follow_up(key, f"✅ **Réparé** : « {name} » repasse au vert après l'annulation "
                           f"([run]({run['html_url']})).", close=True)
            return "réparé"

        if run["conclusion"] not in BROKEN:
            return "rien à faire"

        if repaired:  # échec sur du code réparé
            key = info["key"]
            if BACK in message:
                return "déjà rétabli"
            if wf_id != info["workflow"]:
                self.follow_up(key, f"⚠️ « {name} » échoue aussi, même sur le code réparé "
                               f"([run]({run['html_url']})).")
                self.escalate(key, f"« {name} » échoue toujours après la réparation.")
                return "autre workflow en échec : Développeur prévenu"
            repair = self.repair_commit(key)
            if not repair:
                return "annulation introuvable"
            self.revert([repair], f"revert: rétablir {key[:7]} (l'annulation n'a pas aidé) "
                        f"{BACK} {MARK} {key}", base)
            self.follow_up(key, "⚠️ L'annulation **n'a pas suffi** : « " + name + " » échoue toujours "
                           f"([run]({run['html_url']})). Le changement a donc été **rétabli** : le "
                           "problème vient d'ailleurs (service externe, données, configuration).")
            self.escalate(key, "l'annulation n'a rien réparé.")
            return "rétabli"

        # Le code a été réparé depuis ce run : on relance sur le code actuel, rien à annuler.
        if self.repaired_since(run["head_sha"], base):
            if self.dispatch(wf_id, base):
                return "relancé sur le code réparé"
            return "code déjà réparé (relance manuelle impossible)"

        if run.get("run_attempt", 1) == 1:
            self.rerun(run)
            return "relancé une fois"

        runs = [r for r in self.gh.get(f"{self.api}/actions/workflows/{wf_id}/runs", branch=base,
                                       per_page=100)["workflow_runs"] if r["event"] != "pull_request"]
        good, bad = failure_window(runs)
        key = bad["head_sha"] if bad else f"{wf_id}-{run['head_sha']}"
        title = f"🚑 « {name} » en échec sur {self.repo}"
        if not bad:
            self.notify(key, title, f"« {name} » échoue ([run]({run['html_url']})) et n'a jamais "
                        "réussi : je ne peux pas désigner de coupable." + self.log_excerpt(run))
            self.escalate(key, "je ne peux pas désigner de coupable.")
            return "alerte : jamais réussi"
        if good["head_sha"] == bad["head_sha"]:
            # Même code que lors du dernier succès : rien à annuler, la cause est extérieure.
            if self.incident(key):
                return "cause externe déjà signalée"
            self.notify(key, title, f"⚠️ « {name} » échoue ([run]({run['html_url']})) alors que le code n'a "
                        f"**pas changé** depuis son dernier succès ([run]({good['html_url']})). La cause "
                        "vient de l'extérieur (service ou API indisponible, données absentes, secret "
                        "expiré…) : je n'annule rien." + self.log_excerpt(run))
            self.escalate(key, "la panne ne vient pas d'un changement de code : il faut l'analyser, "
                               "et rendre le code robuste si c'est possible.")
            return "alerte : cause externe"
        if self.already_tried(key):
            return "coupable déjà traité"
        already = {sha for r in self.repairs() for sha in r["reverted"]}
        window = self.merges_since(good["head_sha"], key)
        if window and all(sha in already for sha in window):  # déjà annulés par une réparation
            return "déjà annulé : relancé" if self.dispatch(wf_id, base) else "déjà annulé"
        suspects = [(sha, self.merged_pr(sha)) for sha in window if sha not in already]
        merged = [(sha, pr) for sha, pr in suspects
                  if pr and not (pr.get("head") or {}).get("ref", "").startswith(INFRA_PREFIXES)]
        direct = [sha for sha, pr in suspects if not pr]
        if not merged:
            self.notify(key, title, f"« {name} » échoue depuis le commit `{key[:7]}` "
                        f"([run]({bad['html_url']})), poussé directement sans PR : je ne l'annule pas."
                        + self.log_excerpt(run))
            self.escalate(key, "la panne vient d'un commit poussé directement, que je n'annule jamais.")
            return "alerte : commit direct"

        listing = "\n".join(f"- #{pr['number']} {pr['title']}" for _, pr in merged)
        plural = len(merged) > 1
        dev = any(not (pr.get("head") or {}).get("ref", "").startswith("agent/") for _, pr in merged)
        self.revert([sha for sha, _ in merged],
                    f"revert: annuler {len(merged)} merge(s) depuis le dernier état vert {MARK} {key}\n\n"
                    f"« {name} » échoue depuis ({bad['html_url']}). Annulation automatique par le "
                    f"Réparateur :\n{listing}\n\n"
                    f"annulés: {' '.join(sha for sha, _ in merged)}\nworkflow: {wf_id}\n"
                    f"développeur: {'oui' if dev else 'non'}", base)
        relaunched = self.dispatch(wf_id, base)
        intro = (f"🔧 « {name} » échoue depuis l'arrivée de **{len(merged)} merges rapprochés** "
                 "(les runs intermédiaires n'ont pas pu tourner, je ne sais pas lequel est en cause)"
                 if plural else f"🔧 « {name} » échouait depuis le merge de **#{merged[0][1]['number']} "
                 f"{merged[0][1]['title']}**")
        self.notify(key, title, f"{intro} ([run]({bad['html_url']})), même après une relance."
                    + self.log_excerpt(run) + "\n\n"
                    f"{'Merges annulés' if plural else 'Merge annulé'} automatiquement :\n{listing}\n\n"
                    + (f"Commits poussés directement, laissés en place : "
                       f"{', '.join(f'`{s[:7]}`' for s in direct)}\n\n" if direct else "")
                    + ("Le workflow a été **relancé** : je te confirme ici s'il repasse au vert. Les autres "
                       "workflows en échec seront relancés sur le code réparé."
                       if relaunched else "Ce workflow ne peut pas être relancé à la main : la "
                       "vérification aura lieu à son prochain passage.")
                    + ("\n\nUne fois réparé, le Développeur réintégrera proprement ce qui était utile."
                       if dev else ""))
        return "annulé et relancé"

def main() -> None:
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    repo = os.environ["REPO"].split("/", 1)[1]
    run = gh.get(f"/repos/{OWNER}/{repo}/actions/runs/{os.environ['RUN_ID']}")
    print(Repairer(gh, repo, Path(os.environ["REPO_DIR"]).resolve()).handle(run))


if __name__ == "__main__":
    main()
