"""O contrato da configuracao de cashback no painel.

Duas coisas deste arquivo o painel precisa saber de cor, e as duas ja
custaram caro em outras frentes deste projeto:

**`weekday` 0 = SEGUNDA.** E o `datetime.weekday()` do Python, o mesmo numero
de `branch_business_hours` (armadilha 1). O `getDay()` do JavaScript e
0 = DOMINGO. O painel que mandar o numero do JS grava a terca de 10% na
segunda, e ninguem ve erro: a tela mostra o que o lojista digitou, e o
cashback sai no dia errado.

**Dia ausente de `weekdays` herda `default_percent`, e NUNCA zero.** E o
oposto do `PUT` de horarios (armadilha 3), onde dia ausente significa dia
fechado. A inversao e de proposito: com zero, o lojista que configurasse so a
terca de 10% desligaria o cashback dos outros seis dias sem erro e sem log.

## Dinheiro e percentual saem como STRING de duas casas

`Decimal`, e nao `float`. A armadilha 34 proibe converter um schema isolado
porque isso faz o MESMO campo ter tipo diferente em rotas diferentes — e o
que dispensa a proibicao aqui e que estes campos sao novos: `default_percent`,
`percent` e `min_redeem_balance` nao existem em resposta nenhuma da API hoje,
entao nao ha o que divergir.

Escolhido o lado do `CouponAdminResponse`, que e o parente proximo (campanha
de desconto configurada pelo dono) e ja e `Decimal`, e nao o de
`AdminRestaurantSettingsResponse`, que e `float`. O que decide e a coluna:
`Numeric(5,2)` promete duas casas, e `10.00` sobrevive como `"10.00"` em
string — como numero JSON ele vira `10.0`, porque JSON nao tem decimal de
casa fixa.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from src.core.constants import DESCRICAO_DE_WEEKDAY
from src.schemas.common_schema import BaseResponse


# De onde saiu a regra que vale. E o campo que o painel le para decidir se
# mostra "regra propria desta loja" ou "herdada da rede" — sem ele, as duas
# respostas sao identicas e a tela nao tem como avisar que editar ali cria uma
# sobrescrita.
CashbackRuleSource = Literal["branch", "restaurant", "none"]

# Sete linhas no maximo, uma por dia. O teto nao e defensivo contra o lojista
# — e contra o painel que mande a lista repetida num laco com bug.
MAX_WEEKDAY_ROWS = 7


class CashbackWeekdayInput(BaseModel):
    """O percentual de UM dia da semana.

    `weekday` 0 = SEGUNDA (veja o topo do arquivo). Nao existe "dia
    desligado": para um dia nao gerar, mande `percent: 0` explicitamente —
    omiti-lo faz o dia herdar `default_percent`.
    """

    weekday: int = Field(ge=0, le=6, description=DESCRICAO_DE_WEEKDAY)
    percent: Decimal = Field(ge=0, le=100)


class CashbackWeekdayResponse(BaseResponse):
    weekday: int = Field(description=DESCRICAO_DE_WEEKDAY)
    percent: Decimal


class AdminCashbackRuleWrite(BaseModel):
    """A regra INTEIRA. Nao ha PATCH aqui, e o motivo e a heranca.

    A heranca do cashback e por LINHA, nao por coluna: a filial tem a regra
    toda ou herda a toda. Um PATCH sobre uma filial que ainda nao tem regra
    propria teria que responder "patch sobre o que?" — sobre os valores
    herdados, criando uma sobrescrita silenciosa a partir de um campo so.
    E exatamente o acidente que o `PUT` torna impossivel: quem escreve manda
    a regra completa, e ve o que esta criando.

    **`weekdays` SUBSTITUI a lista inteira**, como o `PUT` de horarios. Dia
    que sair do corpo deixa de ter linha propria — e volta a valer
    `default_percent`, nao zero.
    """

    enabled: bool
    default_percent: Decimal = Field(ge=0, le=100)
    min_redeem_balance: Decimal = Field(ge=0)
    expiry_days: int = Field(gt=0)
    weekdays: list[CashbackWeekdayInput] = Field(
        default_factory=list, max_length=MAX_WEEKDAY_ROWS
    )

    @model_validator(mode="after")
    def weekday_nao_se_repete(self) -> "AdminCashbackRuleWrite":
        """Dois percentuais para a mesma terca sao 422, e nao "vale o ultimo".

        A chave primaria de `cashback_rule_weekdays` e `(rule_id, weekday)`,
        entao o banco recusaria de qualquer jeito — mas com um 500 que nao
        diz ao painel qual dia veio repetido.
        """
        dias = [dia.weekday for dia in self.weekdays]
        repetidos = sorted({dia for dia in dias if dias.count(dia) > 1})
        if repetidos:
            raise ValueError(f"weekday repetido: {repetidos}")
        return self


class AdminCashbackRuleResponse(BaseResponse):
    """A LINHA de configuracao, como ela esta gravada.

    `branch_id` nulo e a regra padrao da rede; preenchido e a sobrescrita de
    uma filial.
    """

    id: uuid.UUID
    restaurant_id: uuid.UUID
    branch_id: uuid.UUID | None = None
    enabled: bool
    default_percent: Decimal
    min_redeem_balance: Decimal
    expiry_days: int
    weekdays: list[CashbackWeekdayResponse] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AdminCashbackRuleView(BaseModel):
    """A regra que VALE, e de onde ela veio.

    Existe porque `rule` sozinho e ambiguo: a mesma resposta sairia para a
    filial que tem regra propria e para a que herda a da rede, e o painel
    precisa da diferenca para decidir entre "editar a regra desta loja" e
    "criar uma sobrescrita".

    `source = "none"` e regra nenhuma configurada, e ai `rule` vem nulo. Nao
    e o mesmo que `enabled: false`: um e "ninguem configurou", o outro e
    "configurado e desligado". Os dois caem em SEM_CASHBACK no checkout, mas
    so o segundo tem numeros para mostrar na tela.
    """

    source: CashbackRuleSource
    rule: AdminCashbackRuleResponse | None = None
