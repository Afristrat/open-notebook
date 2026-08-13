"""Facade documentaire pour les consommateurs externes de Diwan.

Ce paquet contient tout ce qui est propre au contrat expose a des services
tiers (Qalem aujourd'hui, d'autres demain): authentification interservice,
perimetre documentaire, ingestion versionnee, recherche cloisonnee et analyse
d'alignement.

Rien ici ne modifie le comportement historique de Diwan. Les routes de la
facade vivent sous /api/v1/consumers/<nom> et n'ont aucun repli vers les
routes historiques.
"""

CONTRACT_VERSION = "1.0"
