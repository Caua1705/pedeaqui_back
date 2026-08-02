"""Credencial de pagamento por restaurante (BLOCO G da Fase 2).

Cifra em repouso e resolucao da credencial ATIVA (teste ou producao,
conforme MERCADOPAGO_ENVIRONMENT) de um restaurante. O que sai no gateway
de verdade (create_payment usando o access_token resolvido aqui) e coberto
em test_payments.py.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from src.core.config import settings
from src.services.payment_credential_service import PaymentCredentialService
from src.utils.crypto import (
    CredentialDecryptionError,
    CredentialEncryptionNotConfiguredError,
    decrypt_secret,
    encrypt_secret,
)


TEST_KEY = Fernet.generate_key().decode("ascii")


class CryptoTests(unittest.TestCase):
    def test_round_trip_recovers_the_original_value(self):
        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", TEST_KEY):
            encrypted = encrypt_secret("TEST-1234567890-access-token")
            self.assertEqual(decrypt_secret(encrypted), "TEST-1234567890-access-token")

    def test_ciphertext_does_not_contain_the_plain_value(self):
        # Confere que estamos cifrando de verdade, e nao so guardando o
        # texto puro com um nome de coluna diferente.
        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", TEST_KEY):
            encrypted = encrypt_secret("TEST-segredo-do-restaurante")

        self.assertNotIn("TEST-segredo-do-restaurante", encrypted)

    def test_missing_key_refuses_to_encrypt(self):
        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", None):
            with self.assertRaises(CredentialEncryptionNotConfiguredError):
                encrypt_secret("qualquer-coisa")

    def test_missing_key_refuses_to_decrypt(self):
        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", None):
            with self.assertRaises(CredentialEncryptionNotConfiguredError):
                decrypt_secret("qualquer-coisa")

    def test_tampered_ciphertext_does_not_decrypt_into_garbage(self):
        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", TEST_KEY):
            encrypted = encrypt_secret("TEST-1234567890-access-token")
            last_char = "A" if encrypted[-1] != "A" else "B"
            tampered = encrypted[:-1] + last_char
            with self.assertRaises(CredentialDecryptionError):
                decrypt_secret(tampered)

    def test_wrong_key_does_not_decrypt(self):
        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", TEST_KEY):
            encrypted = encrypt_secret("TEST-1234567890-access-token")

        with patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", Fernet.generate_key().decode()):
            with self.assertRaises(CredentialDecryptionError):
                decrypt_secret(encrypted)


class FakeCredentialRepository:
    def __init__(self, record=None):
        self.record = record
        self.requested = None

    def get(self, restaurant_id, environment):
        self.requested = (restaurant_id, environment)
        return self.record


def make_record(**overrides):
    values = {
        "public_key": "PUBLIC-KEY-DO-JUNIOR",
        "access_token_encrypted": None,
        "webhook_secret_encrypted": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PaymentCredentialServiceTests(unittest.TestCase):
    def setUp(self):
        self.key_patcher = patch.object(settings, "PAYMENT_CREDENTIALS_ENCRYPTION_KEY", TEST_KEY)
        self.key_patcher.start()
        self.addCleanup(self.key_patcher.stop)

    def test_no_credential_registered_returns_none(self):
        service = PaymentCredentialService.__new__(PaymentCredentialService)
        service.repository = FakeCredentialRepository(record=None)

        result = service.get_active_credential(uuid.uuid4())

        self.assertIsNone(result)

    def test_resolves_and_decrypts_the_credential_of_the_active_environment(self):
        record = make_record(access_token_encrypted=encrypt_secret("TEST-token-do-junior"))
        repository = FakeCredentialRepository(record=record)
        service = PaymentCredentialService.__new__(PaymentCredentialService)
        service.repository = repository
        restaurant_id = uuid.uuid4()

        with patch.object(settings, "MERCADOPAGO_ENVIRONMENT", "test"):
            credential = service.get_active_credential(restaurant_id)

        self.assertEqual(credential.access_token, "TEST-token-do-junior")
        self.assertEqual(credential.public_key, "PUBLIC-KEY-DO-JUNIOR")
        self.assertEqual(credential.environment, "test")
        # A troca de teste para producao e so essa variavel: confere que a
        # busca no repositorio de fato usa MERCADOPAGO_ENVIRONMENT.
        self.assertEqual(repository.requested, (restaurant_id, "test"))

    def test_resolves_and_decrypts_the_webhook_secret_when_registered(self):
        record = make_record(
            access_token_encrypted=encrypt_secret("TEST-token-do-junior"),
            webhook_secret_encrypted=encrypt_secret("segredo-do-webhook-do-junior"),
        )
        service = PaymentCredentialService.__new__(PaymentCredentialService)
        service.repository = FakeCredentialRepository(record=record)

        credential = service.get_active_credential(uuid.uuid4())

        self.assertEqual(credential.webhook_secret, "segredo-do-webhook-do-junior")

    def test_webhook_secret_is_none_when_not_yet_registered(self):
        # Credencial cadastrada antes deste campo existir, ou restaurante
        # que ainda nao configurou a Notification URL no painel.
        record = make_record(access_token_encrypted=encrypt_secret("TEST-token-do-junior"))
        service = PaymentCredentialService.__new__(PaymentCredentialService)
        service.repository = FakeCredentialRepository(record=record)

        credential = service.get_active_credential(uuid.uuid4())

        self.assertIsNone(credential.webhook_secret)

    def test_switching_the_environment_changes_which_credential_is_requested(self):
        repository = FakeCredentialRepository(record=None)
        service = PaymentCredentialService.__new__(PaymentCredentialService)
        service.repository = repository
        restaurant_id = uuid.uuid4()

        with patch.object(settings, "MERCADOPAGO_ENVIRONMENT", "production"):
            service.get_active_credential(restaurant_id)

        self.assertEqual(repository.requested, (restaurant_id, "production"))


if __name__ == "__main__":
    unittest.main()
