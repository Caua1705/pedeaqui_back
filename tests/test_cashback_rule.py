"""Qual regra de cashback vale, e quanto ela gera no dia.

Duas coisas, e as duas erram calado se ninguém travar: **qual** das duas
linhas vale (a da filial ou a da rede) e **quanto** ela paga hoje.

Sem banco: a resolução é função pura de propósito — ela é chamada dentro do
checkout, e o teste dela não precisa de Postgres.

`2026-08-24` é uma SEGUNDA-feira, e é a data usada em todo lugar aqui.
"""

from datetime import datetime, timezone
from decimal import Decimal

from src.models.cashback_rule_model import CashbackRule, CashbackRuleWeekday
from src.services.cashback_rule import (
    SEM_CASHBACK,
    CashbackTerms,
    expires_at_from_last_order,
    resolve_cashback_terms,
)


SEGUNDA = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
TERCA = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def regra(
    *,
    enabled=True,
    default_percent="5.00",
    min_redeem_balance="5.00",
    expiry_days=60,
    branch_id=None,
    dias=(),
):
    linha = CashbackRule(
        enabled=enabled,
        default_percent=Decimal(default_percent),
        min_redeem_balance=Decimal(min_redeem_balance),
        expiry_days=expiry_days,
        branch_id=branch_id,
    )
    linha.weekdays = [
        CashbackRuleWeekday(weekday=dia, percent=Decimal(percentual))
        for dia, percentual in dias
    ]
    return linha


# ---------------------------------------------------------------------------
# Qual das duas linhas vale
# ---------------------------------------------------------------------------


def test_sem_regra_nenhuma_nao_ha_cashback():
    """Restaurante que nunca foi configurado não dá dinheiro por engano.

    É o oposto da comissão, que cai no padrão da plataforma quando falta
    linha de settings: lá o default protege a receita, aqui ele gastaria o
    dinheiro do lojista sem ele ter pedido.
    """
    assert resolve_cashback_terms(None, None, SEGUNDA) == SEM_CASHBACK


def test_sem_regra_de_filial_vale_a_do_restaurante():
    terms = resolve_cashback_terms(None, regra(default_percent="5.00"), SEGUNDA)

    assert terms.enabled
    assert terms.percent == Decimal("5.00")


def test_regra_da_filial_substitui_a_do_restaurante_INTEIRA():
    """Herança por linha: nada da regra da rede sobrevive na filial.

    É a diferença para o regime da revisão 20260818_0025, onde a herança é
    campo a campo. Aqui o saldo mínimo e a validade vêm da filial junto com o
    percentual — meia regra herdada não é explicável para o lojista.
    """
    da_rede = regra(default_percent="5.00", min_redeem_balance="5.00", expiry_days=60)
    da_filial = regra(default_percent="8.00", min_redeem_balance="20.00", expiry_days=30)

    terms = resolve_cashback_terms(da_filial, da_rede, SEGUNDA)

    assert terms == CashbackTerms(
        enabled=True,
        percent=Decimal("8.00"),
        min_redeem_balance=Decimal("20.00"),
        expiry_days=30,
    )


def test_filial_desligada_sai_da_campanha_da_rede():
    """A loja que não quer participar desliga a própria linha.

    Com herança por coluna isso não teria como ser dito: `enabled = NULL`
    significaria "herda", e a filial voltaria para a campanha.
    """
    terms = resolve_cashback_terms(regra(enabled=False), regra(enabled=True), SEGUNDA)

    assert terms == SEM_CASHBACK


def test_rede_desligada_nao_gera_nada():
    assert resolve_cashback_terms(None, regra(enabled=False), SEGUNDA) == SEM_CASHBACK


# ---------------------------------------------------------------------------
# Quanto gera no dia
# ---------------------------------------------------------------------------


def test_dia_sem_linha_propria_usa_o_percentual_padrao():
    """A regra mais importante do arquivo, e a que inverte a armadilha 3.

    Se dia ausente valesse zero, o lojista que cadastrasse SÓ a terça de 10%
    desligaria o cashback dos outros seis dias — sem erro e sem log, com a
    tela mostrando exatamente o que ele digitou.
    """
    so_a_terca = regra(default_percent="5.00", dias=[(1, "10.00")])

    terms = resolve_cashback_terms(None, so_a_terca, SEGUNDA)

    assert terms.percent == Decimal("5.00")


def test_dia_com_linha_propria_usa_o_percentual_do_dia():
    so_a_terca = regra(default_percent="5.00", dias=[(1, "10.00")])

    terms = resolve_cashback_terms(None, so_a_terca, TERCA)

    assert terms.percent == Decimal("10.00")


def test_zero_como_percentual_do_dia_e_uma_escolha_e_nao_ausencia():
    """`0.00` cadastrado no sábado significa sábado sem cashback.

    É a mesma distinção do `is not None` de `resolve_branch_operation`: a
    diferença é entre NULO e valor, nunca entre verdadeiro e falso. Um
    `or` aqui devolveria o padrão e pagaria 5% num dia que o lojista zerou.
    """
    sabado = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    com_sabado_zerado = regra(default_percent="5.00", dias=[(5, "0.00")])

    terms = resolve_cashback_terms(None, com_sabado_zerado, sabado)

    assert terms.percent == Decimal("0.00")


def test_a_semana_inteira_cadastrada_e_lida_dia_a_dia():
    semana = regra(
        default_percent="5.00",
        dias=[(0, "1.00"), (1, "10.00"), (2, "3.00"), (3, "4.00"), (4, "5.00"), (5, "3.00"), (6, "7.00")],
    )
    domingo = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    assert resolve_cashback_terms(None, semana, SEGUNDA).percent == Decimal("1.00")
    assert resolve_cashback_terms(None, semana, TERCA).percent == Decimal("10.00")
    assert resolve_cashback_terms(None, semana, domingo).percent == Decimal("7.00")


