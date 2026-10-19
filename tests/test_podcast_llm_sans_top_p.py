"""
Le plan et le script du podcast ne doivent plus envoyer `top_p`.

Esperanto impose `top_p = 0.9` par défaut. Le groupe LiteLLM « cheap » répartit
les appels entre fournisseurs et Moonshot refuse tout autre `top_p` que 0.95
(400 « invalid top_p: only 0.95 is allowed for this model »). La résolution des
modèles de langage pose donc `top_p: None`, que le fournisseur retire de l'appel.
Les modèles de voix, eux, gardent leur configuration inchangée.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from esperanto.factory import AIFactory

from open_notebook.podcasts.models import EpisodeProfile, _resolve_model_config

CREDENTIAL_CONFIG = {"api_key": "cle-factice", "base_url": "http://127.0.0.1:1/v1"}


def fake_model():
    credential = MagicMock()
    credential.to_esperanto_config.return_value = dict(CREDENTIAL_CONFIG)
    model = MagicMock()
    model.credential = "credential:x"
    model.provider = "openai_compatible"
    model.name = "cheap"
    model.get_credential_obj = AsyncMock(return_value=credential)
    return model


async def resolve(**kwargs):
    with patch(
        "open_notebook.ai.models.Model.get", AsyncMock(return_value=fake_model())
    ):
        return await _resolve_model_config("model:x", **kwargs)


def langchain_top_p(provider, name, config):
    return AIFactory.create_language(provider, name, config=config).to_langchain().top_p


@pytest.mark.asyncio
async def test_modele_de_langage_sans_top_p():
    provider, name, config = await resolve(max_tokens=4000, language=True)
    assert "top_p" in config and config["top_p"] is None
    assert config["max_tokens"] == 4000
    assert langchain_top_p(provider, name, config) is None


@pytest.mark.asyncio
async def test_defaut_esperanto_inchange_hors_langage():
    provider, name, config = await resolve()
    assert "top_p" not in config
    assert langchain_top_p(provider, name, config) == 0.9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["resolve_outline_config", "resolve_transcript_config"]
)
async def test_plan_et_script_du_profil_sans_top_p(method):
    profile = EpisodeProfile(
        name="p",
        default_briefing="b",
        outline_llm="model:o",
        transcript_llm="model:t",
        max_tokens=4000,
    )
    with patch(
        "open_notebook.ai.models.Model.get", AsyncMock(return_value=fake_model())
    ):
        provider, name, config = await getattr(profile, method)()
    assert langchain_top_p(provider, name, config) is None
