# Politique de conservation et de purge — Dīwān

> Principe RGPD/CNDP : les données ne sont conservées que le temps nécessaire aux
> finalités. Les durées ci‑dessous sont des **valeurs proposées** à valider par
> Afrique Stratégie selon l'usage réel.

## Durées de conservation proposées

| Donnée | Finalité | Durée proposée | Justification |
|---|---|---|---|
| Sources téléversées + texte extrait | Recherche documentaire | Durée du projet / notebook actif, puis purge | Donnée de travail |
| Notes et insights | Production intellectuelle | Idem notebook | Donnée de travail |
| Sessions de chat | Historique de recherche | `[À DÉFINIR]` (ex. 12 mois glissants) | Limiter l'accumulation |
| Fichiers audio/vidéo + transcriptions | Exploitation du contenu | Idem source associée | Lié à la source |
| Podcasts générés (clips, audio, transcript) | Livrable | `[À DÉFINIR]` (ex. 12 mois ou jusqu'à suppression manuelle) | Livrable réutilisable |
| Embeddings vectoriels | Recherche sémantique | Tant que la source existe ; purgés à sa suppression | Dérivé de la source |
| Journaux techniques | Exploitation/débogage | `[À DÉFINIR]` (ex. 30–90 jours) | Minimisation |

> Ces durées sont des points de départ raisonnables. Elles doivent être arrêtées
> formellement (et, en usage public, communiquées dans la politique de
> confidentialité).

## Procédure de purge

### Suppression à l'initiative de l'utilisateur
- Suppression d'un **épisode de podcast** : `DELETE /api/podcasts/episodes/{id}`
  supprime l'enregistrement et le fichier audio associé.
- Suppression d'une **source / d'un notebook** : via l'interface et l'API (les
  embeddings liés sont supprimés avec la source).

> ⚠️ Vérification recommandée : confirmer que la suppression d'un épisode efface
> **l'intégralité** du dossier disque `episodes/<uuid>/` (outline, transcript,
> dossier `clips/`, dossier `audio/`) et pas uniquement le fichier audio final.
> Si ce n'est pas le cas, prévoir un nettoyage complet du dossier — point à
> traiter pour éviter des résidus (cohérent avec l'exigence « zéro trace
> résiduelle »).

### Purge programmée (à mettre en place)
- Aucune purge automatique n'est en place actuellement.
- Recommandation : tâche planifiée appliquant les durées ci‑dessus (suppression
  des sessions, podcasts et journaux au‑delà de la durée retenue), avec
  journalisation des purges.

## Sauvegardes

- Les sauvegardes éventuelles doivent suivre la même politique de durée et être
  purgées en cohérence (une donnée « effacée » ne doit pas survivre indéfiniment
  dans une sauvegarde).
- État actuel : pas de politique de sauvegarde Dīwān documentée (cf.
  `mesures-techniques.md`, section 4).
