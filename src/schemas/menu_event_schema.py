"""O que o cardapio manda para o funil, e o que a rota aceita.

Um envelope com varios eventos dentro, e nao um evento por requisicao. O
motivo esta em `docs/funil-e-origem.md`, secao 4: o front acumula em memoria
e manda em lote, o que faz uma sessao inteira caber em duas a quatro
requisicoes em vez de uma por toque.

Os tres campos que descrevem "de onde e de quem" (`restaurant_id`,
`branch_id`, `session_id`) ficam no ENVELOPE e nao em cada evento, porque
sao os mesmos para o lote inteiro por construcao — repeti-los por item seria
a chance de um lote chegar com dois `branch_id` diferentes e ninguem
conseguir dizer qual e o certo.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.core.constants import MAX_TRAFFIC_SOURCE_LENGTH
from src.utils.normalization import normalize_traffic_source


# Quantos eventos cabem num corpo. O front manda a cada ~10s ou 20 eventos, o
# que vier primeiro (ver a secao 4 do documento), entao 50 e folga larga sobre
# o uso legitimo.
#
# Acima disso o corpo INTEIRO e recusado, nunca truncado: truncar entregaria
# um funil silenciosamente errado, que e pior que um 422 que o front ve na
# primeira tentativa.
MAX_EVENTS_PER_BATCH = 50

# Teto do identificador de sessao. Ele e gerado pelo cliente — e portanto
# entrada NAO CONFIAVEL — e sem teto vira porta para encher a tabela com
# texto arbitrario. Um UUID em hexa tem 36; 64 cobre qualquer formato
# razoavel que o front venha a usar.
MAX_SESSION_ID_LENGTH = 64

MenuEventType = Literal[
    "menu_view",
    "product_view",
    "cart_add",
    "checkout_start",
]


class MenuEventInput(BaseModel):
    """Um degrau, sem quem nem quando.

    **Nao tem instante.** O `occurred_at` e do SERVIDOR: relogio de celular
    erra, as vezes por meses, e um evento datado em 2027 nao apareceria em
    erro nenhum — ele sumiria da janela do relatorio e o numero ficaria
    errado para sempre, sem nada no log.

    A consequencia assumida e que o lote inteiro chega com o instante da
    requisicao, e nao o de cada toque. Para contar quantas sessoes chegaram a
    cada degrau, a diferenca de segundos nao muda nada.
    """

    # `extra="ignore"` como o envelope, e nao `"forbid"` como o resto dos
    # schemas de escrita do projeto. Aqui um front que mande `occurred_at`
    # tem o campo IGNORADO e o horario do servidor vale; com `"forbid"` ele
    # perderia o lote inteiro num 422 que ninguem le (a ultima leva sai por
    # `sendBeacon`). Ignorar em silencio e normalmente o modo de falha ruim —
    # nesta rota especifica e o bom, porque o campo ignorado nao muda
    # resultado nenhum e o lote perdido apaga a medicao.
    model_config = ConfigDict(extra="ignore")

    event_type: MenuEventType
    # So faz sentido em `product_view` e `cart_add`. Chega nulo nos outros
    # dois, e nao ha validacao cruzada de proposito: um `product_id` sobrando
    # num `menu_view` nao envenena contagem nenhuma (o relatorio agrupa por
    # tipo), e recusar o lote por causa disso perderia os outros 49 eventos.
    product_id: UUID | None = None


class MenuEventBatchRequest(BaseModel):
    """O lote que o cardapio manda.

    `extra="ignore"` e nao `"forbid"`, ao contrario da avaliacao: esta rota e
    disparada por `navigator.sendBeacon` no fechamento da aba, onde nao ha
    ninguem para ler um 422. Campo desconhecido vindo de uma versao mais nova
    do front nao pode custar o lote inteiro.
    """

    model_config = ConfigDict(extra="ignore")

    restaurant_id: UUID
    branch_id: UUID
    session_id: str = Field(min_length=1, max_length=MAX_SESSION_ID_LENGTH)
    # Ausente vira `direct` na normalizacao — nunca 422. Ver o validador.
    #
    # `validate_default=True` nao e enfeite: sem ele o validador NAO roda
    # quando o campo vem ausente, e o lote de quem entrou sem parametro
    # nenhum — o caso mais comum — gravaria `None` numa coluna NOT NULL.
    source: str | None = Field(
        default=None,
        max_length=MAX_TRAFFIC_SOURCE_LENGTH,
        validate_default=True,
    )
    events: list[MenuEventInput] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)

    @field_validator("source")
    @classmethod
    def normalize_source(cls, value: str | None) -> str:
        """Rotulo irreconhecivel NAO e recusado: vira `direct`.

        O valor vem de um QR impresso ou de um link que alguem digitou. Um
        422 aqui nao consertaria o QR — so apagaria o funil daquela sessao,
        que e a informacao que estamos tentando coletar. Ver
        `normalize_traffic_source`.
        """
        return normalize_traffic_source(value)


class MenuEventBatchResponse(BaseModel):
    """Quantos eventos entraram.

    Existe para o front conseguir distinguir "gravei" de "engoli" durante o
    desenvolvimento. Em producao ninguem le: o `sendBeacon` nem entrega a
    resposta a pagina.
    """

    recorded: int
