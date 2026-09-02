"""A janela de validade do cupom. UM lugar, e as duas formas dela.

Antes de 03/09/2026 esta regra existia em **tres copias**:

    src/repositories/coupon_repository.py  list_in_window   (SQL)
    src/repositories/menu_repository.py    a vitrine        (SQL)
    src/services/coupon_service.py         evaluate         (Python)

Tres copias de "esta na janela?" e a divergencia esperando acontecer, e ela
aconteceu: quando `valid_until` passou a poder ser nulo, consertar duas das
tres deixaria o cupom sem prazo aparecendo numa superficie e sumindo na outra —
sem erro, sem log, e com o lojista jurando que criou a campanha.

Por isso as duas formas moram aqui, lado a lado, e quem mudar uma ve a outra.

## `valid_until` NULO significa "NAO EXPIRA"

Nao e campo em branco nem dado faltando: e a campanha permanente — "10% no
canal proprio, sem prazo". Mesmo desenho de `code`, que a revisao
`20260828_0043` tornou nulo **com significado** ("aplica sozinho no checkout").

E por isso a coluna NAO vai ser alinhada para `NOT NULL`, ao contrario das
outras 15 do levantamento da armadilha 50: ali o banco estava frouxo e o model
certo; aqui o model e que esta mentindo, e quem se alinha e ele.

**`valid_from` continua obrigatorio.** Campanha sem inicio nao tem significado
util — "vale desde sempre" e o mesmo que a data de criacao — e deixa-lo nulo
so criaria um segundo jeito de escrever a mesma coisa.

## Por que duas formas e nao uma

Nao da para ter so uma. O SQL precisa da expressao para **recortar linhas no
banco** (a vitrine le todos os cupons do restaurante e nao pode trazer os
vencidos); o Python precisa do predicado para **explicar a recusa** — a
diferenca entre "ainda nao comecou" e "ja acabou" e o que o cliente le no
checkout, e um WHERE nao devolve motivo.

O que este modulo garante e que as duas digam a MESMA coisa, e o
`tests/test_janela_do_cupom.py` cobra isso comparando as duas contra os
mesmos casos.
"""

from datetime import datetime, timezone

from sqlalchemy import ColumnElement, or_

from src.models.coupon_model import RestaurantCoupon


def _com_fuso(momento: datetime) -> datetime:
    """Datetime ingenuo vindo do banco e lido como UTC.

    O Postgres devolve `timestamptz` com fuso, mas uma linha gravada por fora
    (script, correcao a mao) pode chegar ingenua — e comparar ingenuo com
    consciente levanta `TypeError`, que aqui viraria 500 no checkout.
    """
    return momento.replace(tzinfo=timezone.utc) if momento.tzinfo is None else momento.astimezone(timezone.utc)


def ja_comecou(valid_from: datetime, agora: datetime) -> bool:
    return _com_fuso(agora) >= _com_fuso(valid_from)


def ja_acabou(valid_until: datetime | None, agora: datetime) -> bool:
    """Nulo NUNCA acabou. E a campanha permanente."""
    if valid_until is None:
        return False
    return _com_fuso(agora) > _com_fuso(valid_until)


def dentro_da_janela(valid_from: datetime, valid_until: datetime | None, agora: datetime) -> bool:
    return ja_comecou(valid_from, agora) and not ja_acabou(valid_until, agora)


def filtro_de_janela(agora: datetime) -> list[ColumnElement[bool]]:
    """A MESMA regra, em SQL, para o `where()` de quem consulta.

    Devolve uma LISTA de condicoes e nao um `and_()` unico porque e assim que
    os dois repositorios ja escrevem seus `where()` — espalhar a lista mantem
    o estilo deles e deixa o `EXPLAIN` legivel condicao a condicao.

    O `IS NULL` do `valid_until` e a metade que faltava: sem ele a campanha
    sem prazo — que `dentro_da_janela` considera valida — nunca apareceria na
    vitrine, porque `NULL >= agora` nao e verdadeiro, e sim NULO.
    """
    return [
        RestaurantCoupon.valid_from <= agora,
        or_(
            RestaurantCoupon.valid_until.is_(None),
            RestaurantCoupon.valid_until >= agora,
        ),
    ]
