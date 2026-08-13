# Contrat Diwan, fournisseur documentaire pour Qalem, version 1

Diwan expose a Qalem une facade documentaire versionnee. Qalem consomme cette
API et rien d'autre : pas de code partage, pas de base commune, aucun acces
direct a SurrealDB, aucun vecteur transmis.

- URL de base : `https://diwan.ai-mpower.com`
- Prefixe du contrat : `/api/v1/consumers/qalem`
- Version du contrat : `1.0`, renvoyee dans le champ `contractVersion` de
  chaque reponse

## 1. Ce que Diwan garde pour lui

Diwan reste responsable de la reception des sources, de leur conservation, de
l'extraction, de la reconnaissance optique de caracteres lorsqu'elle est
necessaire, du decoupage, des embeddings, de l'indexation, de la recherche et
de la restitution de passages verifiables.

Ne sortent jamais de Diwan : les vecteurs, le texte integral des sources, les
fichiers d'origine, et l'existence meme des documents des autres organisations.

## 2. Flux general

```
Qalem                          Diwan
  |                              |
  |  POST /ingestions  (202)     |  cree corpus + versions + job durable
  |----------------------------->|  soumet une tache par source
  |                              |
  |  GET /ingestions/{jobId}     |  extraction -> decoupage -> embeddings
  |<---------------------------->|  ready seulement si tout a reussi
  |                              |
  |  POST /retrieve              |  recherche hybride, liste blanche imposee
  |<---------------------------->|  passages + provenance verifiable
  |                              |
  |  POST /alignment             |  couverture, manques, contradictions
  |<---------------------------->|  chaque conclusion cite des chunkId
  |                              |
  |  DELETE /corpora/{corpusId}  |  revoque l'acces sans detruire les sources
  |----------------------------->|  partagees avec un autre corpus
```

## 3. Authentification interservice

Qalem presente un jeton dedie, distinct du mot de passe global historique de
Diwan :

```
Authorization: Bearer <jeton de service Qalem>
```

Diwan ne stocke jamais ce jeton. La configuration ne contient que des
empreintes SHA-256, sous la forme `consommateur:organisation:empreinte`. Un
jeton presente est hache puis compare en temps constant.

Consequences :

- la valeur du jeton n'apparait ni dans le code, ni dans l'environnement du
  conteneur, ni dans les journaux, ni dans ce document ;
- l'organisation autorisee est **derivee du jeton**. Un `organizationId`
  transmis dans le corps d'une requete n'est jamais une preuve d'autorisation ;
- deux empreintes peuvent coexister pour une meme organisation, ce qui permet
  une rotation sans interruption de service.

Codes de retour :

- `401` si le jeton est absent, malforme ou inconnu ;
- `403` si le jeton est valide mais vise un perimetre qui n'est pas le sien.

Nom de la variable a injecter cote Diwan : `DIWAN_CONSUMER_TOKENS`. Cote Qalem,
le jeton doit venir du coffre, jamais du code.

## 4. Endpoints

### 4.1 Importer des sources

`POST /api/v1/consumers/qalem/ingestions`, corps `multipart/form-data`.

| Champ | Type | Role |
|---|---|---|
| `files` | fichiers | un ou plusieurs documents |
| `urls` | texte, repetable | adresses a ingerer |
| `texts` | texte, repetable | contenus fournis directement |
| `titles` | texte, repetable | titres facultatifs, alignes sur `files` |
| `corpusId` | texte | corpus existant, sinon un corpus est cree |
| `corpusName` | texte | nom du corpus a creer |
| `idempotencyKey` | texte | obligatoire, egalement accepte en en-tete `Idempotency-Key` |

Reponse `202` :

```json
{
  "contractVersion": "1.0",
  "requestId": "5f7c...",
  "jobId": "ingestion_job:7x2k...",
  "corpusId": "corpus:9a1b...",
  "status": "queued",
  "submittedSources": 3,
  "pollAfterSeconds": 30
}
```

Regles appliquees :

