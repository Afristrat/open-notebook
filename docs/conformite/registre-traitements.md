# Registre des activités de traitement — Dīwān

> Établi dans l'esprit de l'article 30 du RGPD. Reflète l'état **réel et vérifié**
> du système au 24/06/2026. À tenir à jour à chaque évolution fonctionnelle.

## Responsable de traitement

- **Entité** : `[À COMPLÉTER]` (Afrique Stratégie — raison sociale, adresse).
- **Point de contact données** : `[À COMPLÉTER]` (courriel dédié, ex. `privacy@…`).
- **Délégué à la protection des données (DPO)** : `[À COMPLÉTER si désigné]`.

## Vue d'ensemble des traitements

| # | Traitement | Finalité | Catégories de données | Base légale (à confirmer) |
|---|---|---|---|---|
| T1 | Téléversement et indexation de sources | Recherche, organisation documentaire | Contenu fourni par l'utilisateur (PDF, audio, vidéo, URL), métadonnées, texte extrait, embeddings vectoriels | Intérêt légitime / exécution d'un service |
| T2 | Notes et insights | Production de notes liées aux sources | Texte saisi ou généré, liens vers sources | Intérêt légitime |
| T3 | Conversations avec l'IA (chat) | Assistance à la recherche | Messages de l'utilisateur + contexte documentaire transmis au modèle | Intérêt légitime + **transfert tiers (T7)** |
| T4 | Transcription audio/vidéo (STT) | Rendre le contenu parlé exploitable | Fichiers audio, transcription textuelle | Intérêt légitime |
| T5 | Génération de podcasts (TTS) | Production de contenus audio | Transcript généré, voix de synthèse, fichiers MP3 | Intérêt légitime |
| T6 | Recherche sémantique | Retrouver le contenu pertinent | Embeddings, requêtes | Intérêt légitime |
| T7 | Recours à des modèles d'IA externes | Capacités conversationnelles | Contenu transmis au fournisseur du modèle | **À encadrer** (voir transferts) |

> ⚠️ Les **bases légales** ci‑dessus sont des hypothèses de travail. Elles doivent
> être confirmées selon le contexte d'usage (interne vs clients) et, le cas
> échéant, le consentement recueilli.

## Données à caractère personnel : nature

Dīwān ne crée pas de profils d'utilisateurs nominatifs (voir « Comptes » plus bas).
Les données personnelles éventuelles proviennent **du contenu téléversé** : un PDF,
un enregistrement ou une page web peut contenir des noms, coordonnées, voire des
données sensibles. Le système ne les catégorise pas automatiquement → la vigilance
repose sur l'utilisateur et sur les durées de conservation (cf.
`politique-retention.md`).

## Comptes et authentification

- Mécanisme prévu : middleware par mot de passe partagé (`OPEN_NOTEBOOK_PASSWORD`).
- **État réel au 24/06/2026 : authentification désactivée** (variable non définie)
  → pas de comptes individuels, pas de journal d'accès nominatif. L'accès est
  protégé uniquement par l'obscurité de l'URL et le réseau. **À corriger** avant
  tout traitement de données réelles de tiers.

## Localisation et stockage des données

| Donnée | Emplacement | Persistance |
|---|---|---|
| Notebooks, sources, notes, chats, épisodes | SurrealDB (volume Docker nommé) | Persistant (vérifié) |
| Fichiers téléversés | `/app/data/uploads` (volume) | Persistant |
| Podcasts générés (outline/transcript/clips/audio) | `/app/data/podcasts/episodes/<uuid>` | Persistant |
| Identifiants des fournisseurs d'IA | SurrealDB, **chiffrés (Fernet)** | Persistant |

Serveur : infrastructure sous contrôle direct d'Afrique Stratégie (souveraineté
des données). **Disque hôte non chiffré** au repos (cf. `mesures-techniques.md`).

## Destinataires et sous-traitants (transferts)

| Destinataire | Donnée transmise | Localisation | Statut |
|---|---|---|---|
| **Anthropic** (Claude / Opus) | Contenu envoyé au chat et aux transformations | États‑Unis | **Transfert hors UE/Maroc** — à encadrer (clauses, information des personnes) |
| Modèles locaux (Ollama, VoxCPM2, Speaches) | Embeddings, TTS, STT | Serveur Afrique Stratégie | Souverain, pas de transfert |

> Action : si du contenu personnel transite par le chat, le recours à Anthropic
> doit être encadré (information des personnes, voire bascule sur un modèle local
> pour les traitements sensibles).

## Durées de conservation

Voir [`politique-retention.md`](politique-retention.md).

## Droits des personnes

En l'absence de comptes nominatifs, l'exercice des droits (accès, rectification,
effacement) s'opère par action directe sur les notebooks/sources concernés
(suppression possible via l'interface et l'API). Une procédure formelle devra être
définie si Dīwān est ouvert à des utilisateurs externes.
