"""Tests hors ligne du Développeur : faux GitHub en mémoire, aucun appel réseau ni Claude.

Lancer depuis la racine du repo :  python3 tests/test_agent.py
"""

import copy
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update(GH_PAT="pat-secret", GITHUB_TOKEN="bot", RUNNER_TEMP=tempfile.mkdtemp(),
                  DRY_RUN="1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

import common  # noqa: E402
import implement as im  # noqa: E402

im.time.sleep = lambda s: None
OWNER = {"login": "menckley"}


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def old_issue(number, cat, kind, effort, fid, title):
    """Issue au format d'avant les cases à cocher (label `approuvé`)."""
    body = (f"**Repo :** [menckley/pricer](https://github.com/menckley/pricer) · **Catégorie :** x · "
            f"**Type :** {kind} · **Priorité :** moyenne · **Effort :** {effort}\n\n"
            "### Constat\n\nconstat\n\n### Proposition\n\nproposition\n\n"
            "### Sources\n\n- https://example.org\n\n---\n\n"
            "✅ **Valider :** ajoute le label `approuvé` → l'agent ouvre une Pull Request.\n\n"
            "❌ **Refuser :** ferme l'issue.\n\n"
            f"<!-- audit-finding: pricer/{fid} -->")
    labels = ["audit", "à-valider", "repo:pricer", f"cat:{cat}", f"type:{kind}"]
    return {"number": number, "title": f"[pricer] {title}", "state": "open", "user": OWNER,
            "body": body, "labels": [{"name": n} for n in labels]}


class FakeGH:
    def __init__(self):
        self.issues_db, self.comments_db, self.prs, self.checks = {}, {}, {}, {}
        self.merged, self.runs, self.wf_before, self.closed = [], {}, {}, []
        self.dependabot_prs, self.created, self.bilan = {}, [], ""

    def issues(self, repo, state="open", labels=None):
        return [i for (r, _), i in self.issues_db.items() if r == repo and i["state"] == "open"]

    def get(self, path, **params):
        if "/issues/" in path:
            parts = path.split("/")
            return self.issues_db[(parts[3], int(parts[5]))]
        if "/actions/workflows/" in path:
            return {"workflow_runs": self.wf_before.get(int(path.split("/")[6]), [])}
        return None

    def comments(self, repo, n):
        return self.comments_db.get((repo, n), [])

    def comment(self, repo, n, body):
        self.comments_db.setdefault((repo, n), []).append({"user": OWNER, "body": common.AGENT_MARK + body})

    def update_issue(self, repo, n, **fields):
        self.issues_db[(repo, n)].update(fields)

    def close_issue(self, repo, n, reason="completed"):
        self.issues_db[(repo, n)]["state"] = "closed"
        self.closed.append((repo, n, reason))

    def add_labels(self, repo, n, labels):
        issue = self.issues_db[(repo, n)]
        names = {label["name"] for label in issue["labels"]}
        issue["labels"] += [{"name": label} for label in labels if label not in names]

    def remove_label(self, repo, n, label):
        issue = self.issues_db[(repo, n)]
        issue["labels"] = [x for x in issue["labels"] if x["name"] != label]

    def repo(self, name):
        return {"default_branch": "main"}

    def pull(self, repo, n):
        return self.prs[(repo, n)]

    def check_runs(self, repo, sha):
        return self.checks.get(sha, [])

    def annotations(self, repo, run_id):
        return [{"path": "a.py", "start_line": 3, "message": "assert"}]

    def merge_pr(self, repo, n, title):
        self.merged.append((repo, n))
        return {"sha": f"m{n}"}

    def update_branch(self, repo, n):
        pass

    def delete_branch(self, repo, branch):
        pass

    def open_pulls(self, repo):
        return self.dependabot_prs.get(repo, [])

    def write(self, method, path, data=None):
        self.comments_db.setdefault(path, []).append(data)
        return {}

    def public_repos(self):
        return [{"name": "pricer"}]

    def workflow_runs(self, repo, **params):
        return self.runs.get(repo, [])

    def open_pr_for(self, *args):
        return None

    def create_pr(self, repo, branch, base, title, body):
        self.created.append(title)
        return {"number": 99, "html_url": "https://pr/99"}

    def create_issue(self, repo, title, body, labels):
        self.created.append(title)
        self.bilan = body
        return {"number": 500, "html_url": "https://bilan"}


def labels(issue):
    return sorted(label["name"] for label in issue["labels"])


def main():
    gh = FakeGH()
    # --- 1. migration des anciennes issues
    gh.issues_db[("audit-repos", 2)] = old_issue(2, "operationnel", "methode", "L", "backtest", "Backtest")
    gh.issues_db[("audit-repos", 3)] = old_issue(3, "maintenance", "dependances", "M", "deps", "Dépendances")
    im.migrate_backlog(gh, {"audit-repos": gh.issues("audit-repos")})
    b2, b3 = gh.issues_db[("audit-repos", 2)], gh.issues_db[("audit-repos", 3)]
    assert "- [ ] ✅ **Lancer**" in b2["body"] and "auto" not in labels(b2), "opérationnel → cases"
    assert "Traitée automatiquement" in b3["body"] and "auto" in labels(b3) and "à-valider" not in labels(b3)
    assert "label `approuvé`" not in b3["body"] and common.MARKER_RE.search(b3["body"]) and common.V2 in b3["body"]
    assert "### Sources" in b3["body"], "contenu conservé"
    before = b3["body"]
    im.migrate_backlog(gh, {"audit-repos": gh.issues("audit-repos")})
    assert b3["body"] == before, "idempotent"
    print("OK 1 migration")

    # --- 2/3/4. refus, lancement manuel, tâche auto
    b2["body"] = b2["body"].replace("- [ ] ❌ **Refuser**", "- [x] ❌ **Refuser**")
    i4 = copy.deepcopy(b2)
    i4.update(number=4, state="open")
    i4["body"] = b2["body"].replace("- [x] ❌", "- [ ] ❌").replace("- [ ] ✅ **Lancer**", "- [x] ✅ **Lancer**")
    i4["body"] = i4["body"].replace("pricer/backtest", "pricer/backtest2")
    gh.issues_db[("audit-repos", 4)] = i4
    tasks = []

    def fake_code_task(g, target, branch, values, ref):
        tasks.append((branch, ref, values["NOTES"]))
        return {"number": len(tasks), "html_url": f"https://pr/{len(tasks)}"}

    im.code_task = fake_code_task
    im.save_state = lambda st: None
    im.ci_first = lambda g, st, q, o: q
    st = {"pending": [], "watch": [], "log": [], "seen": [], "last_bilan": None}
    im.load_state = lambda: st
    gh.comments_db[("audit-repos", 4)] = [{"user": OWNER, "body": "Garde la compat Python 3.11"},
                                          {"user": {"login": "x"}, "body": "pirate"}]
    im.GitHub = lambda token: gh
    im.main()
    assert ("audit-repos", 2, "not_planned") in gh.closed and "refusé" in labels(b2), "refus"
    assert "Lancée le" in i4["body"] and "- [x]" not in i4["body"] and "pr-ouverte" in labels(i4)
    assert "pr-ouverte" in labels(b3), "auto démarrée"
    assert any("Garde la compat" in t[2] and "pirate" not in t[2] for t in tasks), "commentaires"
    assert len(st["pending"]) == 2
    print("OK 2-4 refus / lancement / auto")

    # --- 5. CI verte → merge ; CI rouge → une tentative de correction
    p_auto = next(p for p in st["pending"] if p["issue"] == 3)
    p_man = next(p for p in st["pending"] if p["issue"] == 4)
    gh.prs[("pricer", p_auto["number"])] = {"merged": False, "state": "open", "head": {"sha": "s1"},
                                            "mergeable": True, "title": "fix: deps"}
    gh.prs[("pricer", p_man["number"])] = {"merged": False, "state": "open", "head": {"sha": "s2"},
                                           "mergeable": True, "title": "feat: x"}
    gh.checks["s1"] = [{"id": 1, "name": "tests", "status": "completed", "conclusion": "success"}]
    gh.checks["s2"] = [{"id": 2, "name": "tests", "status": "completed", "conclusion": "failure",
                        "output": {"summary": "1 failed"}}]
    fixes = []
    im.fix_ci = lambda g, e, failed, title: fixes.append(im.failure_report(g, e["target"], failed)) or True
    im.advance_pending(gh, st, True)
    assert ("pricer", p_auto["number"]) in gh.merged and ("audit-repos", 3, "completed") in gh.closed
    assert "mergé" in labels(b3) and len(st["watch"]) == 1 and st["log"][-1]["type"] == "merged"
    assert fixes and "assert" in fixes[0] and p_man["fix_attempted"]
    print("OK 5 merge auto + tentative de correction")

    # --- 6. toujours rouge → renvoyée avec les cases
    im.advance_pending(gh, st, True)
    assert "à-relire" in labels(i4) and "- [ ] ✅ **Lancer**" in i4["body"] and not st["pending"]
    print("OK 6 CI rouge deux fois → à relire")

    # --- 7. aucune CI
    st["pending"].append(dict(p_auto, issue=4, opened=ago(minutes=50), number=7, fix_attempted=False))
    gh.prs[("pricer", 7)] = {"merged": False, "state": "open", "head": {"sha": "s7"}, "mergeable": True,
                             "title": "t"}
    im.advance_pending(gh, st, True)
    assert st["log"][-1]["type"] == "review" and "aucune CI" in st["log"][-1]["detail"]
    print("OK 7 sans CI → à relire")

    # --- 7b. conflit → tâche refaite une fois, puis renvoyée
    i4["labels"] = [{"name": "auto"}, {"name": "pr-ouverte"}]
    gh.prs[("pricer", 8)] = {"merged": False, "state": "open", "head": {"sha": "s1"}, "mergeable": False,
                             "title": "t"}
    gh.update_branch = lambda r, n: (_ for _ in ()).throw(common.GitHubError(422, "conflit"))
    conflict = dict(p_auto, issue=4, number=8, branch_updated=False, fix_attempted=False)
    st["pending"].append(dict(conflict))
    im.advance_pending(gh, st, True)
    assert not st["pending"] and "pr-ouverte" not in labels(i4) and "auto" in labels(i4)
    i4["labels"].append({"name": "pr-ouverte"})
    st["pending"].append(dict(conflict, branch_updated=True))
    im.advance_pending(gh, st, True)
    assert "à-relire" in labels(i4)
    print("OK 7b conflit → refaite, puis renvoyée")

    # --- 8. Dependabot
    def dep(n, title, ref="dependabot/pip/x"):
        return {"number": n, "title": title, "user": {"login": "dependabot[bot]"},
                "head": {"ref": ref, "sha": f"d{n}"}, "html_url": f"https://dep/{n}", "created_at": ago(hours=1)}

    assert not im.is_major(dep(1, "Update requests requirement from >=2.31 to >=2.34.2"))
    assert im.is_major(dep(2, "Bump pandas from 2.2.0 to 3.1.1"))
    assert not im.is_major(dep(3, "Bump actions/checkout from 4 to 7", "dependabot/github_actions/a"))
    gh.dependabot_prs["pricer"] = [dep(10, "Bump pytest from 9.1 to 9.2.0"),
                                   dep(11, "Bump pyyaml from 6.0 to 6.0.3"),
                                   dep(12, "Bump pandas from 2.2 to 3.1")]
    for n in (10, 11):
        gh.checks[f"d{n}"] = [{"id": n, "name": "t", "status": "completed", "conclusion": "success"}]
        gh.prs[("pricer", n)] = {"mergeable": True, "state": "open"}
    n0 = len(gh.merged)
    im.dependabot(gh, st, ["pricer"])
    assert gh.merged[n0:] == [("pricer", 10)], "une seule par passage"
    assert any(e["type"] == "dependabot_major" for e in st["log"])
    gh.dependabot_prs["pricer"] = gh.dependabot_prs["pricer"][1:]
    im.dependabot(gh, st, ["pricer"])
    assert sum(e["type"] == "dependabot_major" for e in st["log"]) == 1, "majeure signalée une fois"
    print("OK 8 Dependabot")

    # --- 9. une annulation faite par le Réparateur rouvre la tâche de l'agent
    agent_merge = st["watch"][0]
    sha = agent_merge["sha"]
    commits = {"pricer": [{"sha": "x", "commit": {"message": "docs: autre chose"}}]}
    gh.get_commits = commits
    real_get = gh.get

    def get(path, **params):
        if path.endswith("/commits"):
            return commits[path.split("/")[3]]
        return real_get(path, **params)

    gh.get = get
    im.watch(gh, st)
    assert agent_merge in st["watch"], "rien d'annulé : la tâche reste close"
    commits["pricer"].insert(0, {"sha": "r", "commit": {"message": f"revert: x {common.REPAIR_MARK} {sha}"}})
    im.watch(gh, st)
    assert agent_merge not in st["watch"] and st["log"][-1]["type"] == "reverted"
    assert b3["state"] == "open" and "annulé" in labels(b3) and "- [ ] ✅ **Lancer**" in b3["body"]
    gh.get = real_get
    print("OK 9 annulation du Réparateur reportée sur la tâche")

    # --- 10. confidentialité et bilan
    im.log(st, "human", title="[pricer] faille X", issue="https://github.com/menckley/audit-securite/issues/1",
           security=True, detail="Révoque la clé X")
    assert all("faille X" not in str(e) and "Révoque" not in str(e) for e in st["log"]), "état public"
    im.maybe_bilan(gh, gh, st, busy=True)
    assert not any("Bilan" in t for t in gh.created)
    im.maybe_bilan(gh, gh, st, busy=False)
    body = gh.bilan
    assert "🛠️ Bilan" in gh.created[-1] and all(e["reported"] for e in st["log"])
    assert "Terminé :" in body and "T'attend :" in body and "Ce qui t'attend" in body
    assert "À faire par toi" in body and "Correctif de sécurité" in body and "faille X" not in body
    assert "Annulé automatiquement" in body
    im.maybe_bilan(gh, gh, st, busy=False)
    assert sum("Bilan" in t for t in gh.created) == 1, "pas de doublon"
    print("OK 10 confidentialité et bilan")

    # --- 11. bilan : un point réglé depuis n'est plus signalé, doublons fusionnés
    entries = [{"type": "review", "pr": "https://github.com/menckley/newsletter/pull/7", "title": "a"},
               {"type": "review", "pr": "https://github.com/menckley/newsletter/pull/7", "title": "a"},
               {"type": "merged", "pr": "https://github.com/menckley/newsletter/pull/7", "title": "a"}]
    assert len(im.dedupe(entries)) == 2
    gh.prs[("newsletter", 7)] = {"merged": True, "state": "closed"}
    assert im.still_waiting(gh, entries[0]) is False
    print("OK 11 bilan à jour de l'état réel")

    # --- 12. l'état « mergeable » est redemandé
    calls = {"n": 0}

    class Slow:
        def pull(self, repo, n):
            calls["n"] += 1
            return {"state": "open", "mergeable": None if calls["n"] < 3 else True}

    assert im.pull_with_mergeable(Slow(), "x", 1)["mergeable"] is True and calls["n"] == 3
    print("OK 12 mergeable redemandé")


def test_alert_install():
    ci = ("CI", "name: CI\n\non:\n  push:\n    branches: [main]\n  pull_request:\n\njobs: {}\n", "s1")
    daily = (".github/workflows/daily.yml", "name: HPFC quotidienne\non:\n  schedule:\n    - cron: '0 6 * * *'\n"
             "  workflow_dispatch:\n", "s2")
    files = [(".github/workflows/ci.yml", ci[1], "s1"), daily]
    changes = im.alert_changes(files)
    assert set(changes) == {".github/workflows/ci.yml", im.ALERT_PATH}
    assert changes[".github/workflows/ci.yml"].startswith("name: CI\n\non:\n  workflow_dispatch:\n  push:")
    assert '"CI", "HPFC quotidienne"' in changes[im.ALERT_PATH]
    assert "reparateur.yml@main" in changes[im.ALERT_PATH]
    files = [(".github/workflows/ci.yml", changes[".github/workflows/ci.yml"], "s1"), daily,
             (im.ALERT_PATH, changes[im.ALERT_PATH], "s3")]
    assert im.alert_changes(files) == {}, "déjà à jour : aucune PR"
    print("OK 13 installation de l'alerte (et relance manuelle ajoutée)")


def test_repairer():
    import repair

    class G:
        def __init__(self):
            self.calls, self.issues_list, self.created = [], [], []
            self.commits, self.pulls, self.runs = [], {}, []

        def repo(self, r):
            return {"default_branch": "main"}

        def get(self, path, **p):
            if path.endswith("/commits"):
                return self.commits
            if "/pulls" in path:
                return self.pulls.get(path.split("/")[-2], [])
            if "/runs" in path:
                return {"workflow_runs": self.runs}
            return None

        def write(self, method, path, data=None):
            self.calls.append((method, path))
            return {}

        def issues(self, repo, state="open", labels=None):
            return [i for i in self.issues_list if i["state"] == "open"]

        def create_issue(self, repo, title, body, labels):
            issue = {"number": len(self.issues_list) + 1, "body": body, "state": "open", "title": title}
            self.issues_list.append(issue)
            self.created.append(title)
            return issue

        def close_issue(self, repo, n, reason="completed"):
            self.issues_list[n - 1]["state"] = "closed"

    git = []
    BAD = "b" * 40
    repair.sh = lambda *a, **k: git.append(a) or (f"{BAD} p1 p2" if "rev-list" in a else "newsha\n")

    def run(conclusion, sha, minutes, attempt=1, message="feat: x"):
        return {"id": minutes, "workflow_id": 7, "name": "HPFC quotidienne", "event": "schedule",
                "status": "completed", "conclusion": conclusion, "head_sha": sha, "head_branch": "main",
                "run_attempt": attempt, "created_at": ago(minutes=minutes), "html_url": f"https://run/{sha}",
                "head_commit": {"message": message}}

    g = G()
    rp = repair.Repairer(g, "power-hpfc", Path("."))
    # 1er échec : simple relance
    assert rp.handle(run("failure", BAD, 5)) == "relancé une fois"
    assert ("POST", "/repos/menckley/power-hpfc/actions/runs/5/rerun-failed-jobs") in g.calls
    # 2e échec : le merge de la PR #9 est coupable → annulé, relancé, issue ouverte
    g.runs = [run("success", "good", 60), run("failure", BAD, 30), run("failure", BAD, 5, 2)]
    g.pulls[BAD] = [{"number": 9, "title": "Bump python-dateutil", "merged_at": "x", "merge_commit_sha": BAD}]
    assert rp.handle(run("failure", BAD, 5, attempt=2)) == "annulé et relancé"
    assert any(a[:2] == ("git", "revert") and BAD in a for a in git) and ("git", "revert", "--no-edit", "-m", "1", BAD) in git
    assert ("POST", "/repos/menckley/power-hpfc/actions/workflows/7/dispatches") in g.calls
    assert len(g.created) == 1 and "#9" in g.issues_list[0]["body"] and "@menckley" in g.issues_list[0]["body"]
    # le workflow relancé repasse au vert → « réparé », issue fermée
    msg = f"revert: Bump python-dateutil (#9) {repair.MARK} {BAD}"
    assert rp.handle(run("success", "rev", 1, message=msg)) == "réparé"
    assert g.issues_list[0]["state"] == "closed"
    assert rp.handle(run("success", "rev", 0, message=msg)) == "réparé" and len(g.created) == 1, "pas de doublon"
    # coupable déjà traité : on n'annule pas deux fois
    g.commits = [{"sha": "rev", "commit": {"message": msg}}]
    assert rp.handle(run("failure", BAD, 5, attempt=2)) == "coupable déjà traité"
    # commit poussé directement (sans PR) : alerte seulement
    g2 = G(); rp2 = repair.Repairer(g2, "power-hpfc", Path("."))
    g2.runs = [run("success", "good", 60), run("failure", "direct", 30)]
    assert rp2.handle(run("failure", "direct", 5, attempt=2)) == "alerte : commit direct"
    # échec malgré l'annulation : le changement est rétabli, alerte
    g3 = G(); rp3 = repair.Repairer(g3, "power-hpfc", Path("."))
    g3.issues_list.append({"number": 1, "state": "open", "body": f"<!-- incident: {BAD} -->", "title": "t"})
    g3.commits = [{"sha": "rev", "commit": {"message": msg}}]
    git.clear()
    assert rp3.handle(run("failure", "rev", 1, message=msg)) == "rétabli"
    assert any(a[:2] == ("git", "revert") and "rev" in a for a in git)
    # échec transitoire puis succès, branche autre que main : rien
    assert rp.handle(dict(run("failure", "x", 1), head_branch="feature")).startswith("ignoré")
    print("OK 14 Réparateur : relance, annulation du coupable, réparé, rétabli, commit direct")


if __name__ == "__main__":
    main()
    test_alert_install()
    test_repairer()
    print("\nTous les tests passent.")
