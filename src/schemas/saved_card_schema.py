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
    """O cartao como a tela precisa dele: para reconhecer E para tokenizar.

    `id` e o nosso UUID, e e ele que volta em `card.saved_card_id` na hora
    de pagar.

    `provider_card_id` e o id do cartao na conta do Mercado Pago do lojista,
    e ele SAI daqui de proposito. A versao anterior deste contrato o retinha
    ("nao ajudaria o front em nada") e isso contradizia o
    `CardPaymentPayload` deste mesmo backend, que exige um `token` gerado no
    navegador "a partir do `card_id` mais o CVV". Sem este campo o navegador
    mandava o nosso UUID no lugar e a tokenizacao voltava
    `400 {"message":"invalid card_id", "cause":[{"code":"E201"}]}` —
    a tela de CVV nunca conseguia confirmar cartao salvo nenhum.

    Publicar o valor nao afrouxa nada: ele so vale acompanhado do CVV (que
    esta pessoa acabou de digitar) ou do access_token do lojista (que nunca
    sai do servidor), a rota e `/customers/me/cards` autorizada pelo Bearer
    do dono do cartao, e a propria referencia do Mercado Pago carrega esse id
    num input escondido do navegador.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider_card_id: str = Field(
        description=(
            "Id do cartao na conta do Mercado Pago do restaurante. O SDK do "
            "navegador precisa dele para gerar o token de cobranca a partir "
            "de `card_id` + CVV."
        ),
    )
    brand: str = Field(
        description='Bandeira, no vocabulario do gateway: "visa", "master", "elo".',
    )
    last_four_digits: str = Field(description="Os quatro ultimos digitos, para a tela.")
    expiration_month: int | None = None
    expiration_year: int | None = None
    created_at: datetime
