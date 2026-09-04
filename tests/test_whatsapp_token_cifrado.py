"""O token do WhatsApp é cifrado com uma chave PRÓPRIA, e sem queda para a do pagamento.

É a armadilha 32 aplicada a segredo em repouso: `ADMIN_AUTH_SECRET` caía em
`CUSTOMER_AUTH_SECRET` quando vazia, e o resultado era que vazar o segredo do
app do cliente valia o painel de todo restaurante. A queda entre segredos é o
furo — não a ausência dela.

Aqui os dois valores cifrados são credenciais de TERCEIROS diferentes (a conta
do lojista no Mercado Pago e a Business Manager dele na Meta). Rotacionar uma
por causa de um incidente do gateway não pode obrigar a recifrar a outra no
mesmo minuto, e uma chave vazada não pode abrir as duas.

O que estes testes travam é o que não dá erro se alguém "economizar uma
variável": com a do WhatsApp AUSENTE e a do pagamento preenchida, a resposta
certa é levantar dizendo qual falta — e não cifrar com a que sobrou.
"""

import pytest
from cryptography.fernet import Fernet

from src.core.config import settings
from src.utils.crypto import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
    decrypt_secret,
    decrypt_whatsapp_token,
    encrypt_secret,
    encrypt_whatsapp_token,
)


CHAVE_DO_WHATSAPP = Fernet.generate_key().decode()
CHAVE_DO_PAGAMENTO = Fernet.generate_key().decode()

TOKEN = "EAAG-token-de-sistema-do-junior"


@pytest.fixture
def as_duas_chaves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", CHAVE_DO_WHATSAPP)
    monkeypatch.setattr(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", CHAVE_DO_PAGAMENTO)


class TestOTokenVaiEVoltaCifrado:
    def test_ida_e_volta(self, as_duas_chaves: None) -> None:
        cifrado = encrypt_whatsapp_token(TOKEN)

        assert cifrado != TOKEN
        assert decrypt_whatsapp_token(cifrado) == TOKEN


class TestAsDuasChavesNaoSeSubstituem:
    def test_sem_a_do_whatsapp_nao_cai_na_do_pagamento(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", None)
        monkeypatch.setattr(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", CHAVE_DO_PAGAMENTO)

        with pytest.raises(CredentialEncryptionNotConfiguredError) as erro:
            encrypt_whatsapp_token(TOKEN)

        assert "WHATSAPP_TOKEN_ENCRYPTION_KEY" in str(erro.value)

    def test_com_a_do_whatsapp_a_mesma_chamada_nao_levanta(self, as_duas_chaves: None) -> None:
        """O par do teste acima: sem ele, o `raises` passaria por outro motivo."""
        assert encrypt_whatsapp_token(TOKEN)

    def test_o_token_do_whatsapp_nao_decifra_com_a_chave_do_pagamento(
        self, as_duas_chaves: None
    ) -> None:
        cifrado = encrypt_whatsapp_token(TOKEN)

        with pytest.raises(CredentialDecryptionError):
            decrypt_secret(cifrado)

    def test_a_credencial_de_pagamento_nao_decifra_com_a_chave_do_whatsapp(
        self, as_duas_chaves: None
    ) -> None:
        cifrado = encrypt_secret("APP_USR-access-token")

        with pytest.raises(CredentialDecryptionError):
            decrypt_whatsapp_token(cifrado)
