"""Contrato de /customers/me/cards.

O que NAO existe aqui e a parte importante: nao ha campo de numero de
cartao, de CVV nem de validade de ENTRADA. O unico dado do cartao que
atravessa a fronteira e o `token` que o SDK do Mercado Pago gerou no
navegador — se algum dia um campo de PAN aparecer neste arquivo, a
integracao saiu do padrao de tokenizacao.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SaveCardRequest(BaseModel):
    """O que o navegador manda para salvar um cartao.

    `restaurant_slug` no corpo, e nao inferido: o cartao salvo vive dentro
    da conta do Mercado Pago DAQUELE restaurante (ver
    `customer_saved_cards`), entao nao existe "salvar o cartao" sem dizer
    onde. Um default silencioso salvaria na loja errada e o cartao nao
    apareceria na tela do checkout.
    """

    restaurant_slug: str = Field(
        description="Restaurante em cuja conta do gateway o cartao sera salvo.",
    )
    token: str = Field(
        min_length=1,
        description=(
            "Token de uso unico gerado pelo SDK do Mercado Pago NO NAVEGADOR. "
            "O numero do cartao nao passa por esta API."
        ),
    )


class SavedCardResponse(BaseModel):
    """O cartao como a tela precisa dele: para reconhecer, nao para cobrar.

    `id` e o nosso UUID, e e ele que volta em `card.saved_card_id` na hora
    de pagar. O id do cartao no Mercado Pago **nao sai daqui** — ele so tem
    sentido dentro da conta do restaurante, e publica-lo nao ajudaria o
    front em nada.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    brand: str = Field(
        description='Bandeira, no vocabulario do gateway: "visa", "master", "elo".',
    )
    last_four_digits: str = Field(description="Os quatro ultimos digitos, para a tela.")
    expiration_month: int | None = None
    expiration_year: int | None = None
    created_at: datetime
