"""A leitura das duas regras, contra o Postgres.

Contra banco de verdade porque o que pode dar errado aqui é SQL: o `OR` que
traz a linha da rede junto com a da filial, o escopo do restaurante, e o
`selectinload` que evita um SELECT por regra dentro do checkout.

A resolução em si é testada sem banco em `test_cashback_rule.py`.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import inspect

from src.models.cashback_rule_model import CashbackRule, CashbackRuleWeekday
from src.repositories.cashback_rule_repository import CashbackRuleRepository
from src.services.cashback_rule import resolve_cashback_terms
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


def criar_regra(db, restaurante, filial=None, *, enabled=True, percent="5.00", dias=()):
    regra = CashbackRule(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
        enabled=enabled,
        default_percent=Decimal(percent),
        min_redeem_balance=Decimal("5.00"),
        expiry_days=60,
    )
    db.add(regra)
    db.flush()
    for dia, percentual in dias:
        db.add(
            CashbackRuleWeekday(
                rule_id=regra.id, weekday=dia, percent=Decimal(percentual)
            )
        )
    db.flush()
    return regra


def test_filial_sem_regra_propria_traz_so_a_do_restaurante(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, percent="5.00")

    da_filial, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial.id
    )

    assert da_filial is None
    assert do_restaurante is not None
    assert do_restaurante.default_percent == Decimal("5.00")


def test_as_duas_linhas_vem_juntas_quando_a_filial_sobrescreve(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, percent="5.00")
    criar_regra(db, restaurante, filial, percent="8.00")

    da_filial, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial.id
    )

    assert da_filial.default_percent == Decimal("8.00")
    assert do_restaurante.default_percent == Decimal("5.00")


def test_a_regra_de_outra_filial_nao_vaza(db):
    """Duas lojas da mesma rede, cada uma com o próprio dia fraco.

    Sem o `branch_id` no WHERE, a Matriz leria a regra da Varjota — e o
    percentual da terça de uma loja sairia na outra.
    """
    restaurante = criar_restaurante(db)
    matriz = criar_filial(db, restaurante, nome="Matriz")
    varjota = criar_filial(db, restaurante, nome="Varjota")
    criar_regra(db, restaurante, percent="5.00")
    criar_regra(db, restaurante, varjota, percent="8.00")

    da_filial, _ = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, matriz.id
    )

    assert da_filial is None


def test_a_regra_de_outro_restaurante_nao_vaza(db):
    outro = criar_restaurante(db)
    filial_de_outro = criar_filial(db, outro)
    criar_regra(db, outro, percent="9.00")
    restaurante = criar_restaurante(db)

    da_filial, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial_de_outro.id
    )

    assert da_filial is None
    assert do_restaurante is None


def test_os_dias_da_semana_vem_carregados_na_mesma_ida(db):
    """`selectinload`, e não lazy: o N+1 apareceria dentro do checkout.

    `inspect(...).unloaded` é o que prova a carga — sem ele o teste passaria
    do mesmo jeito, disparando o SELECT extra que se quer evitar.
    """
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, percent="5.00", dias=[(1, "10.00")])
    db.expire_all()

    _, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial.id
    )

    assert "weekdays" not in inspect(do_restaurante).unloaded
    assert [(dia.weekday, dia.percent) for dia in do_restaurante.weekdays] == [
        (1, Decimal("10.00"))
    ]


def test_do_banco_ate_o_percentual_do_dia(db):
    """As duas metades juntas, que é como o pedido vai usar."""
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    criar_regra(db, restaurante, percent="5.00")
    criar_regra(db, restaurante, filial, percent="3.00", dias=[(1, "10.00")])

    da_filial, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial.id
    )
    terca = datetime(2026, 8, 25, 12, tzinfo=timezone.utc)
    terms = resolve_cashback_terms(da_filial, do_restaurante, terca)

    assert terms.enabled
    assert terms.percent == Decimal("10.00")
