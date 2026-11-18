"""🚑 Le Réparateur : appelé par le fichier d'alerte d'un repo dès qu'un de ses workflows
se termine sur la branche principale.

1. Échec au premier essai          → relance du workflow tel quel (souvent une panne externe).
2. Échec au second essai           → recherche du commit coupable (premier run en échec
                                     après le dernier succès). Si c'est le merge d'une PR,
                                     il est annulé, puis le workflow est relancé.
3. Succès après une annulation     → « réparé ».
4. Échec après une annulation      → le code n'était pas en cause : le changement est
                                     rétabli et le propriétaire alerté.

Un même commit n'est jamais annulé deux fois. Un commit poussé directement (sans PR) n'est
jamais annulé. Chaque incident fait l'objet d'une issue qui mentionne le propriétaire
(donc un e-mail), puis de commentaires à chaque étape.

Tourne avec le GITHUB_TOKEN du repo concerné (aucun secret supplémentaire).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from common import BROKEN, OWNER, REPAIR_BACK, REPAIR_MARK, GitHub, GitHubError, first_failure, sh

MARK = REPAIR_MARK
BACK = REPAIR_BACK
LABEL = "réparation"
SHA_RE = re.compile(re.escape(MARK) + r" ([0-9a-f]{40})")


class Repairer:
    def __init__(self, gh: GitHub, repo: str, repo_dir: Path):
        self.gh, self.repo, self.dir = gh, repo, repo_dir
        self.api = f"/repos/{OWNER}/{repo}"

    # ----------------------------------------------------------------- notifications
    def incident(self, key: str) -> dict | None:
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
            number = self.gh.create_issue(self.repo, title, body, [LABEL])["number"]
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

    def revert(self, sha: str, message: str, base: str) -> str:
        sh("git", "config", "user.name", "github-actions[bot]", cwd=self.dir)
        sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com",
           cwd=self.dir)
        sh("git", "fetch", "-q", "origin", base, cwd=self.dir)
        sh("git", "checkout", "-q", "-B", base, f"origin/{base}", cwd=self.dir)
        parents = sh("git", "rev-list", "--parents", "-n", "1", sha, cwd=self.dir).split()
        mainline = ["-m", "1"] if len(parents) > 2 else []
        sh("git", "revert", "--no-edit", *mainline, sha, cwd=self.dir)
        sh("git", "commit", "-q", "--amend", "-m", message, cwd=self.dir)
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

        if run["conclusion"] == "success":
            if repaired and BACK not in message:
                key = repaired[1]
                self.follow_up(key, f"✅ **Réparé** : « {name} » repasse au vert après l'annulation "
                            f"([run]({run['html_url']})). Le changement fautif reste annulé : il "
                            "faudra l'adapter avant de le remettre.", close=True)
                return "réparé"
            return "rien à faire"

        if run["conclusion"] not in BROKEN:
            return "rien à faire"

        if repaired:  # échec malgré l'annulation : le code n'était pas en cause
            key = repaired[1]
            if BACK in message:
                return "déjà rétabli"
            repair = self.repair_commit(key)
            if not repair:
                return "annulation introuvable"
            self.revert(repair, f"revert: rétablir {key[:7]} (l'annulation n'a pas aidé) "
                        f"{BACK} {MARK} {key}", base)
            self.follow_up(key, "⚠️ L'annulation **n'a pas suffi** : « " + name + "» échoue toujours "
                        f"([run]({run['html_url']})). Le changement a donc été **rétabli** : le problème "
                        "vient d'ailleurs (service externe, données, configuration). **À regarder.**")
            return "rétabli"

        if run.get("run_attempt", 1) == 1:
            self.rerun(run)
            return "relancé une fois"

        runs = [r for r in self.gh.get(f"{self.api}/actions/workflows/{wf_id}/runs", branch=base,
                                       per_page=100)["workflow_runs"] if r["event"] != "pull_request"]
        bad = first_failure(runs)
        key = bad["head_sha"] if bad else f"{wf_id}-{run['head_sha']}"
        title = f"🚑 « {name} » en échec sur {self.repo}"
        if not bad:
            self.notify(key, title, f"« {name} » échoue ([run]({run['html_url']})) et n'a jamais "
                        "réussi : je ne peux pas désigner de coupable. **À regarder.**")
            return "alerte : jamais réussi"
        culprit = bad["head_sha"]
        if self.already_tried(culprit):
            return "coupable déjà traité"
        pr = self.merged_pr(culprit)
        if not pr:
            self.notify(key, title, f"« {name} » échoue depuis le commit `{culprit[:7]}` "
                        f"([run]({bad['html_url']})), poussé directement sans PR : je ne l'annule pas. "
                        "**À regarder.**")
            return "alerte : commit direct"

        self.revert(culprit, f"revert: {pr['title']} (#{pr['number']}) {MARK} {culprit}\n\n"
                    f"« {name} » échoue depuis ce merge ({bad['html_url']}). Annulation automatique "
                    "par le Réparateur.", base)
        relaunched = self.dispatch(wf_id, base)
        self.notify(key, title, f"🔧 « {name} » échouait depuis le merge de **#{pr['number']} "
                    f"{pr['title']}** ([run]({bad['html_url']})), même après une relance.\n\n"
                    "- Ce merge a été **annulé** automatiquement.\n"
                    + ("- Le workflow a été **relancé** : je te confirme ici s'il repasse au vert."
                       if relaunched else "- Ce workflow ne peut pas être relancé à la main : "
                       "la vérification aura lieu à son prochain passage."))
        return "annulé et relancé"


def main() -> None:
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    repo = os.environ["REPO"].split("/", 1)[1]
    run = gh.get(f"/repos/{OWNER}/{repo}/actions/runs/{os.environ['RUN_ID']}")
    print(Repairer(gh, repo, Path(os.environ["REPO_DIR"]).resolve()).handle(run))


if __name__ == "__main__":
    main()
