"""A configuração de cashback pelo painel, contra o Postgres.

Contra banco de verdade porque o que pode quebrar aqui é escrita: os dois
índices parciais (um padrão por restaurante, uma sobrescrita por filial), o
`delete-orphan` que troca a lista de dias, e o `CASCADE` que leva os dias
junto quando a sobrescrita é apagada.

A resolução da herança em si é testada sem banco em `test_cashback_rule.py`;
o que se prova aqui é que o painel **enxerga** essa herança — o `source` — e
que a escrita não a quebra.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.admin_cashback_schema import AdminCashbackRuleWrite
from src.services.admin_cashback_service import AdminCashbackService
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


def escopo(restaurante, filial=None) -> AdminScope:
    """Escopo de dono. `branch_id` nulo é "todas as filiais"."""
    return AdminScope(
        admin_user=SimpleNamespace(role="owner"),
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
    )


def corpo(**mudancas) -> AdminCashbackRuleWrite:
    campos = {
        "enabled": True,
        "default_percent": Decimal("5.00"),
        "min_redeem_balance": Decimal("10.00"),
        "expiry_days": 60,
        "weekdays": [],
    }
    campos.update(mudancas)
    return AdminCashbackRuleWrite(**campos)


# ---------------------------------------------------------------------------
# O que o painel lê: a herança precisa ser visível
# ---------------------------------------------------------------------------


def test_restaurante_sem_regra_responde_none(db):
    """`source: "none"` não é o mesmo que `enabled: false`.

    Um é "ninguém configurou", o outro é "configurado e desligado". Os dois
    caem em SEM_CASHBACK no checkout, mas só o segundo tem números na tela.
    """
    restaurante = criar_restaurante(db)

    vista = AdminCashbackService(db).get_restaurant_rule(escopo(restaurante))

    assert vista.source == "none"
    assert vista.rule is None


def test_filial_sem_sobrescrita_le_a_regra_da_rede_marcada_como_herdada(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    service = AdminCashbackService(db)
    service.replace_restaurant_rule(escopo(restaurante), corpo(default_percent=Decimal("5.00")))

    vista = service.get_branch_rule(escopo(restaurante), filial.id)

    assert vista.source == "restaurant"
    assert vista.rule.default_percent == Decimal("5.00")
    # O `branch_id` nulo é a segunda prova de que a linha é a da rede: o
    # painel que confiar só no `source` continua certo, e o que olhar a linha
    # também.
    assert vista.rule.branch_id is None


def test_filial_com_sobrescrita_le_a_propria_marcada_como_propria(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    service = AdminCashbackService(db)
    service.replace_restaurant_rule(escopo(restaurante), corpo(default_percent=Decimal("5.00")))
    service.replace_branch_rule(escopo(restaurante), filial.id, corpo(default_percent=Decimal("9.00")))

    vista = service.get_branch_rule(escopo(restaurante), filial.id)

    assert vista.source == "branch"
    assert vista.rule.default_percent == Decimal("9.00")
    assert vista.rule.branch_id == filial.id


def test_filial_sem_regra_nenhuma_em_lugar_nenhum_responde_none(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)

    vista = AdminCashbackService(db).get_branch_rule(escopo(restaurante), filial.id)

    assert vista.source == "none"
    assert vista.rule is None


# ---------------------------------------------------------------------------
# O que o painel escreve
# ---------------------------------------------------------------------------


def test_o_put_da_rede_e_upsert_e_nao_cria_segunda_linha(db):
    """O índice parcial `ux_cashback_rules_padrao_do_restaurante` proíbe duas.

    Se o `PUT` inserisse em vez de atualizar, o segundo salvamento da tela
    derrubaria a requisição com IntegrityError.
    """
    restaurante = criar_restaurante(db)
    service = AdminCashbackService(db)

    primeira = service.replace_restaurant_rule(escopo(restaurante), corpo(default_percent=Decimal("5.00")))
    segunda = service.replace_restaurant_rule(escopo(restaurante), corpo(default_percent=Decimal("7.00")))

    assert primeira.id == segunda.id
    assert segunda.default_percent == Decimal("7.00")


def test_o_put_da_filial_e_upsert_tambem(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    service = AdminCashbackService(db)

    primeira = service.replace_branch_rule(escopo(restaurante), filial.id, corpo())
    segunda = service.replace_branch_rule(
        escopo(restaurante), filial.id, corpo(default_percent=Decimal("8.00"))
    )

    assert primeira.id == segunda.id
    assert segunda.default_percent == Decimal("8.00")


def test_os_dias_saem_ordenados(db):
    """`cashback_rule_weekdays` não tem coluna de ordem (armadilha 14).

    Sem o `sorted` explícito a tela de configuração troca de ordem sozinha a
    cada leitura.
    """
    restaurante = criar_restaurante(db)
    service = AdminCashbackService(db)

    regra = service.replace_restaurant_rule(
        escopo(restaurante),
        corpo(
            weekdays=[
                {"weekday": 5, "percent": Decimal("2.00")},
                {"weekday": 0, "percent": Decimal("3.00")},
                {"weekday": 3, "percent": Decimal("4.00")},
            ]
        ),
    )

    assert [dia.weekday for dia in regra.weekdays] == [0, 3, 5]


def test_o_put_substitui_a_lista_de_dias_inteira(db):
    """Dia que sai do corpo perde a linha própria — e volta a `default_percent`.

    É a inversão deliberada em relação ao `PUT` de horários (armadilha 3),
    onde dia ausente significa dia FECHADO. Aqui ausente significa "vale o
    padrão", nunca zero.
    """
    restaurante = criar_restaurante(db)
    service = AdminCashbackService(db)
    service.replace_restaurant_rule(
        escopo(restaurante),
        corpo(
            weekdays=[
                {"weekday": 1, "percent": Decimal("10.00")},
                {"weekday": 2, "percent": Decimal("11.00")},
            ]
        ),
    )

    regra = service.replace_restaurant_rule(
        escopo(restaurante),
        corpo(default_percent=Decimal("5.00"), weekdays=[{"weekday": 1, "percent": Decimal("10.00")}]),
    )

    assert [dia.weekday for dia in regra.weekdays] == [1]


def test_dia_ausente_herda_o_padrao_e_nao_zero(db):
    """A metade do contrato que o painel precisa saber de cor.

    Conferida contra `resolve_cashback_terms`, que é quem o checkout chama —
    e não contra a linha gravada, que não responde esta pergunta.
    """
    from datetime import datetime, timezone

    from src.repositories.cashback_rule_repository import CashbackRuleRepository
    from src.services.cashback_rule import resolve_cashback_terms

    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    AdminCashbackService(db).replace_restaurant_rule(
        escopo(restaurante),
        corpo(default_percent=Decimal("5.00"), weekdays=[{"weekday": 1, "percent": Decimal("10.00")}]),
    )

    da_filial, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial.id
    )
    # 2026-08-24 é uma SEGUNDA (weekday 0), que não tem linha própria.
    segunda = resolve_cashback_terms(
        da_filial, do_restaurante, datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)
    )
    # 2026-08-25 é a terça configurada.
    terca = resolve_cashback_terms(
        da_filial, do_restaurante, datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
    )

    assert segunda.percent == Decimal("5.00")
    assert terca.percent == Decimal("10.00")


# ---------------------------------------------------------------------------
# Apagar a sobrescrita
# ---------------------------------------------------------------------------


def test_apagar_a_sobrescrita_devolve_a_filial_a_heranca(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    service = AdminCashbackService(db)
    service.replace_restaurant_rule(escopo(restaurante), corpo(default_percent=Decimal("5.00")))
    service.replace_branch_rule(escopo(restaurante), filial.id, corpo(default_percent=Decimal("9.00")))

    service.delete_branch_rule(escopo(restaurante), filial.id)

    vista = service.get_branch_rule(escopo(restaurante), filial.id)
    assert vista.source == "restaurant"
    assert vista.rule.default_percent == Decimal("5.00")


def test_apagar_sobrescrita_que_nao_existe_e_404(db):
    """"Voltou a herdar agora" e "já herdava" são estados diferentes na tela."""
    from fastapi import HTTPException

    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)

    with pytest.raises(HTTPException) as erro:
        AdminCashbackService(db).delete_branch_rule(escopo(restaurante), filial.id)

    assert erro.value.status_code == 404


def test_apagar_a_sobrescrita_leva_os_dias_junto(db):
    """`ondelete=CASCADE` mais `delete-orphan`. Dia órfão prenderia a linha."""
    from sqlalchemy import func, select

    from src.models.cashback_rule_model import CashbackRuleWeekday

    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    service = AdminCashbackService(db)
    regra = service.replace_branch_rule(
        escopo(restaurante),
        filial.id,
        corpo(weekdays=[{"weekday": 1, "percent": Decimal("10.00")}]),
    )

    service.delete_branch_rule(escopo(restaurante), filial.id)

    sobraram = db.scalar(
        select(func.count())
        .select_from(CashbackRuleWeekday)
        .where(CashbackRuleWeekday.rule_id == regra.id)
    )
    assert sobraram == 0


# ---------------------------------------------------------------------------
# Escopo: a filial no path não autoriza nada
# ---------------------------------------------------------------------------


def test_filial_de_outro_restaurante_e_404(db):
    """404 e não 403: um 403 confirmaria que aquela filial existe."""
    from fastapi import HTTPException

    meu = criar_restaurante(db)
    alheio = criar_restaurante(db, nome="Concorrente")
    filial_alheia = criar_filial(db, alheio)

    with pytest.raises(HTTPException) as erro:
        AdminCashbackService(db).get_branch_rule(escopo(meu), filial_alheia.id)

    assert erro.value.status_code == 404


def test_gerente_preso_a_uma_filial_nao_le_a_outra(db):
    """`ensure_branch_allowed`, o mesmo 404 de `AdminSettingsService`."""
    from fastapi import HTTPException

    restaurante = criar_restaurante(db)
    minha = criar_filial(db, restaurante)
    outra = criar_filial(db, restaurante, nome="Aldeota")

    with pytest.raises(HTTPException) as erro:
        AdminCashbackService(db).get_branch_rule(escopo(restaurante, minha), outra.id)

    assert erro.value.status_code == 404


def test_a_sobrescrita_de_uma_filial_nao_alcanca_a_outra(db):
    """Duas filiais, dois índices parciais, duas linhas independentes."""
    restaurante = criar_restaurante(db)
    centro = criar_filial(db, restaurante, nome="Centro")
    aldeota = criar_filial(db, restaurante, nome="Aldeota")
    service = AdminCashbackService(db)
    service.replace_restaurant_rule(escopo(restaurante), corpo(default_percent=Decimal("5.00")))

    service.replace_branch_rule(escopo(restaurante), centro.id, corpo(default_percent=Decimal("9.00")))

    assert service.get_branch_rule(escopo(restaurante), centro.id).source == "branch"
    assert service.get_branch_rule(escopo(restaurante), aldeota.id).source == "restaurant"


def test_filial_pode_sair_da_campanha_com_a_rede_ligada(db):
    """Sobrescrita própria desligada é o recurso, não o defeito.

    É como uma loja sai da campanha sem tirar a rede inteira.
    """
    from src.repositories.cashback_rule_repository import CashbackRuleRepository
    from src.services.cashback_rule import resolve_cashback_terms

    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    service = AdminCashbackService(db)
    service.replace_restaurant_rule(escopo(restaurante), corpo(enabled=True))
    service.replace_branch_rule(escopo(restaurante), filial.id, corpo(enabled=False))

    da_filial, do_restaurante = CashbackRuleRepository(db).get_rules_for_branch(
        restaurante.id, filial.id
    )
    termos = resolve_cashback_terms(da_filial, do_restaurante)

    assert termos.enabled is False
