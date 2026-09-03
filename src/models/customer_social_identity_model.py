import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class CustomerSocialIdentity(Base):
    """De quem e esta conta do lado do provedor de identidade. Revisao 0049.

    `provider_user_id` e o `sub` do `id_token`, e **e ele que liga**, nunca o
    e-mail: o e-mail muda e a pessoa continua a mesma. Ligar por e-mail
    transformaria uma troca de endereco no Google em perda da conta.

    O e-mail do Google NAO e gravado aqui, nem para suporte. Nao serve para o
    vinculo e seria dado pessoal numa segunda tabela fora de `customers` — a
    copia a mais que a exclusao de conta pode esquecer. `sub` e opaco.
    """

    __tablename__ = "customer_social_identities"
    __table_args__ = (
        # Uma conta do provedor aponta para UM cliente, sempre. Nao ha o par
        # (customer_id, provider) de proposito: dois Google no mesmo cliente
        # e legitimo (o pessoal e o do trabalho, os dois confirmados por
        # codigo), e o UNIQUE ali derrubaria a segunda ligacao com
        # IntegrityError no fim de um fluxo que deu certo.
        UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_customer_social_identities_provider_user",
        ),
        CheckConstraint(
            "provider = ANY (ARRAY['google'::text])",
            name="ck_customer_social_identities_provider",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    # Nulo ate o primeiro login por este provedor: no caso (b) a ligacao
    # acontece numa requisicao e o login sai na mesma, mas a ordem de escrita
    # deixa a coluna passar por nula.
    last_login_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