- une meme cle d'idempotence ne cree jamais deux imports, elle renvoie le job
  existant ;
- le type de chaque fichier est determine par son **contenu**, puis confronte a
  son extension. Un fichier nomme `.pdf` qui n'en est pas un est refuse ;
- une empreinte SHA-256 est calculee sur le fichier d'origine, qui est conserve
  dans le volume persistant de Diwan ;
- un nouvel import ne remplace jamais une version precedente ;
- une source n'est `ready` que lorsque extraction **et** embeddings ont reussi ;
- l'echec d'une source ne masque pas l'etat des autres sources du meme lot.

### 4.2 Suivre un import

`GET /api/v1/consumers/qalem/ingestions/{jobId}`

```json
{
  "contractVersion": "1.0",
  "requestId": "5f7c...",
  "jobId": "ingestion_job:7x2k...",
  "corpusId": "corpus:9a1b...",
  "status": "queued|extracting|chunking|embedding|ready|partially_failed|failed",
  "progress": 72,
  "sources": [
    {
      "sourceId": "source:...",
      "sourceVersion": "source_version:...",
      "originalName": "document.pdf",
      "status": "ready",
      "checksumSha256": "sha256:...",
      "pages": 32,
      "chunks": 84,
      "errorCode": null,
      "errorMessage": null
    }
  ],
  "pollAfterSeconds": 30
}
```

L'etat vit en base, pas en memoire : un job survit au redemarrage du service et
le suivi reprend apres une reconnexion. Le serveur reste la source de verite.

### 4.3 Bibliotheque documentaire

`GET /api/v1/consumers/qalem/sources`

Filtres : `corpusId`, `status`, `query`, `mediaType`, `page`, `pageSize`.

La reponse ne contient que les sources autorisees pour l'organisation deduite
du jeton, et **jamais** le texte integral.

### 4.4 Manifeste de sources

`POST /api/v1/consumers/qalem/sources/manifest`, corps `{"sourceIds": [...]}`.

Renvoie pour chaque source sa version, son empreinte, son etat, le parseur et
le modele d'embedding utilises. Toute source hors perimetre fait echouer la
requete avec `SOURCE_NOT_AUTHORIZED`.

### 4.5 Recherche documentaire

`POST /api/v1/consumers/qalem/retrieve`

```json
{
  "corpusId": "corpus:9a1b...",
  "sourceIds": ["source:a", "source:b"],
  "query": "Comment la source decrit-elle le SIPOC ?",
  "limit": 12,
  "minimumScore": 0.35,
  "searchMode": "hybrid"
}
```

`sourceIds` est **obligatoire** : cette facade n'expose aucune recherche
globale. Le filtre est applique dans la requete SurrealDB, jamais apres coup.

Reponse :

```json
{
  "contractVersion": "1.0",
  "requestId": "...",
  "status": "ok",
  "query": "...",
  "evidence": [
    {
      "chunkId": "source_embedding:...",
      "sourceId": "source:a",
      "sourceVersion": "source_version:a1",
      "sourceTitle": "Titre",
      "pageNumber": 12,
      "sectionTitle": "SIPOC",
      "content": "Passage pertinent...",
      "score": 0.87,
      "contentHash": "...",
      "sourceChecksumSha256": "sha256:..."
    }
  ]
}
```

Si aucun passage n'atteint le seuil, `status` vaut `insufficient_evidence` et
`evidence` est vide. Une absence de preuve n'est jamais comblee par du contenu
invente ni par une recherche Web.

### 4.6 Controle d'alignement

`POST /api/v1/consumers/qalem/alignment`, corps `corpusId`, `sourceIds`,
`authorRequest`, `expectedLanguage`.

Renvoie `status` parmi `aligned`, `partially_aligned`, `conflicting`,
`insufficient_evidence`, un `coverageScore`, les exigences couvertes avec leurs
`evidenceChunkIds`, les exigences manquantes, les contradictions, une
`recommendedAction` et une demande reformulee directement exploitable.

