import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.customer_social_identity_model import CustomerSocialIdentity


class CustomerSocialIdentityRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_provider_user(
        self, provider: str, provider_user_id: str
    ) -> CustomerSocialIdentity | None:
        """A identidade pelo par que o UNIQUE protege — o `sub`, nunca o e-mail.

        Nao existe `get_by_email` nesta tabela, e a ausencia e a regra: o
        e-mail nao esta gravado aqui, e ligar por ele faria a pessoa perder a
        conta ao trocar de endereco no Google.
        """
        stmt = select(CustomerSocialIdentity).where(
            CustomerSocialIdentity.provider == provider,
            CustomerSocialIdentity.provider_user_id == provider_user_id,
        )
        return self.db.scalar(stmt)

    def list_of_customer(self, customer_id: uuid.UUID) -> list[CustomerSocialIdentity]:
        stmt = (
            select(CustomerSocialIdentity)
            .where(CustomerSocialIdentity.customer_id == customer_id)
            .order_by(CustomerSocialIdentity.created_at)
        )
        return list(self.db.scalars(stmt).all())

    def create(self, **values) -> CustomerSocialIdentity:
        identity = CustomerSocialIdentity(**values)
        self.db.add(identity)
        self.db.flush()
        return identity

    def register_login(
        self, identity: CustomerSocialIdentity, now: datetime
    ) -> CustomerSocialIdentity:
        identity.last_login_at = now
        self.db.add(identity)
        self.db.flush()
        return identity

    def delete_of_customer(self, customer_id: uuid.UUID) -> int:
        """Apaga as identidades sociais da pessoa. Devolve quantas sairam.

        Chamado pela exclusao de conta. Sem este passo a conta anonimizada
        continuaria guardando um ponteiro para o cadastro dela dentro do
        Google — o mesmo motivo pelo qual `_delete_saved_cards` apaga o perfil
        de pagamento junto do cartao.

        E o segundo estrago seria pior: a identidade sobrevivente faria quem
        excluiu a conta e voltou pelo Google cair no caso "sub conhecido",
        logado numa conta `is_active=False`. 403 para sempre, sem como se
        recadastrar pelo Google.
        """
        resultado = self.db.execute(
            delete(CustomerSocialIdentity).where(
                CustomerSocialIdentity.customer_id == customer_id
            )
        )
        return resultado.rowcount or 0
