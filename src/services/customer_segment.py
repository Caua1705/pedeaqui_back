"""A classificacao RFV de um cliente, escrita UMA vez, em SQL.

Estas expressoes sao montadas dentro da agregacao de
`AdminCustomerRepository`, entao o rotulo existe **antes** do `LIMIT` — e por
isso da para filtrar por ele sem devolver pagina com tres linhas.

## Por que nao ha mais uma versao em Python

Havia. `classify_customer` era uma funcao pura, testada sem banco, e foi
apagada quando os cinco filtros entraram (21/08/2026). O docstring dela
registrava a divida e as duas saidas possiveis: ou a versao SQL vira a unica,
ou as duas nascem do mesmo lugar. **Venceu a primeira, e o motivo nao foi
gosto.**

Com os filtros pre-`LIMIT`, o SQL deixou de ser opcional. Manter a funcao
Python ao lado a deixaria sem nenhum chamador de producao: quinze testes
rapidos e verdes provando codigo que requisicao nenhuma executa. E o mesmo
caso da armadilha 13 — quando o mecanismo forte passa a cobrir o fraco, o
fraco sai junto, senao vira ruina que parece protecao.

**E havia uma divergencia concreta que so a implementacao unica elimina.** O
Python fazia `(now - last).days`, piso inteiro. O SQL natural seria
`extract(epoch ...) / 86400`, fracionario. Com cadencia de 7 dias o limiar e
14: um cliente a **14 dias e 23 horas** sairia `fiel` de um lado e `em_risco`
do outro. Uma janela de ate 24h por cliente e por limiar, que nenhuma leitura
de codigo revela e que um teste de equivalencia so pegaria se alguem tivesse
pensado em por um caso fracionario na tabela.

O piso inteiro ficou (`floor`), e `days_since_expression` alimenta ao mesmo
tempo o rotulo e o campo `days_since_last_order` da resposta: **o numero na
tela nao consegue discordar da etiqueta ao lado.**

## Por que um modulo de `services/` e importado por um repositorio

E o unico lugar do projeto onde a seta aponta para tras, e e deliberado. A
camada e `endpoint -> service -> repository`, e ela continua valendo para o
CAMINHO DA REQUISICAO: nada aqui consulta, decide ou commita. O que este
modulo entrega sao pedacos de expressao SQL, e eles precisam estar DENTRO da
agregacao — em `HAVING` sobre alias o Postgres nao deixa, e repetir o `CASE`
no filtro seria a segunda copia que este arquivo inteiro existe para evitar.

A regra continua sendo de dominio, e por isso mora em `services/` ao lado de
`branch_operation.py`, e nao em `repositories/`, onde viraria detalhe de
consulta. Quem for mover isto, mova a regra inteira — nao uma metade.

## A escada, na ordem em que o CASE a le

    sem last_order_at                     -> novo
    dias > cadencia * RFV_LOST_FACTOR      -> perdido
    dias > cadencia * RFV_AT_RISK_FACTOR   -> em_risco
    orders_count >= RFV_ORDERS_FOR_LOYAL   -> fiel
    primeiro pedido dentro da janela       -> novo
    senao                                  -> ocasional

Recencia ANTES de contagem, e a ordem e a regra: invertida, o cliente de doze
pedidos sumido ha seis meses sai como "fiel" — e e exatamente ele que a
reativacao precisa achar.

## Duas armadilhas de SQL que estao dentro destas funcoes

**`GREATEST`/`LEAST` do Postgres IGNORAM NULL.** `GREATEST(NULL, 0)` devolve
`0`, e nao `NULL`. Usar isso para pisar os dias em zero transformaria "cliente
sem pedido" em "pediu hoje", sem erro nenhum. Por isso o piso dos dias e um
`CASE` — em `cadence_expression` o `LEAST`/`GREATEST` continua, e la e seguro
porque o valor ja passou por `coalesce`.

**A divisao por zero nao acontece por sorte.** `nullif(orders_count - 1, 0)`
faz a media virar NULL em vez de erro quando ha um pedido so, e o `coalesce`
seguinte a manda para `RFV_FALLBACK_CADENCE_DAYS`. O mesmo `coalesce` cobre
data faltando (`created_at` e nullable no model): se `first` ou `last` for
NULL, o intervalo e NULL e cai no mesmo lugar. Os dois `return` que a versao
Python tinha viraram propagacao de NULL.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, Numeric, case, cast, func, literal

from src.core.config import settings
from src.schemas.admin_customer_schema import CustomerSegment


SEGUNDOS_POR_DIA = 86400.0


def days_since_expression(moment, now: datetime):
    """Dias inteiros entre `moment` e o agora. NULL quando nao ha data.

    `floor`, e nao a divisao crua, porque o limiar e comparado com dias
    inteiros desde sempre — ver o cabecalho deste modulo.
    """
    decorridos = func.extract("epoch", _agora(now) - moment) / SEGUNDOS_POR_DIA
    dias = func.floor(decorridos)
    # `CASE` e nao `GREATEST(dias, 0)`: o GREATEST do Postgres ignora NULL e
    # devolveria 0 para quem nao tem pedido nenhum. Aqui `dias < 0` e NULL
    # quando `dias` e NULL, o CASE cai no `else_`, e o NULL sobrevive.
    return case((dias < 0, literal(0)), else_=cast(dias, Integer))


def cadence_expression(orders_count, first_order_at, last_order_at):
    """De quantos em quantos dias ESTE cliente costuma pedir.

    Grampeada entre `RFV_MIN_CADENCE_DAYS` e `RFV_MAX_CADENCE_DAYS`. Sem o
    piso, dois pedidos no mesmo almoco dao cadencia ~0 e o cliente vira
    "perdido" no dia seguinte; sem o teto, dois pedidos separados por oito
    meses nunca saem de "fiel".
    """
    intervalo = func.extract("epoch", last_order_at - first_order_at) / SEGUNDOS_POR_DIA
    # NULL, e nao erro, quando ha um pedido so: nao ha intervalo a medir.
    media = intervalo / func.nullif(orders_count - 1, 0)
    bruta = func.coalesce(media, float(settings.RFV_FALLBACK_CADENCE_DAYS))
    # Aqui o LEAST/GREATEST e seguro: `bruta` nunca e NULL depois do coalesce.
    return func.least(
        func.greatest(bruta, float(settings.RFV_MIN_CADENCE_DAYS)),
        float(settings.RFV_MAX_CADENCE_DAYS),
    )


def average_ticket_expression(total_spent, billable_orders_count):
    """O gasto dividido pelos pedidos que geraram gasto.

    Arredondado a duas casas AQUI, e nao so na resposta: o filtro
    `min_ticket`/`max_ticket` compara este valor, e um ticket exibido como
    33,33 que nao passa em `min_ticket=33.33` por causa da terceira casa e um
    chamado que ninguem consegue reproduzir.
    """
    ticket = total_spent / func.nullif(billable_orders_count, 0)
    return func.round(cast(func.coalesce(ticket, 0), Numeric), 2)


def segment_expression(orders_count, first_order_at, last_order_at, now: datetime):
    """O rotulo RFV desta linha. Ver a escada no cabecalho do modulo."""
    dias = days_since_expression(last_order_at, now)
    idade_do_relacionamento = days_since_expression(first_order_at, now)
    cadencia = cadence_expression(orders_count, first_order_at, last_order_at)

    return case(
        (last_order_at.is_(None), literal(CustomerSegment.NOVO.value)),
        (dias > cadencia * settings.RFV_LOST_FACTOR, literal(CustomerSegment.PERDIDO.value)),
        (dias > cadencia * settings.RFV_AT_RISK_FACTOR, literal(CustomerSegment.EM_RISCO.value)),
        (orders_count >= settings.RFV_ORDERS_FOR_LOYAL, literal(CustomerSegment.FIEL.value)),
        # Sem `first_order_at` nao da para dizer que o relacionamento e
        # antigo, e quem chegou ate aqui esta em dia com o proprio ritmo:
        # "novo" e a leitura que nao inventa historico.
        (
            func.coalesce(idade_do_relacionamento, 0) <= settings.RFV_NEW_WINDOW_DAYS,
            literal(CustomerSegment.NOVO.value),
        ),
        else_=literal(CustomerSegment.OCASIONAL.value),
    )


def _agora(now: datetime):
    """O instante do lado de CA, preso na consulta como parametro.

    Nao e `func.now()`: o service ja promete um unico agora para a pagina
    inteira, e um relogio escolhido pelo banco nao da para o teste fixar nem
    para a consulta ser reproduzida depois.
    """
    return literal(now, DateTime(timezone=True))
