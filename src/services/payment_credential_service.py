"""Resolve a credencial de pagamento ATIVA de um restaurante.

"Ativa" e definida por settings.MERCADOPAGO_ENVIRONMENT ("test" ou
"production"), uma variavel global: todo restaurante opera no mesmo
ambiente ao mesmo tempo. E o que permite desenvolver inteiro com credencial
de teste e, no dia de ir para producao, trocar so essa variavel — nenhum
restaurante precisa recadastrar nada, porque a linha de producao ja foi
cadastrada com antecedencia (ver scripts/register_restaurant_payment_credential.py).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.core.config import settings
from src.repositories.restaurant_payment_credential_repository import (
    RestaurantPaymentCredentialRepository,
)
from src.utils.crypto import decrypt_secret


@dataclass(frozen=True)
class ActivePaymentCredential:
    environment: str
    public_key: str
    # Decifrado na hora: nunca guardar isto em um atributo de objeto de
    # vida longa, nem loga-lo. Passa direto para o header Authorization da
    # chamada ao gateway.
    access_token: str
    # None quando o restaurante ainda nao cadastrou a "Assinatura secreta"
    # do painel do Mercado Pago (registro criado antes desse campo existir,
    # ou webhook ainda nao configurado la). Mesma regra do access_token:
    # decifrado na hora, nunca logado.
    webhook_secret: str | None


class PaymentCredentialService:
    def __init__(self, db: Session):
        self.repository = RestaurantPaymentCredentialRepository(db)

    def get_active_credential(self, restaurant_id: uuid.UUID) -> ActivePaymentCredential | None:
        """None quando o restaurante nao tem credencial para o ambiente ativo.

        Quem chama decide o que fazer com o None — no PaymentService isso
        vira 503, como ja acontecia quando a credencial global estava vazia.
        """
        environment = settings.MERCADOPAGO_ENVIRONMENT
        record = self.repository.get(restaurant_id, environment)
        if record is None:
            return None
        return ActivePaymentCredential(
            environment=environment,
            public_key=record.public_key,
            access_token=decrypt_secret(record.access_token_encrypted),
            webhook_secret=(
                decrypt_secret(record.webhook_secret_encrypted)
                if record.webhook_secret_encrypted
                else None
            ),
        )
