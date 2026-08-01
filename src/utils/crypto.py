"""Cifra em repouso de segredo de terceiro (access token de gateway).

Fernet e nao um hash: senha e codigo de verificacao a gente so precisa
CONFERIR (hash_password, hash_verification_code em security.py), nunca
devolver o valor original. O access token do restaurante e o oposto — a
aplicacao precisa mandar o valor original para o Mercado Pago a cada
cobranca, entao tem que dar para decifrar. Fernet e simetrico e autenticado
(AES-128-CBC + HMAC): decifra so quem tem a chave, e um bloco alterado no
banco quebra a verificacao em vez de decifrar em lixo.

A chave mora em settings.PAYMENT_CREDENTIALS_ENCRYPTION_KEY (.env), nunca no
banco — cifrar a coluna e inutil se a chave fica ao lado dela.
"""

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import settings


class CredentialEncryptionNotConfiguredError(Exception):
    """PAYMENT_CREDENTIALS_ENCRYPTION_KEY ausente ou invalida."""


class CredentialDecryptionError(Exception):
    """Valor cifrado nao decifra com a chave atual (corrompido ou chave trocada)."""


def encrypt_secret(plain_value: str) -> str:
    return _fernet().encrypt(plain_value.encode("utf-8")).decode("ascii")


def decrypt_secret(encrypted_value: str) -> str:
    try:
        return _fernet().decrypt(encrypted_value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialDecryptionError(
            "Nao foi possivel decifrar a credencial: valor corrompido ou "
            "PAYMENT_CREDENTIALS_ENCRYPTION_KEY diferente da usada para cifrar."
        ) from exc


def _fernet() -> Fernet:
    key = (settings.PAYMENT_CREDENTIALS_ENCRYPTION_KEY or "").strip()
    if not key:
        raise CredentialEncryptionNotConfiguredError(
            "PAYMENT_CREDENTIALS_ENCRYPTION_KEY nao configurada: nenhuma "
            "credencial de pagamento pode ser cifrada ou lida."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionNotConfiguredError(
            "PAYMENT_CREDENTIALS_ENCRYPTION_KEY invalida: precisa ser uma chave "
            "Fernet (32 bytes urlsafe-base64). Gere com "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        ) from exc
