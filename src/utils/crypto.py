"""Cifra em repouso de segredo de terceiro (access token de gateway, token da Meta).

Fernet e nao um hash: senha e codigo de verificacao a gente so precisa
CONFERIR (hash_password, hash_verification_code em security.py), nunca
devolver o valor original. O access token do restaurante e o oposto — a
aplicacao precisa mandar o valor original para o Mercado Pago a cada
cobranca, entao tem que dar para decifrar. Fernet e simetrico e autenticado
(AES-128-CBC + HMAC): decifra so quem tem a chave, e um bloco alterado no
banco quebra a verificacao em vez de decifrar em lixo.

A chave mora no .env, nunca no banco — cifrar a coluna e inutil se a chave
fica ao lado dela.

## SAO DUAS CHAVES, e a ausencia de queda de uma para a outra e o desenho

    PAYMENT_CREDENTIALS_ENCRYPTION_KEY   access token e webhook secret do
                                         Mercado Pago do restaurante
    WHATSAPP_TOKEN_ENCRYPTION_KEY        access token da Business Manager do
                                         lojista na Meta

Sao credenciais de TERCEIROS DIFERENTES. Uma chave so seria mais comoda e
teria o custo da armadilha 32: rotacionar por causa de um incidente do
gateway obrigaria a recifrar os tokens do WhatsApp no mesmo minuto, e uma
chave vazada abriria os dois lados. Por isso `_fernet_do_whatsapp` NAO le a
variavel do pagamento nem como fallback — a queda entre segredos e o furo,
nao a ausencia dela.

E por isso a mensagem de erro carimba QUAL variavel falta: sem ela, quem
esqueceu a do WhatsApp le "PAYMENT_CREDENTIALS_ENCRYPTION_KEY nao
configurada" e vai mexer no lugar errado.
"""

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings


class CredentialEncryptionNotConfiguredError(Exception):
    """Chave de cifra ausente ou invalida."""


class CredentialDecryptionError(Exception):
    """Valor cifrado nao decifra com a chave atual (corrompido ou chave trocada)."""


def encrypt_secret(plain_value: str) -> str:
    """Credencial do gateway de pagamento."""
    return _encrypt(_fernet_do_pagamento(), plain_value)


def decrypt_secret(encrypted_value: str) -> str:
    return _decrypt(_fernet_do_pagamento(), encrypted_value, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY")


def encrypt_whatsapp_token(plain_value: str) -> str:
    """Token de acesso do canal de WhatsApp (Cloud API)."""
    return _encrypt(_fernet_do_whatsapp(), plain_value)


def decrypt_whatsapp_token(encrypted_value: str) -> str:
    return _decrypt(_fernet_do_whatsapp(), encrypted_value, "WHATSAPP_TOKEN_ENCRYPTION_KEY")


def _encrypt(fernet: Fernet, plain_value: str) -> str:
    return fernet.encrypt(plain_value.encode("utf-8")).decode("ascii")


def _decrypt(fernet: Fernet, encrypted_value: str, variavel: str) -> str:
    try:
        return fernet.decrypt(encrypted_value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Nao foi possivel decifrar a credencial: valor corrompido ou "
            f"{variavel} diferente da usada para cifrar."
        ) from exc


def _fernet_do_pagamento() -> Fernet:
    return _fernet(settings.PAYMENT_CREDENTIALS_ENCRYPTION_KEY, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY")


def _fernet_do_whatsapp() -> Fernet:
    return _fernet(settings.WHATSAPP_TOKEN_ENCRYPTION_KEY, "WHATSAPP_TOKEN_ENCRYPTION_KEY")


def _fernet(chave_configurada: str | None, variavel: str) -> Fernet:
    key = (chave_configurada or "").strip()
    if not key:
        raise CredentialEncryptionNotConfiguredError(
            f"{variavel} nao configurada: nenhuma credencial protegida por ela "
            "pode ser cifrada ou lida."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionNotConfiguredError(
            f"{variavel} invalida: precisa ser uma chave Fernet (32 bytes "
            "urlsafe-base64). Gere com "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        ) from exc
