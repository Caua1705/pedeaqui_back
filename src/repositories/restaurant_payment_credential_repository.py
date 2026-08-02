import uuid

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.models.restaurant_payment_credential_model import RestaurantPaymentCredential


class RestaurantPaymentCredentialRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, restaurant_id: uuid.UUID, environment: str) -> RestaurantPaymentCredential | None:
        stmt = select(RestaurantPaymentCredential).where(
            RestaurantPaymentCredential.restaurant_id == restaurant_id,
            RestaurantPaymentCredential.environment == environment,
        )
        return self.db.scalar(stmt)

    def upsert(
        self,
        *,
        restaurant_id: uuid.UUID,
        environment: str,
        public_key: str,
        access_token_encrypted: str,
        webhook_secret_encrypted: str | None = None,
    ) -> RestaurantPaymentCredential:
        """Cadastra ou substitui a credencial do (restaurante, ambiente).

        `ON CONFLICT DO UPDATE` e nao um SELECT-depois-INSERT/UPDATE: o
        script de cadastro (scripts/register_restaurant_payment_credential.py)
        precisa poder rodar de novo para trocar um token vazado sem antes
        descobrir se ja existe linha.

        `webhook_secret_encrypted` e None por padrao e so entra no UPDATE
        quando informado: recadastrar (rotacionar) o access_token nao pode
        apagar um segredo de webhook ja configurado so porque quem rodou o
        script desta vez nao tinha esse valor em maos. Para atualizar SO o
        segredo do webhook de uma credencial que ja existe, sem passar de
        novo por access_token/public_key, use update_webhook_secret.
        """
        values = {
            "restaurant_id": restaurant_id,
            "environment": environment,
            "public_key": public_key,
            "access_token_encrypted": access_token_encrypted,
        }
        update_set = {
            "public_key": public_key,
            "access_token_encrypted": access_token_encrypted,
            "updated_at": func.now(),
        }
        if webhook_secret_encrypted is not None:
            values["webhook_secret_encrypted"] = webhook_secret_encrypted
            update_set["webhook_secret_encrypted"] = webhook_secret_encrypted

        stmt = (
            pg_insert(RestaurantPaymentCredential)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_restaurant_payment_credentials_restaurant_environment",
                set_=update_set,
            )
            .returning(RestaurantPaymentCredential)
        )
        return self.db.execute(stmt).scalar_one()

    def update_webhook_secret(
        self,
        restaurant_id: uuid.UUID,
        environment: str,
        webhook_secret_encrypted: str,
    ) -> RestaurantPaymentCredential | None:
        """Atualiza SO o segredo do webhook de uma credencial ja cadastrada.

        Nunca cria linha: uma credencial sem access_token nao serve para
        nada, entao nao faz sentido nascer uma so com o segredo do webhook.
        None quando o (restaurante, ambiente) ainda nao tem credencial —
        quem chama decide o que fazer (o script recusa com uma mensagem
        clara em vez de deixar uma linha incompleta no banco).
        """
        stmt = (
            update(RestaurantPaymentCredential)
            .where(
                RestaurantPaymentCredential.restaurant_id == restaurant_id,
                RestaurantPaymentCredential.environment == environment,
            )
            .values(webhook_secret_encrypted=webhook_secret_encrypted, updated_at=func.now())
            .returning(RestaurantPaymentCredential)
        )
        return self.db.execute(stmt).scalar_one_or_none()
