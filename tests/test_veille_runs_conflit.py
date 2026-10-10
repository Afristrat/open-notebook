"""update_run rejoue un conflit d'écriture SurrealDB (veille du 09/10, requête de 07:55:01).

Ces tests utilisent le VRAI runs.update_run (les autres tests le remplacent par FakeStore) et
simulent seulement repo_query.
"""

import pytest

from open_notebook.veille import runs

CONFLICT = RuntimeError(
    "The query was not executed due to a failed transaction. Failed to commit transaction "
    "due to a read or write conflict. This transaction can be retried"
)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(runs, "UPDATE_BACKOFF_SECONDS", 0)


def _query_failing(monkeypatch, failures: int, error: Exception = CONFLICT):
    calls = []

    async def fake_query(query, params=None):
        calls.append(query)
        if len(calls) <= failures:
            raise error
        return [{"id": "veille_run:1"}]

    monkeypatch.setattr(runs, "repo_query", fake_query)
    return calls


@pytest.mark.asyncio
async def test_conflict_is_retried_then_succeeds(monkeypatch):
    calls = _query_failing(monkeypatch, failures=2)
    await runs.update_run("veille_run:1", statut="en_cours", etape="recuperation")
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_persistent_conflict_raises_after_the_attempt_limit(monkeypatch):
    calls = _query_failing(monkeypatch, failures=99)
    with pytest.raises(RuntimeError, match="read or write conflict"):
        await runs.update_run("veille_run:1", statut="en_cours")
    assert len(calls) == runs.UPDATE_ATTEMPTS


@pytest.mark.asyncio
async def test_other_errors_are_not_retried(monkeypatch):
    calls = _query_failing(monkeypatch, failures=99, error=RuntimeError("connexion perdue"))
    with pytest.raises(RuntimeError, match="connexion perdue"):
        await runs.update_run("veille_run:1", statut="en_cours")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_no_fields_means_no_query(monkeypatch):
    calls = _query_failing(monkeypatch, failures=0)
    await runs.update_run("veille_run:1")
    assert calls == []
