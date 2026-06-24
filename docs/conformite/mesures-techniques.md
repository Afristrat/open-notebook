# Mesures techniques et organisationnelles — Dīwān

> État **réel et vérifié** au 24/06/2026 (audit serveur), suivi des recommandations.
> L'objectif est la transparence : les écarts sont documentés, pas masqués.

## 1. Chiffrement

### Existant
- **Identifiants des fournisseurs d'IA** : chiffrés en base via Fernet
  (`OPEN_NOTEBOOK_ENCRYPTION_KEY` défini et vérifié). Les clés API ne sont jamais
  renvoyées en clair par l'API (seules des métadonnées le sont).
- **Transport** : accès public via tunnel Cloudflare (HTTPS) → chiffrement en
  transit pour les échanges externes.

### Écarts
- **Contenu utilisateur non chiffré au repos** : sources, notes, transcriptions,
  podcasts et la base SurrealDB sont stockés en clair dans les volumes.
- **Disque hôte non chiffré** : aucun chiffrement de volume (LUKS/dm‑crypt) détecté.
  Un accès physique ou root au serveur donne accès au contenu.

### Recommandations
1. Chiffrer le disque hôte (LUKS) ou a minima le volume de données Dīwān.
2. Évaluer un chiffrement applicatif du contenu sensible si Dīwān héberge des
   données de tiers.
3. Conserver `OPEN_NOTEBOOK_ENCRYPTION_KEY` hors dépôt (déjà le cas : coffre DPAPI).

## 2. Contrôle d'accès

### Écart majeur
- **Authentification désactivée** : `OPEN_NOTEBOOK_PASSWORD` non défini → l'API et
  l'interface sont accessibles sans authentification. Le code prévoit un middleware
  par mot de passe, mais il est inactif.

### Recommandations
1. **Définir `OPEN_NOTEBOOK_PASSWORD`** (mot de passe robuste, dans le coffre)
   avant tout traitement de données réelles de tiers.
2. Pour un usage multi‑utilisateurs réel, remplacer le mot de passe partagé par
   une authentification individuelle (OAuth/JWT) — cf. recommandation amont du
   projet open‑notebook.
3. Restreindre l'exposition réseau au strict nécessaire.

## 3. Transferts vers des tiers

- **Anthropic (Claude/Opus)** : modèle de conversation et de transformation par
  défaut → le contenu correspondant est transmis aux États‑Unis.
- **Souverain (local)** : embeddings (Ollama), synthèse vocale (VoxCPM2),
  transcription (Speaches) restent sur le serveur.

### Recommandations
1. Informer les personnes concernées du recours à Anthropic (mention dans la
   politique de confidentialité).
2. Offrir/option : un modèle de chat **local** pour les traitements sensibles,
   afin d'éviter tout transfert hors périmètre souverain.

## 4. Sauvegardes et continuité

- Volumes de données **persistants** (vérifié `docker inspect`) : survivent aux
  redéploiements.
- **À documenter/mettre en place** : politique de sauvegarde régulière et testée
  de la base et des fichiers Dīwān (la persistance ≠ sauvegarde : une corruption
  ou une suppression se propagerait au volume).

## 5. Journalisation

- Journaux applicatifs via Loguru (techniques). Pas de journal d'accès nominatif
  (authentification désactivée).
- Recommandation : journaliser les accès une fois l'authentification activée, avec
  une durée de conservation maîtrisée.

## 6. Minimisation et exactitude

- Le système n'impose pas de collecte : l'utilisateur choisit ce qu'il téléverse.
- Recommandation : sensibiliser les utilisateurs à ne pas téléverser de données
  personnelles inutiles, et appliquer les durées de `politique-retention.md`.

## Tableau de bord des écarts

| Écart | Gravité | Action | Statut |
|---|---|---|---|
| Authentification désactivée | Élevée | Définir `OPEN_NOTEBOOK_PASSWORD` | À faire |
| Contenu non chiffré au repos | Moyenne‑élevée | Chiffrer disque/volume | À faire |
| Disque hôte en clair | Moyenne | LUKS | À faire |
| Transfert Anthropic non encadré | Moyenne | Informer + option modèle local | À faire |
| Sauvegardes non documentées | Moyenne | Définir et tester | À faire |
