# ADR-009 : Podcast quotidien de veille demandé par Saqr

Statut : accepté (02/10/2026). Auteurs : session Dīwān, sur demande d'Amine.

## Contexte

Saqr publie chaque jour une veille (blog, page Rami). Amine veut qu'un podcast de cette veille soit
produit par Dīwān et que son lien revienne dans le blog et dans la publication. Fenêtre de Saqr : veille
envoyée avant 08 h 30 UTC, lien attendu avant 09 h 45 UTC ; passé ce délai, Saqr publie sans podcast.

Les essais manuels des 30/09 au 02/10 ont montré trois risques :

1. le moteur de voix échoue au milieu d'une génération (5 essais sur 6 le 30/09) ;
2. le texte généré peut contenir un chiffre ou un terme commercial absent des sources
   (contrôle de contenu, `open_notebook/podcasts/content_guard.py`) ;
3. un chiffre de la veille de Saqr peut venir du CORPS d'un article (arXiv, article lié à un post X)
   alors que Dīwān n'avait lu que la page de résumé.

## Décision

Un consommateur « saqr » calqué sur la façade Qalem.

- **Récepteur** : `POST /api/v1/consumers/saqr/veille-podcast` répond 202 aussitôt ; suivi en lecture
  `GET …/veille-podcast/{ref}`. Corps `{ref, revision, titre, date_publication, url}`.
  Idempotent sur (`ref`, `revision`). Même `ref` avec une autre `revision` : 409
  `VEILLE_REVISION_CONFLICT`, sauf si l'essai précédent a échoué (la demande relance alors la production).
- **Jeton Saqr vers Dīwān** : empreinte SHA-256 dans `DIWAN_CONSUMER_TOKENS` (`saqr:<organisation>:<empreinte>`).
  Dīwān ne détient jamais le jeton. **Jeton Dīwān vers Saqr** : second jeton DISTINCT
  (`DIWAN_SAQR_PODCAST_TOKEN`), envoyé en `Authorization: Bearer`. Lecture de la pièce chez Saqr :
  `SAQR_DIWAN_SOURCES_TOKEN`.
- **Suivi durable** : table `veille_run` (migration 29), une ligne par veille. C'est la seule mémoire commune
  entre l'API (qui reçoit) et le worker (qui produit), deux processus distincts.
- **Production** (commande `saqr_veille_podcast`, `open_notebook/veille/pipeline.py`) : lecture de la pièce
  publiée, lecture des sources PAR Dīwān (jamais sur la foi de Saqr), plafond de 10 % de sources
  inexploitables, extraits d'articles complets portant les nombres de la veille, génération avec le profil
  « Veille », relances, vérification du MP3, rappel.
- **Relances** : refus du contrôle de contenu = nouvelle transcription (avant toute voix) ; panne de voix = reprise
  de l'épisode au clip (`resume_episode_id`) ; au plus `SAQR_VEILLE_MAX_ATTEMPTS` essais (5), dans le délai de
  `SAQR_VEILLE_DEADLINE_MINUTES` (70) depuis la réception.
- **Jamais le silence** : toute issue produit un rappel `{ref, revision, statut: "pret"|"echec", audio_url,
  page_url, duree_s, erreur}`. Un rappel non délivré est rejoué ; une production dont le battement s'arrête
  (worker redémarré) est relancée au plus 3 fois ; une échéance dépassée conclut en « echec » annoncé
  (`open_notebook/veille/reconcile.py`, boucle de fond de l'API).
- **« pret » seulement après lecture du fichier** : en-tête MP3, codec `mp3` selon `ffprobe`, durée mesurée
  (`open_notebook/veille/audio_check.py`). Un WAV étiqueté `audio/mpeg` est refusé.
- **Adresse audio** : `{DIWAN_PUBLIC_URL}/api/podcasts/episodes/{id}/audio` (HTTPS, lecture par plages,
  `HEAD` non supporté).

## Mise en service (02/10/2026)

Variables de l'application Coolify (`ohir87jvt32284sh6wfwhhz2`), toutes avec « disponible au build » à faux :

| Variable | Rôle | Origine |
|---|---|---|
| `DIWAN_CONSUMER_TOKENS` | empreintes des consommateurs : `qalem:<org>:<empreinte>,saqr:saqr:<empreinte>` | coffre d'Amine (valeur d'origine) + empreinte SHA-256 de `SAQR_DIWAN_API_TOKEN` |
| `SAQR_DIWAN_SOURCES_TOKEN` | Dīwān lit la veille chez Saqr | coffre |
| `DIWAN_SAQR_PODCAST_TOKEN` | Dīwān rappelle Saqr (48 caractères) | coffre, identique au Vault de Saqr |

Deux pièges rencontrés, à connaître avant toute opération Coolify sur cette application :

