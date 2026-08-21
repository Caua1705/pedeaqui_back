from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Teto do comentario. O MESMO de `orders.notes`, e nao por acaso: os dois sao
# o campo livre em que a pessoa escreve uma frase sobre o pedido, e um teto
# diferente aqui so criaria uma segunda regra para lembrar.
MAX_REVIEW_COMMENT_LENGTH = 500

# A nota a partir da qual NAO se pergunta o que deu errado. Ver
# `CreateOrderReviewRequest.validate_problem_tag_matches_rating`.
LOW_RATING_CEILING = 3

ReviewProblemTag = Literal[
    "atrasou",
    "veio_errado",
    "veio_frio",
    "faltou_item",
    "qualidade",
    "outro",
]


class CreateOrderReviewRequest(BaseModel):
    """A avaliacao que o cliente manda. Nota obrigatoria, o resto opcional.

    UMA nota geral, e nao notas separadas por comida/entrega/embalagem. O
    motivo nao e so taxa de resposta: o formulario teria que mudar de forma
    por `order_type`, porque pedido de RETIRADA nao tem entrega — e uma nota
    de entrega nula ficaria indistinguivel de "nao respondeu", fazendo a
    media por dimensao depender do mix de retirada da loja.
    """

    model_config = ConfigDict(extra="forbid")

    rating: int = Field(ge=1, le=5)
    problem_tag: ReviewProblemTag | None = None
    comment: str | None = Field(default=None, max_length=MAX_REVIEW_COMMENT_LENGTH)

    @model_validator(mode="after")
    def validate_problem_tag_matches_rating(self):
        """Etiqueta de problema so acompanha nota baixa.

        Um 5 estrelas com `problem_tag="atrasou"` nao e uma opiniao
        complexa: e front mandando campo que a tela nao devia ter mostrado.
        Aceitar isso envenena o agregado do painel, que e a unica razao de a
        etiqueta existir — "7 das 12 notas baixas desta semana foram atraso"
        deixa de ser verdade se um 5 estrelas entrar na conta.

        422 e nao descarte silencioso, pela mesma razao do `branch_id`
        obrigatorio no /chat: o front descobre na primeira chamada, com o
        nome do campo, em vez de mandar dado que some sem aviso.
        """
        if self.problem_tag is not None and self.rating > LOW_RATING_CEILING:
            raise ValueError(
                f"problem_tag so e aceito com rating ate {LOW_RATING_CEILING}"
            )
        return self

    @model_validator(mode="after")
    def blank_comment_is_no_comment(self):
        """`"   "` vira None. Sem isto o painel mostra um balao vazio."""
        if self.comment is not None and not self.comment.strip():
            self.comment = None
        return self


class OrderReviewResponse(BaseModel):
    """A avaliacao gravada, devolvida para a tela confirmar o que ficou.

    Nao leva `order_id`: quem chamou ja tem o token do pedido, e devolve-lo
    so acrescentaria um identificador interno a uma resposta publica.
    """

    id: UUID
    rating: int
    problem_tag: ReviewProblemTag | None = None
    comment: str | None = None
    created_at: datetime
    updated_at: datetime
