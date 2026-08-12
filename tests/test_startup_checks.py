"""As checagens de boot que derrubam a API.

O que se testa aqui e a LISTA de erros, nao o logging: `validate_settings`
so junta o que `collect_configuration_errors` devolveu.
"""

import pytest

from src.core.config import settings
from src.core.startup_checks import (
    StartupConfigurationError,
    collect_configuration_errors,
    validate_settings,
)


def _settings_with(monkeypatch, **overrides):
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    return settings


class TestOsDoisSegredosDeAssinatura:
    """Lojista e cliente nao podem compartilhar chave.

    O caso concreto: com ADMIN_AUTH_SECRET ausente, o token do painel era
    assinado com CUSTOMER_AUTH_SECRET. Quem tivesse um dos dois segredos
    conseguiria forjar token do outro publico — so o campo `purpose`
    separava, e ele viaja dentro do proprio token forjado.
    """

    def test_valores_iguais_derrubam_o_boot(self, monkeypatch):
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET="o-mesmo-segredo-dos-dois-lados",
            CUSTOMER_AUTH_SECRET="o-mesmo-segredo-dos-dois-lados",
        )
        errors = collect_configuration_errors(settings)
        assert any("ADMIN_AUTH_SECRET" in error for error in errors)

    def test_espaco_em_branco_nao_disfarca_o_valor_repetido(self, monkeypatch):
        # Um ' ' no fim da linha do .env nao pode transformar "mesmo segredo"
        # em "segredos diferentes".
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET=" o-mesmo-segredo-dos-dois-lados ",
            CUSTOMER_AUTH_SECRET="o-mesmo-segredo-dos-dois-lados",
        )
        errors = collect_configuration_errors(settings)
        assert any("ADMIN_AUTH_SECRET" in error for error in errors)

    def test_valores_diferentes_passam(self, monkeypatch):
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET="segredo-do-painel-do-lojista",
            CUSTOMER_AUTH_SECRET="segredo-do-app-do-cliente",
        )
        errors = collect_configuration_errors(settings)
        assert not any("ADMIN_AUTH_SECRET" in error for error in errors)

    def test_validate_settings_levanta_e_nomeia_a_variavel(self, monkeypatch):
        _settings_with(
            monkeypatch,
            ADMIN_AUTH_SECRET="segredo-repetido",
            CUSTOMER_AUTH_SECRET="segredo-repetido",
        )
        with pytest.raises(StartupConfigurationError, match="ADMIN_AUTH_SECRET"):
            validate_settings(settings)
