# audit-repos

Agent d'audit et de maintenance de mes repos GitHub publics, propulsé par Claude Code
(abonnement Pro).

## Deux rôles

- **🔍 L'Auditeur** (workflow `audit.yml`) : le 1er du mois, il lit et analyse chaque repo sans rien modifier, puis écrit les propositions et le récap.
- **🛠️ Le Développeur** (workflow `implement.yml`) : toutes les heures, et dès que je coche une case, il code, ouvre les PR, merge si la CI est verte, surveille, annule si besoin et rédige le bilan.

Ils ne se parlent qu'à travers les issues : l'Auditeur les écrit, le Développeur les traite.

## Fonctionnement

```
1er du mois ──► Audit de chaque repo public (hors forks et archivés, nouveaux repos inclus)
                 · sécurité · maintenance · améliorations · opérationnel (fonctionnalités,
                   veille technique, méthodes, marchés)
                       │
        ┌──────────────┴───────────────┐
        ▼                              ▼
 Sécurité (tout) + maintenance  Amélioration + opérationnel
 (sauf changements majeurs)     (et changements majeurs)
        │                              │
   🤖 automatique              ☑️ je coche « Lancer » (+ commentaire facultatif)
        │                              │
        └──────────────┬───────────────┘
                       ▼
     Agent : branche, code + tests ──► PR ──► CI verte ? ──► merge automatique
                                                  │ rouge : 1 tentative de correction,
                                                  │ sinon PR laissée ouverte pour moi
                                                  ▼
                       Surveillance 48 h : si un workflow du repo casse ──► annulation
                                                  ▼
                              🛠️ Bilan détaillé par e-mail
```

## Ce que je reçois par e-mail

- **📋 Récap d'audit** (début de mois) : ce qui sera traité automatiquement et ce qui
  attend ma décision.
- **🛠️ Bilan automatique**, une fois le travail terminé (au plus un toutes les 12 h) :
  ce qui a été mergé, annulé, ce qui demande mon attention, et les actions hors code
  que je suis seul à pouvoir faire (ex. révoquer un secret).

## Décider d'une proposition

Chaque proposition d'amélioration ou opérationnelle se termine par :

- [ ] ✅ **Lancer** : l'agent la réalise et merge si la CI est verte
- [ ] ❌ **Refuser** : l'issue est fermée et ne sera plus proposée

Un clic suffit. Pour ajouter des précisions, j'écris un commentaire **avant** de cocher :
l'agent le lit et le fait passer avant la proposition initiale.

Une nouvelle fonctionnalité commence par sa spécification (`docs/specs/`), commitée dans
la même PR que le code. Une piste de marché donne une analyse sourcée en commentaire,
sans code.

## Garde-fous

- **Merge automatique seulement si la CI est verte.** Sans CI dans le repo, la PR reste
  ouverte et l'agent me le signale.
- **Une seule tentative de correction** d'une CI rouge, sans jamais supprimer ni affaiblir
  un test.
- **Annulation automatique** si un workflow de la branche principale (collecte,
  newsletter, CI…) échoue dans les 48 h après un merge de l'agent alors qu'il passait avant.
- **Changements majeurs** hors sécurité (version majeure d'une bibliothèque ou de Python, workflow de
  collecte, format de données) et efforts L/XL : toujours soumis à ma décision.
- **Failles de sécurité** : détails uniquement dans le repo privé `audit-securite`.
- **Dependabot** : mises à jour non majeures mergées si la CI est verte, une par repo et
  par heure. Les versions majeures restent à ma décision.
- Les tokens GitHub ne sont jamais transmis à Claude. Seules les issues créées par mon
  compte, et mes propres clics, déclenchent l'agent.

## Installation (une seule fois)

Secrets du repo (*Settings → Secrets and variables → Actions*) :

| Secret | Contenu |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | résultat de `claude setup-token` (lié à l'abonnement Pro) |
| `GH_PAT` | token GitHub *fine-grained*, *All repositories* : Contents, Issues, Pull requests, Workflows en lecture/écriture |

## Lancer à la main

- *Actions → 🔍 Auditeur → Run workflow* : audit complet (`repos` vide) ou d'un seul
  repo (`repos` = `pricer`, récap marqué « test »).
- *Actions → 🛠️ Développeur → Run workflow* : fait avancer le travail sans attendre l'heure suivante.

## Réglages

`config.json` : catégories et efforts traités automatiquement, durée de surveillance,
intervalle minimal entre deux bilans, Dependabot, repos exclus, modèle. Les consignes de
l'agent sont dans `agent/prompts/`. L'état de l'agent est dans `state/`.
