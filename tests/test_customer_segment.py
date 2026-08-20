"""A escada de classificacao RFV, sem banco.

Nao leva marcador `db`: `classify_customer` e funcao pura sobre quatro
numeros que a listagem ja consulta.

O que estes testes protegem, em ordem de importancia:

1. **A cadencia e do cliente.** O caso que a frente inteira existe para
   resolver e o de baixo: semanal e mensal, o MESMO numero de dias em
   silencio, rotulos diferentes.
2. **Os dois grampos.** Piso e teto nao sao decoracao — sem eles a media crua
   produz "perdido no dia seguinte" e "fiel para sempre", e os dois casos sao
   de cardapio real, nao hipotese.
3. **A ordem da escada.** Recencia antes de contagem. Invertida, o cliente de
   doze pedidos sumido ha seis meses sai como "fiel" — e e exatamente ele que
   a reativacao precisa achar.
"""

from datetime import datetime, timedelta, timezone

from src.schemas.admin_customer_schema import CustomerSegment
from src.services.customer_segment import (
    cadence_days,
    classify_customer,
    days_since_last_order,
)


AGORA = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)


def _ha(dias: float) -> datetime:
    return AGORA - timedelta(days=dias)


def _classificar(orders_count: int, primeiro: datetime | None, ultimo: datetime | None):
    return classify_customer(
        orders_count=orders_count,
        first_order_at=primeiro,
        last_order_at=ultimo,
        now=AGORA,
    )


# Os dois clientes do enunciado, montados de tras para frente: o intervalo
# medido e (ultimo - primeiro) / (n - 1), entao o PRIMEIRO pedido e calculado
# a partir do silencio atual para a cadencia sair exata.
#
# Isso nao e preciosismo de fixture. Datas escolhidas a olho descrevem outro
# cliente: seis pedidos com o primeiro ha 150 dias e o ultimo ha 70 nao e um
# cliente mensal calado ha dois meses — e um cliente QUINZENAL que parou, e o
# rotulo certo para ele e outro.


def _semanal(silencio_de_dias: float):
    """Doze pedidos, um por semana. Cadencia medida: 77 / 11 = 7 dias."""
    return dict(
        orders_count=12,
        primeiro=_ha(silencio_de_dias + 77),
        ultimo=_ha(silencio_de_dias),
    )


def _mensal(silencio_de_dias: float):
    """Seis pedidos, um por mes. Cadencia medida: 150 / 5 = 30 dias."""
    return dict(
        orders_count=6,
        primeiro=_ha(silencio_de_dias + 150),
        ultimo=_ha(silencio_de_dias),
    )


# --------------------------------------------------------------------------
# 1. A cadencia e do proprio cliente
# --------------------------------------------------------------------------


def test_o_semanal_entra_em_risco_em_duas_semanas():
    """Cadencia de 7 dias: o dobro sao 14, e no dia 16 ele ja passou."""
    assert _classificar(**_semanal(16)) == CustomerSegment.EM_RISCO


def test_o_mensal_com_os_mesmos_dezesseis_dias_continua_fiel():
    """O MESMO silencio do teste de cima, e o rotulo e outro.

    E a frase que motivou a frente: quem pede uma vez por mes nao esta em
    risco por ter passado duas semanas. Uma faixa fixa nao consegue dizer
    isso sem errar o semanal — e uma faixa por RESTAURANTE tambem nao, porque
    estes dois clientes sao do mesmo restaurante.
    """
    assert _classificar(**_mensal(16)) == CustomerSegment.FIEL


def test_o_semanal_em_dia_e_fiel():
    assert _classificar(**_semanal(10)) == CustomerSegment.FIEL


def test_o_semanal_sumido_ha_um_mes_esta_perdido():
    assert _classificar(**_semanal(30)) == CustomerSegment.PERDIDO


def test_o_mensal_so_entra_em_risco_depois_de_dois_meses():
    assert _classificar(**_mensal(40)) == CustomerSegment.FIEL
    assert _classificar(**_mensal(70)) == CustomerSegment.EM_RISCO
    assert _classificar(**_mensal(130)) == CustomerSegment.PERDIDO