Garanties : chaque exigence couverte cite au moins un bloc reellement retourne,
tout identifiant de bloc invente par le modele est supprime avant reponse, et
en cas de contradiction la recommandation est toujours `author_arbitration`.
Diwan ne tranche jamais a la place de l'auteur.

### 4.7 Contradictions entre sources

`POST /api/v1/consumers/qalem/conflicts`, corps `corpusId`, `sourceIds`.

Distingue la contradiction reelle, la difference de perimetre et la difference
de date. Renvoie `no_material_conflict` lorsqu'aucune contradiction
substantielle n'est etablie. Une contradiction exige au moins deux positions
etayees par des blocs distincts : un ecart de score vectoriel ne suffit jamais.

### 4.8 Revoquer un corpus

`DELETE /api/v1/consumers/qalem/corpora/{corpusId}`

Le corpus et ses rattachements sont marques revoques. Une source partagee avec
un autre corpus y reste intacte et consultable. Rien n'est supprime en silence.

## 5. Matrice des erreurs

Toutes les erreurs suivent la meme enveloppe :

```json
{
  "contractVersion": "1.0",
  "requestId": "...",
  "error": {
    "code": "SOURCE_NOT_AUTHORIZED",
    "message": "Cette source n'est pas accessible dans ce perimetre.",
    "retryable": false,
    "details": {}
  }
}
```

| Code | HTTP | Rejouable | Quand |
|---|---|---|---|
| `UNAUTHENTICATED` | 401 | non | jeton absent, malforme ou inconnu |
| `FORBIDDEN` | 403 | non | jeton valide, perimetre etranger |
| `CORPUS_NOT_FOUND` | 404 | non | corpus inconnu dans ce perimetre |
| `SOURCE_NOT_FOUND` | 404 | non | source inconnue dans ce perimetre |
| `SOURCE_NOT_AUTHORIZED` | 403 | non | source hors perimetre, ou inexistante |
| `SOURCE_NOT_READY` | 409 | oui | extraction ou vectorisation incomplete |
| `INVALID_SOURCE_TYPE` | 415 | non | contenu reel non accepte |
| `FILE_TOO_LARGE` | 413 | non | fichier au dela de la limite |
| `INGESTION_FAILED` | 500 | oui | echec d'import |
| `EXTRACTION_FAILED` | 500 | oui | extraction impossible |
| `OCR_FAILED` | 500 | oui | reconnaissance optique impossible |
| `EMBEDDING_FAILED` | 500 | oui | vectorisation impossible |
| `EMBEDDING_MODEL_UNAVAILABLE` | 503 | oui | modele indisponible |
| `EMBEDDING_DIMENSION_MISMATCH` | 409 | non | dimensions incompatibles, reindexation requise |
| `INSUFFICIENT_EVIDENCE` | 200 | non | aucun passage au dessus du seuil |
| `RATE_LIMITED` | 429 | oui | frequence depassee |
| `SERVICE_BUSY` | 503 | oui | service sature |
| `CONTRACT_VERSION_UNSUPPORTED` | 400 | non | version de contrat inconnue |
| `INVALID_REQUEST` | 422 | non | requete invalide, liste de sources absente comprise |

Absence de fuite d'existence : une source inexistante et une source appartenant
a une autre organisation rendent exactement la meme reponse.

## 6. Limites

| Limite | Valeur par defaut | Variable |
|---|---|---|
| Taille d'un fichier | 50 Mo | `DIWAN_CONSUMER_MAX_UPLOAD_MB` |
| Fichiers par import | 20 | `DIWAN_CONSUMER_MAX_FILES` |
| Requetes de lecture | 120 par minute et par identite | `DIWAN_CONSUMER_RATE_LIMIT` |
| Imports | 10 par minute et par identite | `DIWAN_CONSUMER_INGEST_RATE_LIMIT` |
| Extractions simultanees | 2 | `DIWAN_CONSUMER_EXTRACTION_CONCURRENCY` |
| Vectorisations simultanees | 2 | `DIWAN_CONSUMER_EMBEDDING_CONCURRENCY` |
| Passages par recherche | 50 au maximum | parametre `limit` |

