# Conformité Dīwān — Socle commun (RGPD + CNDP)

> **Statut : socle de base, extensible.** Ce dossier constitue le socle commun de
> conformité de Dīwān, valable aussi bien pour un usage interne (Afrique Stratégie)
> que pour une future ouverture à des utilisateurs externes. Il documente l'état
> **réel et vérifié** du traitement des données, et non un modèle générique.
>
> ⚠️ Ce socle est un travail **technique et organisationnel**. Il ne remplace pas
> une validation juridique. Les éléments marqués `[À COMPLÉTER]` relèvent d'une
> décision d'Afrique Stratégie (identité du responsable de traitement, désignation
> d'un point de contact, durées définitives, etc.).

## Cadre réglementaire de référence

| Cadre | Référence | Portée |
|---|---|---|
| **RGPD** (Union européenne) | Règlement (UE) 2016/679, applicable depuis le 25 mai 2018 | Si des données de personnes situées dans l'UE sont traitées, ou en cas d'établissement dans l'UE |
| **Loi marocaine** | Loi n° 09-08 (dahir n° 1‑09‑15 du 18 février 2009), autorité : **CNDP** | Traitement de données personnelles au Maroc |

> Vérification à la date de rédaction (24/06/2026) : le RGPD et la loi 09‑08
> demeurent les textes de référence. Toute évolution réglementaire (notamment une
> éventuelle réforme de la loi marocaine) devra être revérifiée avant publication
> d'une politique destinée au public.

## Périmètre du produit

Dīwān est un assistant de recherche IA auto‑hébergé (fork d'open‑notebook). Il
permet de téléverser des contenus (PDF, audio, vidéo, pages web), de générer des
notes, de discuter avec des modèles d'IA, de faire de la recherche sémantique et
de produire des podcasts. **Hébergement souverain** : serveur sous le contrôle
direct d'Afrique Stratégie (pas de cloud tiers pour le stockage).

## Contenu du dossier

| Document | Objet |
|---|---|
| [`registre-traitements.md`](registre-traitements.md) | Registre des activités de traitement (esprit art. 30 RGPD) — **factuel** |
| [`mesures-techniques.md`](mesures-techniques.md) | Mesures de sécurité réelles + **écarts identifiés** et recommandations |
| [`politique-retention.md`](politique-retention.md) | Durées de conservation et procédure de purge |
| [`politique-confidentialite.md`](politique-confidentialite.md) | Politique de base, extensible vers une version publique |

## Synthèse des écarts à traiter en priorité

Identifiés lors de l'audit technique (cf. `mesures-techniques.md`) :

1. **Authentification désactivée** — l'API est actuellement accessible sans mot
   de passe (`OPEN_NOTEBOOK_PASSWORD` non défini). À activer avant toute
   exposition à des données réelles de tiers.
2. **Contenu utilisateur non chiffré au repos** — seuls les identifiants des
   fournisseurs d'IA sont chiffrés (Fernet). Le contenu (sources, notes,
   transcriptions) et le disque hôte sont en clair.
3. **Transfert hors UE/Maroc** — le modèle de conversation par défaut est Claude
   (Anthropic, États‑Unis). Le contenu envoyé au chat quitte le périmètre
   souverain. (La synthèse/transcription vocale et les embeddings, eux, sont
   locaux.)

Ces points ne sont pas des défauts cachés : ils sont documentés ici pour être
arbitrés et corrigés.