# --------------------------------------------------------------------------
# 2. Os grampos
# --------------------------------------------------------------------------


def test_dois_pedidos_no_mesmo_almoco_nao_viram_perdido_na_semana_seguinte():
    """Esqueceu a bebida e pediu de novo: intervalo bruto ~0.

    Sem o piso, a cadencia seria zero e QUALQUER silencio ultrapassaria o
    quadruplo dela — o cliente sairia como perdido no dia seguinte.
    """
    mesmo_dia = _ha(5)

    assert cadence_days(2, mesmo_dia, mesmo_dia) == 7
    assert _classificar(2, mesmo_dia, mesmo_dia) == CustomerSegment.NOVO


def test_dois_pedidos_separados_por_oito_meses_nao_ficam_fieis_para_sempre():
    """Cadencia crua de 240 dias, que o teto derruba para 60.

    Sem o teto, "em risco" chegaria so no dia 480 — ou seja, nunca.
    """
    assert cadence_days(2, _ha(260), _ha(20)) == 60
    assert _classificar(2, _ha(400), _ha(160)) == CustomerSegment.EM_RISCO


def test_quem_tem_um_pedido_so_nao_tem_intervalo_para_medir():
    """Nao ha o que dividir: vale a cadencia padrao."""
    assert cadence_days(1, _ha(3), _ha(3)) == 30

    assert _classificar(1, _ha(1), _ha(1)) == CustomerSegment.NOVO
    assert _classificar(1, _ha(100), _ha(100)) == CustomerSegment.EM_RISCO
    assert _classificar(1, _ha(200), _ha(200)) == CustomerSegment.PERDIDO


# --------------------------------------------------------------------------
# 3. A ordem da escada, e o buraco que "ocasional" fecha
# --------------------------------------------------------------------------


def test_recencia_vem_antes_da_contagem():
    """Doze pedidos nao salvam quem sumiu ha seis meses.

    Se a contagem fosse conferida primeiro, este cliente sairia "fiel" — e o
    gatilho de reativacao nunca o encontraria.
    """
    assert _classificar(12, _ha(260), _ha(180)) == CustomerSegment.PERDIDO


def test_dois_pedidos_espacados_em_dez_meses_nao_sao_um_cliente_novo():
    """O residuo que a quinta classe fechou.

    Pediu semana passada, entao esta em dia com o proprio ritmo; mas o
    relacionamento tem dez meses, e chamar isso de "novo" faz a tela mentir
    na primeira leitura.
    """
    assert _classificar(2, _ha(300), _ha(7)) == CustomerSegment.OCASIONAL


def test_novo_conta_do_primeiro_pedido():
    """Dois clientes com dois pedidos cada, os dois em dia. O que separa e a
    idade do relacionamento, e nao a do ultimo pedido."""
    assert _classificar(2, _ha(10), _ha(2)) == CustomerSegment.NOVO
    assert _classificar(2, _ha(45), _ha(2)) == CustomerSegment.OCASIONAL


def test_o_terceiro_pedido_e_o_que_vira_padrao():
    assert _classificar(2, _ha(40), _ha(5)) == CustomerSegment.OCASIONAL
    assert _classificar(3, _ha(40), _ha(5)) == CustomerSegment.FIEL


# --------------------------------------------------------------------------
# 4. Os dias, que aparecem na tela ao lado do rotulo
# --------------------------------------------------------------------------


def test_sem_pedido_nao_ha_dias_nem_classificacao_derivada_de_data():
    assert days_since_last_order(None, AGORA) is None
    assert _classificar(0, None, None) == CustomerSegment.NOVO


def test_relogio_adiantado_nao_produz_dias_negativos():
    """`created_at` e do banco, mas relogio adiantado em qualquer ponta daria
    "-1 dia sem pedir", que nao e uma frase que o painel consiga mostrar."""
    assert days_since_last_order(AGORA + timedelta(hours=5), AGORA) == 0


def test_os_dias_sao_inteiros_para_baixo():
    assert days_since_last_order(_ha(2.9), AGORA) == 2
