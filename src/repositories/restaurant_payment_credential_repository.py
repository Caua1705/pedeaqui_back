import uuid

from sqlalchemy import func, select
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
    ) -> RestaurantPaymentCredential:
        """Cadastra ou substitui a credencial do (restaurante, ambiente).

        `ON CONFLICT DO UPDATE` e nao um SELECT-depois-INSERT/UPDATE: o
        script de cadastro (scripts/register_restaurant_payment_credential.py)
        precisa poder rodar de novo para trocar um token vazado sem antes
        descobrir se ja existe linha.
        """
        stmt = (
            pg_insert(RestaurantPaymentCredential)
            .values(
                restaurant_id=restaurant_id,
                environment=environment,
                public_key=public_key,
                access_token_encrypted=access_token_encrypted,
            )
            .on_conflict_do_update(
                constraint="uq_restaurant_payment_credentials_restaurant_environment",
                set_={
                    "public_key": public_key,
                    "access_token_encrypted": access_token_encrypted,
                    "updated_at": func.now(),
                },
            )
            .returning(RestaurantPaymentCredential)
        )
        return self.db.execute(stmt).scalar_one()
