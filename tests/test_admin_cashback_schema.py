"""O que o contrato de escrita do cashback recusa, sem banco.

Os dois casos que dão dinheiro errado sem levantar erro em lugar nenhum são
`weekday` fora de 0..6 e percentual acima de 100 — o primeiro porque o painel
que mandar o `getDay()` do JavaScript manda `7` num domingo (armadilha 1), o
segundo porque `Numeric(5,2)` aceita `999.99` e o CHECK do banco viraria 500
em vez de 422.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.schemas.admin_cashback_schema import AdminCashbackRuleWrite


def regra(**mudancas):
    corpo = {
        "enabled": True,
        "default_percent": Decimal("5.00"),
        "min_redeem_balance": Decimal("10.00"),
        "expiry_days": 60,
    }
    corpo.update(mudancas)
    return AdminCashbackRuleWrite(**corpo)


def test_a_regra_minima_dispensa_weekdays():
    """`weekdays` ausente é a configuração comum: um percentual para a semana."""
    assert regra().weekdays == []


def test_o_dia_da_semana_vai_ate_seis():
    """0 = segunda, 6 = domingo. `7` é o `getDay()` do JS num domingo."""
    with pytest.raises(ValidationError):
        regra(weekdays=[{"weekday": 7, "percent": Decimal("10.00")}])


def test_o_dia_da_semana_nao_e_negativo():
    with pytest.raises(ValidationError):
        regra(weekdays=[{"weekday": -1, "percent": Decimal("10.00")}])


def test_percentual_acima_de_cem_e_recusado():
    """O CHECK do banco recusaria também — mas com 500, não com 422."""
    with pytest.raises(ValidationError):
        regra(default_percent=Decimal("101"))


def test_percentual_do_dia_acima_de_cem_e_recusado():
    with pytest.raises(ValidationError):
        regra(weekdays=[{"weekday": 1, "percent": Decimal("150")}])


def test_expiry_days_zero_e_recusado():
    """`> 0` é o CHECK da tabela. Zero seria "vence no mesmo instante"."""
    with pytest.raises(ValidationError):
        regra(expiry_days=0)


def test_saldo_minimo_negativo_e_recusado():
    with pytest.raises(ValidationError):
        regra(min_redeem_balance=Decimal("-1"))


def test_dia_repetido_e_recusado_com_o_dia_na_mensagem():
    """A PK é `(rule_id, weekday)`: sem isto o banco daria 500 sem dizer qual.

    Não é "vale o último": duas terças no corpo são erro do painel, e
    escolher uma delas silenciosamente grava um percentual que ninguém pediu.
    """
    with pytest.raises(ValidationError) as erro:
        regra(
            weekdays=[
                {"weekday": 1, "percent": Decimal("10.00")},
                {"weekday": 1, "percent": Decimal("20.00")},
            ]
        )

    assert "weekday repetido" in str(erro.value)
    assert "[1]" in str(erro.value)


def test_mais_de_sete_dias_e_recusado():
    """Sete dias na semana. Oito linhas é laço com bug do outro lado."""
    with pytest.raises(ValidationError):
        regra(
            weekdays=[
                {"weekday": dia % 7, "percent": Decimal("1.00")} for dia in range(8)
            ]
        )


def test_a_semana_inteira_configurada_passa():
    corpo = regra(
        weekdays=[{"weekday": dia, "percent": Decimal("3.00")} for dia in range(7)]
    )

    assert len(corpo.weekdays) == 7