Deux vectorisations simultanees d'une meme version sont impossibles : un verrou
par version serialise les traitements.

## 7. Rotation du jeton, sans interruption

1. Generer une nouvelle valeur aleatoire cote Qalem ou sur le serveur, sans la
   faire transiter par un canal de discussion.
2. Calculer son empreinte : `sha256sum < fichier`.
3. Ajouter cette empreinte a `DIWAN_CONSUMER_TOKENS`, **en conservant
   l'ancienne**, separees par une virgule.
4. Redeployer Diwan. Les deux jetons sont alors acceptes.
5. Basculer Qalem sur la nouvelle valeur.
6. Retirer l'ancienne empreinte, puis redeployer.

Aucune requete n'est refusee pendant l'operation.

## 8. Revocation d'un consommateur

Retirer son entree de `DIWAN_CONSUMER_TOKENS` puis redeployer suffit : le
consommateur ne s'authentifie plus. Les donnees restent intactes, et la
revocation d'un corpus (paragraphe 4.8) est une operation distincte, qui
n'efface rien.

## 9. Politique de versionnement

- Le contrat est versionne dans l'URL, sous `/v1`.
- Chaque reponse porte `contractVersion`.
- Un client peut transmettre `contractVersion` dans le corps : une valeur
  inconnue renvoie `CONTRACT_VERSION_UNSUPPORTED`.
- Pendant toute la duree de `v1`, les champs existants conservent leur nom et
  leur signification. Seuls des champs facultatifs peuvent etre ajoutes.
- Un changement incompatible ouvre `/v2`, et `v1` reste servi pendant la
  transition.
- Chaque reponse porte un `requestId`, egalement accepte en en-tete
  `X-Request-Id` pour correler les journaux des deux cotes.

## 10. Provenance et absence de derive

- Aucun contenu produit par Diwan n'est presente comme une source.
- Chaque analyse cite des `chunkId` reels.
- Chaque bloc appartient a une version immuable, identifiee par l'empreinte
  SHA-256 du document d'origine.
- Une nouvelle version ne modifie pas retroactivement les preuves deja citees :
  les anciens blocs restent rattaches a leur version.
- Un changement de modele d'embedding produit une nouvelle version d'index ;
  une recherche dont les dimensions ne correspondent pas echoue explicitement
  avec `EMBEDDING_DIMENSION_MISMATCH` au lieu de rendre un resultat vide
  trompeur.
- La recherche Web n'est jamais melangee au corpus documentaire. Elle demeure
  une responsabilite distincte, activee explicitement dans Qalem.

## 11. Observabilite

Sont journalises : `requestId`, identifiant de consommateur, identifiant
interne d'organisation, `corpusId`, `sourceId`, etat du job, duree, nombre de
blocs, code d'erreur, modele d'embedding.

Ne sont jamais journalises : le jeton, les fichiers, le texte integral des
sources, les passages retournes, les donnees personnelles extraites, les cles
des fournisseurs.

## 12. Guide de validation pour la session Qalem

1. Verifier le refus anonyme :
   `curl -s -o /dev/null -w "%{http_code}" https://diwan.ai-mpower.com/api/v1/consumers/qalem/sources`
   doit rendre `401`.
2. Verifier le refus d'un jeton invalide, avec un en-tete `Authorization`
   fantaisiste : `401`.
3. Avec le jeton du coffre, lister les sources : `200`, et la liste ne contient
   que les corpus de l'organisation du jeton.
4. Importer un document, recuperer le `jobId`, interroger le suivi apres trente
   secondes, jusqu'a `ready`.
5. Lancer une recherche limitee a une seule source, verifier qu'aucun passage
   ne provient d'une autre.
6. Demander une analyse d'alignement, verifier que chaque exigence couverte
   cite un `chunkId` present dans la reponse de recherche.
7. Tenter d'acceder a un `sourceId` inconnu : la reponse doit etre identique a
   celle obtenue pour une source appartenant a une autre organisation.
