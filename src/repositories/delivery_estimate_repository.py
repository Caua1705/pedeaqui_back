import hmac
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.delivery_estimate_model import DeliveryEstimate


class DeliveryEstimateRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, estimate: DeliveryEstimate) -> DeliveryEstimate:
        self.db.add(estimate)
        self.db.flush()
        return estimate

    def get_valid_by_token(self, token: str, now: datetime) -> DeliveryEstimate | None:
        # A expiracao entra na consulta e nao no service porque uma linha
        # vencida nao deve nem ser lida: o preco dela nao vale mais.
        stmt = select(DeliveryEstimate).where(
            DeliveryEstimate.token == token,
            DeliveryEstimate.expires_at > now,
        )
        estimate = self.db.scalar(stmt)
        if estimate is None:
            return None

        # Reconferencia em tempo constante, igual a de
        # `order_repository.get_order_by_tracking_token`. Este era o `==` que
        # tinha ficado de fora: o token da estimativa e sorteado pelo MESMO
        # `generate_tracking_token` e vale a mesma regra da armadilha 18.
        #
        # SEJA HONESTO SOBRE O QUE ISTO COMPRA. Se a linha voltou, o `=` do
        # Postgres ja disse que os textos sao iguais, e este `compare_digest`
        # devolve True sempre. O que ele compra e a falha fechada se o WHERE
        # acima deixar de ser igualdade exata um dia — um `ilike`, um `like`
        # com escape errado, uma collation que aproxime formas Unicode
        # (armadilha 31).
        #
        # O que um token de estimativa vazado vale: a taxa, a distancia e o
        # prazo daquele endereco. Nao e o preco do pedido — o fingerprint de
        # endereco continua sendo a defesa que impede pagar a taxa do
        # endereco perto para entregar no longe (armadilha 12).
        if not hmac.compare_digest(estimate.token, token):
            return None
        return estimate

    def delete_expired(self, now: datetime) -> int:
        result = self.db.execute(
            delete(DeliveryEstimate).where(DeliveryEstimate.expires_at <= now)
        )
        return result.rowcount or 0
