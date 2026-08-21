import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.models.order_model import Order
from src.models.order_review_model import OrderReview


class OrderReviewRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_order_id(self, order_id: uuid.UUID) -> OrderReview | None:
        stmt = select(OrderReview).where(OrderReview.order_id == order_id)
        return self.db.scalar(stmt)

    def create(self, order_id: uuid.UUID, **campos) -> OrderReview:
        """NAO commita: quem commita e o service (regra de camadas)."""
        avaliacao = OrderReview(order_id=order_id, **campos)
        self.db.add(avaliacao)
        self.db.flush()
        return avaliacao

    def update(self, avaliacao: OrderReview, **campos) -> OrderReview:
        for nome, valor in campos.items():
            setattr(avaliacao, nome, valor)
        self.db.flush()
        return avaliacao

    def list_by_customer(self, customer_id: uuid.UUID) -> list[tuple[OrderReview, int]]:
        """As avaliacoes desta pessoa, para a exportacao de dados dela."""
        stmt = (
            select(OrderReview, Order.order_number)
            .join(Order, Order.id == OrderReview.order_id)
            .where(Order.customer_id == customer_id)
            .order_by(OrderReview.created_at.desc())
        )
        return [(linha[0], linha[1]) for linha in self.db.execute(stmt).all()]

    def clear_comments_of_customer(self, customer_id: uuid.UUID) -> int:
        """Apaga o TEXTO das avaliacoes desta pessoa. A nota fica.

        Chamado pela exclusao de conta. A nota e numero, nao identifica
        ninguem e e o historico de qualidade do restaurante — apaga-la
        reescreveria a media do lojista a cada exclusao. O comentario e o
        campo livre, e e nele que a pessoa escreve endereco e nome.

        Um `UPDATE` so, com subconsulta: o laco em Python seria uma ida ao
        banco por avaliacao dentro de uma transacao que ja e longa.
        """
        stmt = (
            update(OrderReview)
            .where(
                OrderReview.order_id.in_(
                    select(Order.id).where(Order.customer_id == customer_id)
                ),
                OrderReview.comment.is_not(None),
            )
            .values(comment=None)
        )
        return self.db.execute(stmt).rowcount or 0

    def clear_comments_created_before(self, cutoff: datetime) -> int:
        """Apaga o texto das avaliacoes velhas. A nota fica.

        A OUTRA metade da defesa, e ela existe porque `orders.customer_id` e
        NULO no pedido de convidado: aquele texto nao e alcancavel por conta
        nenhuma, exatamente como o do `ai_feedback`. Quem sabe ate quando e
        `order_review_service.review_retention_cutoff`.

        E `UPDATE` e nao `DELETE`, ao contrario das outras quatro tabelas do
        expurgo: apagar a linha levaria a nota junto e reescreveria a media
        historica do lojista.
        """
        stmt = (
            update(OrderReview)
            .where(OrderReview.created_at < cutoff, OrderReview.comment.is_not(None))
            .values(comment=None)
        )
        return self.db.execute(stmt).rowcount or 0
