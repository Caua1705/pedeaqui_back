"""Qual regra de cashback vale para ESTA filial, neste instante.

Existe pelo mesmo motivo de `branch_operation.py`: haver **um** lugar que
combine o que esta na filial com o que esta no restaurante. Duas
implementacoes da mesma heranca discordam sem erro, e a discordancia aqui e
dinheiro creditado a mais ou a menos.

## A heranca e por LINHA, e nao por coluna

Em `branches` (revisao 20260818_0025), `NULL` numa coluna significa "herda o
valor do restaurante", e a resolucao e campo a campo. **Aqui nao.** A filial
tem a regra inteira (`cashback_rules.branch_id` preenchido) ou nao tem
nenhuma, e nesse caso vale a linha de `branch_id IS NULL`.

O que impede a heranca por coluna e o percentual por dia da semana: ele mora
numa tabela filha, e "coluna nula" nao existe numa tabela filha. Uma regra
meio herdada — percentual base do restaurante, terca-feira da filial — nao e
explicavel para o lojista.

**Consequencia que precisa ser sabida:** a filial que tem regra propria com
`enabled = false` fica SEM cashback mesmo com a rede inteira ligada. Isso e o
recurso, nao o defeito — e como uma loja sai da campanha.

## Sem regra e SEM CASHBACK, nunca "o padrao da plataforma"

Ao contrario da comissao, que cai em `DEFAULT_PLATFORM_COMMISSION_PERCENT`
quando o restaurante nao tem linha de settings. La o default protege a
receita; aqui um default daria dinheiro do lojista sem ele ter pedido.

Restaurante sem linha, regra desligada e filial que optou por sair caem todos
no mesmo `SEM_CASHBACK`, e quem chama confere um campo so.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from src.core.constants import PLATFORM_TIMEZONE
from src.models.cashback_rule_model import CashbackRule
from src.utils.money import ZERO, to_decimal
from src.utils.security import utcnow


# O dia da semana e o do BALCAO, nao o do UTC. As 21h de uma segunda em
# Belem ja e terca em UTC, e a terca de 10% comecaria tres horas mais cedo
# todo dia — sem erro em lugar nenhum. Mesmo fuso de BranchHoursService e dos
# relatorios.
CASHBACK_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)


@dataclass(frozen=True)
class CashbackTerms:
    """O que vale para um pedido: gera quanto, resgata a partir de quanto,
    dura quantos dias.

    Frozen porque e resultado de leitura, nao estado: quem quiser mudar a
    regra escreve em `cashback_rules` e resolve de novo.

    `percent` ja vem resolvido para o dia — o chamador nao escolhe dia
    nenhum, e e assim que existe um jeito so de responder "quanto gera hoje".
    """

    enabled: bool
    percent: Decimal
    min_redeem_balance: Decimal
    expiry_days: int


# Sem regra, regra desligada, filial fora da campanha: tudo cai aqui, e quem
# chama confere `enabled` e mais nada.
#
# `expiry_days = 0` nao contradiz o CHECK da tabela (que exige > 0): isto nao
# e linha de banco, e um saldo que nao existe nao tem prazo para vencer.
SEM_CASHBACK = CashbackTerms(
    enabled=False,
    percent=ZERO,
    min_redeem_balance=ZERO,
    expiry_days=0,
)


def resolve_cashback_terms(
    branch_rule: CashbackRule | None,
    restaurant_rule: CashbackRule | None,
    momento: datetime | None = None,
) -> CashbackTerms:
    """Junta a regra da filial e a do restaurante em um valor por campo.

    Chame isto em vez de ler `cashback_rules` direto: a linha da filial
    responde "o que esta sobrescrito", que quase nunca e a pergunta.

    **`momento` e o instante do PEDIDO, nao o do credito.** Os dois nao sao o
    mesmo dia: o pedido de terca (10%) so e concluido na quarta (3%), e quem
    prometeu 10% ao cliente foi a tela do checkout. Passar o relogio do
    credito aqui creditaria o percentual do dia errado, e o cliente teria
    razao na reclamacao.

    O default e o relogio para o caminho que so quer saber "quanto gera
    agora" — a tela do cardapio, o painel. Quem esta creditando um pedido
    passa `order.created_at` e nao usa o default.
    """
    regra = branch_rule if branch_rule is not None else restaurant_rule
    if regra is None:
        return SEM_CASHBACK
    if not regra.enabled:
        return SEM_CASHBACK

    return CashbackTerms(
        enabled=True,
        percent=_percentual_do_dia(regra, _dia_da_semana_local(momento or utcnow())),
        min_redeem_balance=to_decimal(regra.min_redeem_balance),
        expiry_days=regra.expiry_days,
    )


def _percentual_do_dia(regra: CashbackRule, dia_da_semana: int) -> Decimal:
    """O percentual daquele dia, ou o padrao da propria regra.

    **Dia sem linha herda `default_percent`, e nunca zero.** E o oposto do
    `PUT` de horarios (armadilha 3), onde dia ausente significa dia fechado, e
    a inversao e de proposito: com zero, o lojista que cadastrasse SO a terca
    de 10% desligaria o cashback dos outros seis dias — sem erro, sem log, e
    com a tela mostrando exatamente o que ele digitou.

    Laco e nao dicionario porque sao no maximo sete linhas, e o laco se le
    inteiro.
    """
    for dia in regra.weekdays:
        if dia.weekday == dia_da_semana:
            return to_decimal(dia.percent)
    return to_decimal(regra.default_percent)


def _dia_da_semana_local(momento: datetime) -> int:
    """0 = SEGUNDA, no fuso da operacao.

    Zero e segunda porque e o `datetime.weekday()` do Python e o CHECK de
    `branch_business_hours` (armadilha 1). O `getDay()` do JavaScript e
    0 = domingo, e o painel que mandar o numero do JS grava a terca de 10% na
    segunda.

    Datetime ingenuo e tratado como UTC, e nao como o relogio da maquina: o
    processo roda em container UTC e a suite roda no Windows de quem escreve,
    e a alternativa faria o mesmo teste dar dias diferentes nos dois.
    """
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(CASHBACK_TIMEZONE).weekday()
