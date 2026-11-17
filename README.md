# audit-repos

Agent d'audit mensuel de mes repos GitHub publics, propulsé par Claude Code (abonnement Pro).

L'agent **n'agit jamais sans validation** : il propose, je valide tâche par tâche, il
ouvre une Pull Request, et **je reste seul à merger**.

## Fonctionnement

```
1er du mois ──► Audit de chaque repo public (hors forks et archivés, nouveaux repos inclus)
                 · sécurité · maintenance · améliorations · opérationnel (fonctionnalités,
                   veille technique, méthodes, marchés)
                       │
                       ├─► une issue par proposition (ce repo, publique)
                       ├─► une issue par faille de sécurité (repo privé audit-securite)
                       └─► une issue « Récap » → e-mail de GitHub
                                │
Je pose le label `approuvé` ────┘
                       │
                       ▼
Agent : branche dédiée, code + tests, commits propres ──► Pull Request ──► je relis et merge
```

- **Quota Pro** : les repos sont audités un par un. Si le quota s'épuise, le passage
  suivant (toutes les 5 h environ, du 1er au 4) reprend là où il s'était arrêté.
  L'avancement est dans `state/AAAA-MM.json`.
- **Tâches validées** : traitées dès la pose du label (ce repo) ou au passage suivant,
  toutes les 3 h (repo de sécurité, reprises après quota).

## Valider ou refuser

| Je veux… | Je fais… |
|---|---|
| Valider une tâche | label `approuvé` |
| Refuser une tâche | fermer l'issue (elle ne sera plus reproposée) |
| Donner des précisions | un commentaire **avant** de poser `approuvé` |
| Réessayer après un échec | remettre `approuvé` |

Selon le type de tâche :

- **Correctif, refactoring, dépendances…** → une PR de code avec tests.
- **Nouvelle fonctionnalité** → d'abord une PR de **spécification** (`docs/specs/`).
  Je la merge si elle me convient, puis je remets `approuvé` pour lancer le code.
- **Marché, stratégie** (`non-codable`) → une analyse détaillée, sourcée, postée en commentaire.

Labels de suivi : `à-valider` → `en-cours` → `pr-ouverte` / `spec-en-revue` /
`analyse-livrée`, ou `à-préciser` (l'agent pose des questions) / `échec`.

## Sécurité

- Les failles ne sont jamais publiées ici : elles vont dans le repo **privé**
  `audit-securite`. Le récap public n'en donne que le nombre.
- Les tokens GitHub ne sont jamais transmis à Claude. C'est le script qui pousse les
  branches et ouvre les PR.
- Seules les issues créées avec mon compte et labellisées par moi déclenchent l'agent.
- L'agent ne merge jamais et ne pousse jamais sur `main`.

## Installation (une seule fois)

Secrets du repo (*Settings → Secrets and variables → Actions*) :

| Secret | Contenu |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | résultat de `claude setup-token` (lié à l'abonnement Pro) |
| `GH_PAT` | token GitHub *fine-grained*, *All repositories* : Contents, Issues, Pull requests, Workflows en lecture/écriture |

## Lancer à la main

*Actions → Audit mensuel → Run workflow* :

- `repos` = `pricer` : audit de test sur un seul repo (récap marqué « test »).
- `force` : relancer tout l'audit du mois.

## Réglages

`config.json` : repos exclus, modèle, nombre maximal de propositions par repo, nombre de
tâches par passage. Les consignes de l'agent sont dans `agent/prompts/`.