1. **L'API Coolify 4.3.23 ne renvoie pas le champ `value`** des variables. Une valeur « lue » est donc vide, et un `PATCH` calculé à partir d'elle écrase la variable (incident du 02/10 : `DIWAN_CONSUMER_TOKENS` a été écrasée par la seule entrée `saqr`, rattrapée avant tout redémarrage grâce à la valeur d'origine conservée au coffre). Toujours écrire une valeur construite depuis le coffre, jamais depuis une lecture Coolify.
2. **La configuration de l'application épingle `git_commit_sha`** sur un ancien commit (`fff6df1`). Un déploiement ou un redémarrage demandé par l'API construit ce commit, pas la tête de la branche ; seul un déploiement par le webhook (poussée sur `deploy-coolify`) construit le commit poussé. Pour appliquer une variable, pousser un commit, ne pas passer par l'API de déploiement.

## Conséquences et limites connues

- Le contrôle de contenu reste le garde-fou factuel : il peut refuser toutes les relances, et l'issue est alors
  un « echec » annoncé avec sa raison. Il ne détecte PAS une déformation de portée (voir
  `docs/diwan-acces-veille-sources.md` de Saqr).
- **Pas de reprise d'agence** (ex. une dépêche Reuters bloquée retrouvée chez un autre éditeur) : OpenSERP
  n'écoute aujourd'hui que sur le port local de l'hôte (127.0.0.1:7001, non vérifié depuis le conteneur), donc
  cette reprise n'est pas portée dans le dépôt. Une source bloquée compte comme altérée.
- **Posts X** : X ne se laisse pas lire de façon fiable (le 02/10, 71 caractères lus contre 228). Pour les seuls
  posts X, quand Dīwān n'obtient pas plus que le titre relevé par Saqr, ce titre (qui est le texte du post tel que
  Saqr l'a capturé) sert de texte de la source, et le rapport le dit (« texte du post relevé par Saqr »). Sans ce
  repli, les chiffres de la veille tirés du post n'ont plus de source et le contrôle refuse tout l'épisode (essai de
  bout en bout du 02/10, 5 tentatives refusées). Les chiffres tirés de l'ARTICLE que le post annonce (Saqr ne
  renseigne pas `article_url` pour un post X) viennent de la résolution automatique : recherche sur l'API publique
  d'arXiv à partir du texte du post (trois termes au plus, en « AND » : le même jeu en « OR » noyait l'article, mesuré
  le 02/10), puis l'article n'est retenu que si le post retrouve au moins 60 % de ses mots distinctifs dans son titre
  et son résumé (`ingest.pick_arxiv_match`) ; jamais un article au hasard. Le PDF complet est alors lu comme celui
  d'une source arXiv. Limites : un article hors arXiv n'est pas retrouvé, et si arXiv est injoignable la source reste
  lue comme avant ; dans les deux cas le contrôle refuse un chiffre sans source, c'est voulu. La correction à la
  cause reste chez Saqr : renseigner `article_url` pour les posts X.
- **`page_url`** : la page qui renvoie vers l'ensemble des plateformes n'existe pas encore chez Dīwān ;
  `SAQR_VEILLE_PAGE_URL_TEMPLATE` (vide par défaut) la renseignera. Le rappel envoie `null` d'ici là.
- Les adresses audio sont publiques et non signées, comme le reste de `/api/podcasts/*` ; elles changeront si
  l'authentification globale de Dīwān est activée.
- Durée : le profil « Veille » vise 8 à 12 minutes dans son briefing ; le contrôle accepte de 1 100 à 2 400 mots
  (environ 8 à 14 minutes), plafond relevé le 03/10 (monologue de 2 027 à 2 193 mots refusé 5 fois sur 5 à 2 000).
  Passer à 13–30 minutes est une décision de produit, qui touche le briefing du profil et `word_range` du contrôle.
- Nombres : un montant décimal de la source (« $777.72 ») peut être dit arrondi (777 ou 778), ce n'est pas un chiffre
  sans source ; tout autre nombre absent des sources reste refusé.
- Voix : inchangées (profil de voix `veille`, voix Higgs). La demande de Saqr d'utiliser les voix de Qalem est
  traitée à part.

## Renvoi manuel à Saqr (11/10/2026)

Un épisode « Veille Saqr AAAA-MM-JJ » produit hors du circuit de l'API (studio, relance à la main) n'annonce jamais
« pret » à Saqr, qui affiche alors « echec » alors que l'audio existe (08/10 et 11/10). Le bouton « Renvoyer à Saqr »
de la carte d'épisode appelle `POST /api/podcasts/episodes/{id}/renvoyer-saqr` (`open_notebook/veille/renvoi.py`).

- **Révision** : celle de la demande de Saqr, lue dans `veille_run`, jamais déduite d'un texte. Un épisode créé avant
  cette demande (marge de 120 s, comme `episodes_of_run`) correspond à une autre révision et est refusé (409).
- **Refus** (sans rappel) : nom autre que « Veille Saqr AAAA-MM-JJ » (422), veille jamais demandée (404), production
  en cours (409), épisode sans audio ou MP3 invalide (409).
- **Rappel** : le même corps que le rappel automatique (`ref`, `revision`, `statut: "pret"`, `audio_url`, `page_url`,
  `duree_s`), un seul essai. Saqr remplace l'épisode précédent de la même révision, et un « echec » tardif n'écrase
  jamais un « pret » (contrat confirmé par sa session le 11/10).
- **Refus de Saqr** (400, 401, 403, 404) : la ligne de suivi retrouve son état d'avant, la raison de Saqr est rendue
  (502). **Saqr injoignable** : l'épisode reste rattaché, le rappel reste « a_envoyer » et la boucle de reprise le
  rejoue (503).
- Comme le reste de `/api/podcasts/*`, la route n'est pas protégée tant que l'authentification globale est désactivée.