# ---------------------------------------------------------------------------
# O dia é o do balcão
# ---------------------------------------------------------------------------


def test_weekday_zero_e_segunda_e_nao_domingo():
    """Armadilha 1: o backend conta 0 = segunda, o `getDay()` do JS conta
    0 = domingo. Se o painel mandar o número do JS, a terça de 10% é gravada
    na segunda — e a tela mostra "terça", porque foi o que o lojista digitou.
    """
    so_o_dia_zero = regra(default_percent="5.00", dias=[(0, "9.00")])
    domingo = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)

    assert resolve_cashback_terms(None, so_o_dia_zero, SEGUNDA).percent == Decimal("9.00")
    assert resolve_cashback_terms(None, so_o_dia_zero, domingo).percent == Decimal("5.00")


def test_as_23h_de_segunda_em_belem_ainda_e_segunda():
    """UTC-3: 23h30 de segunda no balcão já é 02h30 de terça em UTC.

    Sem a conversão para o fuso da operação, a terça de 10% começaria três
    horas mais cedo todos os dias — e a segunda perderia as três últimas
    horas de movimento, que são justamente as de jantar.
    """
    tarde_da_segunda_em_belem = datetime(2026, 8, 25, 2, 30, tzinfo=timezone.utc)
    com_terca_dobrada = regra(default_percent="5.00", dias=[(1, "10.00")])

    terms = resolve_cashback_terms(None, com_terca_dobrada, tarde_da_segunda_em_belem)

    assert terms.percent == Decimal("5.00")


def test_datetime_ingenuo_e_lido_como_utc():
    """O container roda em UTC e a suíte roda no Windows de quem escreve.

    Tratar ingênuo como relógio da máquina faria o mesmo teste responder
    dias diferentes nos dois lugares.
    """
    ingenuo = datetime(2026, 8, 25, 2, 30)
    com_terca_dobrada = regra(default_percent="5.00", dias=[(1, "10.00")])

    terms = resolve_cashback_terms(None, com_terca_dobrada, ingenuo)

    assert terms.percent == Decimal("5.00")


# ---------------------------------------------------------------------------
# O resto dos termos
# ---------------------------------------------------------------------------


def test_saldo_minimo_e_validade_saem_da_regra_que_valeu():
    terms = resolve_cashback_terms(
        None,
        regra(min_redeem_balance="5.00", expiry_days=60),
        SEGUNDA,
    )

    assert terms.min_redeem_balance == Decimal("5.00")
    assert terms.expiry_days == 60


def test_desligado_nao_carrega_numero_nenhum():
    """Quem conferir só `percent` não pode achar que há campanha.

    Por isso os três caminhos de "sem cashback" devolvem o MESMO objeto: não
    existe estado meio preenchido para alguém ler por engano.
    """
    terms = resolve_cashback_terms(None, regra(enabled=False, default_percent="9.00"), SEGUNDA)

    assert terms.percent == Decimal("0")
    assert terms.min_redeem_balance == Decimal("0")
    assert terms.expiry_days == 0


# ---------------------------------------------------------------------------
# A validade — o relógio é o último pedido
# ---------------------------------------------------------------------------


def termos(expiry_days=60):
    return resolve_cashback_terms(None, regra(expiry_days=expiry_days), SEGUNDA)


def test_a_validade_conta_do_ultimo_pedido_e_nao_da_data_do_credito():
    """É a peça que dispensa expiração por lote.

    Um saldo, uma data. Sem isso voltariam FIFO, crédito partido ao meio e
    linhas vencendo em dias diferentes — tudo para responder a mesma
    pergunta.
    """
    assert expires_at_from_last_order(SEGUNDA, termos(expiry_days=60)) == datetime(
        2026, 10, 23, 12, 0, tzinfo=timezone.utc
    )


def test_pedido_mais_novo_empurra_a_data_para_frente():
    """O ponto inteiro do mecanismo: pedir de novo renova o saldo TODO.

    E é por isso que a data precisa aparecer na tela — o cliente não tem como
    descobrir sozinho que um pedido devolve o prazo.
    """
    antes = expires_at_from_last_order(SEGUNDA, termos())
    depois = expires_at_from_last_order(TERCA, termos())

    assert depois > antes
    assert (depois - antes).days == 1


def test_sem_pedido_nenhum_o_saldo_nao_vence():
    """Saldo sem pedido não tem de quando contar.

    Hoje só chega aqui o ajuste manual por SQL. Usar a data do crédito seria
    criar o segundo relógio que este desenho existe para não ter.
    """
    assert expires_at_from_last_order(None, termos()) is None


def test_restaurante_sem_campanha_nao_expira_saldo():
    """`SEM_CASHBACK` tem `expiry_days = 0`, e zero não é "vence hoje".

    Ninguém configurou prazo nenhum; apagar dinheiro de cliente por ausência
    de configuração é o lado errado do erro.
    """
    assert expires_at_from_last_order(SEGUNDA, SEM_CASHBACK) is None


def test_pedido_com_data_ingenua_e_lido_como_utc():
    """Mesmo motivo do dia da semana: o container é UTC e a suíte roda no
    Windows de quem escreve. Sem isso o mesmo teste daria horas diferentes
    nos dois lugares."""
    vencimento = expires_at_from_last_order(
        datetime(2026, 8, 24, 12, 0), termos(expiry_days=1)
    )

    assert vencimento == datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
