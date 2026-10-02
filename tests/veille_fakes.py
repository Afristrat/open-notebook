"""Faux suivi en mémoire du podcast de veille: les tests n'ont besoin ni de base ni de réseau.

Même contrat que open_notebook/veille/runs.py; `install` remplace ses fonctions pour la durée
du test, de sorte que pipeline.py, reconcile.py et le routeur travaillent sur ce faux.
"""

import time
from typing import Any, Dict, List, Optional

from open_notebook.veille import runs


class FakeStore:
    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self._seq = 0

    def add(self, ref: str = "veille-2026-10-02", revision: str = "rev1", **fields: Any) -> Dict[str, Any]:
        self._seq += 1
        row_id = f"veille_run:{self._seq}"
        row = {
            "id": row_id, "ref": ref, "revision": revision, "statut": "accepte", "tentatives": 0,
            "rappel_statut": "a_envoyer", "rappel_tentatives": 0, "reprises": 0,
            "echeance": time.time() + runs.deadline_seconds(),
        }
        row.update(fields)
        self.rows[row_id] = row
        return dict(row)

    def by_ref(self, ref: str) -> Dict[str, Any]:
        return next(r for r in self.rows.values() if r["ref"] == ref)

    async def get_run(self, ref: str) -> Optional[Dict[str, Any]]:
        row = next((r for r in self.rows.values() if r["ref"] == ref), None)
        return dict(row) if row else None

    async def create_run(self, ref, revision, titre, date_publication, page_url) -> Dict[str, Any]:
        return self.add(ref, revision)

    async def relaunch_run(self, run_id, revision, titre, date_publication, page_url) -> None:
        row = self.rows[run_id]
        for key in ("etape", "job", "episode", "audio_url", "duree_s", "erreur", "rapport", "battement"):
            row.pop(key, None)
        row.update(
            revision=revision, statut="accepte", tentatives=0, rappel_statut="a_envoyer",
            rappel_tentatives=0, reprises=0, echeance=time.time() + runs.deadline_seconds(),
        )

    async def update_run(self, run_id: str, **fields: Any) -> None:
        for key, value in fields.items():
            if value is None:
                self.rows[run_id].pop(key, None)
            else:
                self.rows[run_id][key] = value

    async def list_active(self) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.rows.values() if r["statut"] in runs.ACTIVE]

    async def list_pending_callbacks(self) -> List[Dict[str, Any]]:
        return [
            dict(r) for r in self.rows.values()
            if r["statut"] in runs.TERMINAL and r["rappel_statut"] == "a_envoyer"
        ]

    def install(self, monkeypatch) -> "FakeStore":
        for name in (
            "get_run", "create_run", "relaunch_run", "update_run", "list_active",
            "list_pending_callbacks",
        ):
            monkeypatch.setattr(runs, name, getattr(self, name))
        return self
