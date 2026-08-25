"""Consultas de cartao salvo. So consulta: nao decide e nao commita."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.customer_saved_card_model import CustomerPaymentProfile, CustomerSavedCard


class CustomerSavedCardRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_profile(
        self, customer_id: uuid.UUID, restaurant_id: uuid.UUID, environment: str
    ) -> CustomerPaymentProfile | None:
        statement = select(CustomerPaymentProfile).where(
            CustomerPaymentProfile.customer_id == customer_id,
            CustomerPaymentProfile.restaurant_id == restaurant_id,
            CustomerPaymentProfile.environment == environment,
        )
        return self.db.execute(statement).scalar_one_or_none()

    def create_profile(self, profile: CustomerPaymentProfile) -> CustomerPaymentProfile:
        self.db.add(profile)
        self.db.flush()
        return profile

    def list_cards(self, profile_id: uuid.UUID) -> list[CustomerSavedCard]:
        """Mais recente primeiro: o cartao que a pessoa acabou de cadastrar e
        o que ela procura na tela."""
        statement = (
            select(CustomerSavedCard)
            .where(CustomerSavedCard.payment_profile_id == profile_id)
            .order_by(CustomerSavedCard.created_at.desc())
        )
        return list(self.db.execute(statement).scalars().all())

    def get_card_by_provider_id(
        self, profile_id: uuid.UUID, provider_card_id: str
    ) -> CustomerSavedCard | None:
        statement = select(CustomerSavedCard).where(
            CustomerSavedCard.payment_profile_id == profile_id,
            CustomerSavedCard.provider_card_id == provider_card_id,
        )
        return self.db.execute(statement).scalar_one_or_none()

    def get_card_of_customer(
        self, customer_id: uuid.UUID, card_id: uuid.UUID
    ) -> CustomerSavedCard | None:
        """O cartao E o perfil dono dele, conferindo que o perfil e do cliente.

        O JOIN e a autorizacao: sem ele, um id de cartao de outra pessoa
        seria removido — ou cobrado — por quem tivesse o UUID. Escopo vem do
        token, nunca do que o cliente manda na URL.
        """
        statement = (
            select(CustomerSavedCard)
            .join(CustomerPaymentProfile)
            .where(
                CustomerSavedCard.id == card_id,
                CustomerPaymentProfile.customer_id == customer_id,
            )
        )
        return self.db.execute(statement).scalar_one_or_none()

    def add_card(self, card: CustomerSavedCard) -> CustomerSavedCard:
        self.db.add(card)
        self.db.flush()
        return card

    def delete_card(self, card: CustomerSavedCard) -> None:
        self.db.delete(card)
        self.db.flush()

    def list_all_cards_of_customer(self, customer_id: uuid.UUID) -> list[CustomerSavedCard]:
        """Todos os cartoes da pessoa, em todos os restaurantes e ambientes.

        Existe para a exclusao de conta (LGPD): la o escopo e a PESSOA, e
        nao uma loja.
        """
        statement = (
            select(CustomerSavedCard)
            .join(CustomerPaymentProfile)
            .where(CustomerPaymentProfile.customer_id == customer_id)
        )
        return list(self.db.execute(statement).scalars().all())

    def list_profiles_of_customer(
        self, customer_id: uuid.UUID
    ) -> list[CustomerPaymentProfile]:
        statement = select(CustomerPaymentProfile).where(
            CustomerPaymentProfile.customer_id == customer_id
        )
        return list(self.db.execute(statement).scalars().all())

    def delete_profile(self, profile: CustomerPaymentProfile) -> None:
        self.db.delete(profile)
        self.db.flush()
