"""Crédito, resgate e devolução — contra o Postgres.

Contra banco de verdade porque o saldo **é** uma consulta: `SUM(amount)` das
linhas `available` daquele restaurante. Testar isso com dublê seria testar a
soma que o dublê faz, não a que o cliente vê.

E porque a garantia final da idempotência é um índice
(`ux_cashback_transactions_idempotency_key`), não um `if`.

`2026-08-24` é SEGUNDA e `2026-08-25` é TERÇA — as datas aparecem quando o
percentual do dia entra na história.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.cashback_rule_model import CashbackRule, CashbackRuleWeekday
from src.repositories.cashback_repository import CashbackRepository
from src.services.cashback_service import CashbackService
from tests.fabricas_db import (
    criar_cliente,
    criar_filial,
    criar_pedido,
    criar_restaurante,
)


pytestmark = pytest.mark.db

TERCA = datetime(2026, 8, 25, 15, 0, tzinfo=timezone.utc)
SEGUNDA = datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc)


class Cenario:
    """Uma loja com campanha ligada, um cliente e uma forma de pagamento."""

    def __init__(
        self,
        db,
        *,
        enabled=True,
        percent="5.00",
        min_redeem_balance="5.00",
        earns_cashback=True,
        dias=(),
    ):
        self.db = db
        self.restaurante = criar_restaurante(db)
        self.filial = criar_filial(db, self.restaurante)
        self.cliente = criar_cliente(db)
        self.regra = CashbackRule(
            restaurant_id=self.restaurante.id,
            enabled=enabled,
            default_percent=Decimal(percent),
            min_redeem_balance=Decimal(min_redeem_balance),
            expiry_days=60,
        )
        db.add(self.regra)
        db.add(
            BranchPaymentMethod(
                branch_id=self.filial.id,
                payment_flow="delivery",
                method_type="cash",
                label="Dinheiro",
                enabled=True,
                earns_cashback=earns_cashback,
            )
        )
        db.flush()
        for dia, percentual in dias:
            db.add(
                CashbackRuleWeekday(
                    rule_id=self.regra.id, weekday=dia, percent=Decimal(percentual)
                )
            )
        db.flush()

    def pedido(self, *, base="100.00", concluido=True, cliente=True, created_at=TERCA, **campos):
        pedido = criar_pedido(
            self.db,
            self.restaurante,
            self.filial,
            cliente=self.cliente if cliente else None,
            status="completed" if concluido else "pending",
            created_at=created_at,
        )
        pedido.payment_method = campos.get("payment_method", "cash")
        pedido.payment_flow = campos.get("payment_flow", "delivery")
        pedido.commission_base_amount = Decimal(base)
        self.db.flush()
        return pedido

    @property
    def saldo(self) -> Decimal:
        return CashbackRepository(self.db).get_available_balance_for_restaurant(
            self.cliente.id, self.restaurante.id
        )


# ---------------------------------------------------------------------------
# Crédito
# ---------------------------------------------------------------------------


def test_pedido_concluido_credita_o_percentual_sobre_a_base_da_comissao(db):
    """A base é a da comissão, já congelada no pedido.

    Uma base, dois usos: taxa de entrega e taxa de serviço ficam de fora sem
    precisar ser mencionadas de novo, e as duas contas não têm como divergir.
    """
    cenario = Cenario(db, percent="5.00")
    pedido = cenario.pedido(base="100.00")

    creditado = CashbackService(db).credit_for_order(pedido)

    assert creditado == Decimal("5.00")
    assert cenario.saldo == Decimal("5.00")


def test_o_credito_grava_percentual_e_base_na_linha(db):
    """Sem eles o valor não é conferível depois — mesma razão de
    `commission_percent` estar gravado no pedido, e não recalculado."""
    cenario = Cenario(db, percent="5.00")
    pedido = cenario.pedido(base="100.00")

    CashbackService(db).credit_for_order(pedido)

    linha = CashbackRepository(db).get_by_idempotency_key(f"cashback:earn:{pedido.id}")
    assert linha.type == "earned"
    assert linha.status == "available"
    assert linha.restaurant_id == cenario.restaurante.id
    assert linha.metadata_ == {"percent": "5.00", "base": "100.00"}
    # A validade é do SALDO e conta do último pedido: preencher aqui criaria
    # uma segunda resposta para "quando isto vence".
    assert linha.expires_at is None


def test_creditar_duas_vezes_o_mesmo_pedido_nao_duplica(db):
    """O painel reenvia: clique duplo, retry de rede, replay de chave.

    Creditar de novo seria dinheiro criado do nada.
    """
    cenario = Cenario(db)
    pedido = cenario.pedido(base="100.00")
    servico = CashbackService(db)

    servico.credit_for_order(pedido)
    segunda_vez = servico.credit_for_order(pedido)

    assert segunda_vez == Decimal("0")
    assert cenario.saldo == Decimal("5.00")


def test_pedido_de_convidado_nao_credita(db):
    cenario = Cenario(db)
    pedido = cenario.pedido(cliente=False)

    assert CashbackService(db).credit_for_order(pedido) == Decimal("0")


def test_loja_sem_campanha_nao_credita(db):
    cenario = Cenario(db, enabled=False)
    pedido = cenario.pedido()

    assert CashbackService(db).credit_for_order(pedido) == Decimal("0")
    assert cenario.saldo == Decimal("0")


def test_forma_de_pagamento_marcada_para_nao_gerar_nao_credita(db):
    cenario = Cenario(db, earns_cashback=False)
    pedido = cenario.pedido()

    assert CashbackService(db).credit_for_order(pedido) == Decimal("0")


def test_forma_de_pagamento_sem_linha_na_filial_nao_credita(db):
    """A filial removeu o método depois do pedido.

    Ausência de configuração não é permissão para gastar o dinheiro do
    lojista — e o caso fica no log, que é o que torna diagnosticável um
    cashback que parou de sair.
    """
    cenario = Cenario(db)
    pedido = cenario.pedido(payment_method="pix", payment_flow="online")

    assert CashbackService(db).credit_for_order(pedido) == Decimal("0")


def test_o_percentual_e_o_do_dia_do_PEDIDO_e_nao_o_de_hoje(db):
    """Pedido de terça (10%) concluído na quarta (3%) paga os 10%.

    Quem prometeu 10% ao cliente foi a tela do checkout, na terça. O crédito
    acontece na conclusão, que é outro dia — e é justamente por isso que o
    instante do pedido é o que entra na conta.
    """
    cenario = Cenario(db, percent="3.00", dias=[(1, "10.00")])
    pedido = cenario.pedido(base="100.00", created_at=TERCA)

    assert CashbackService(db).credit_for_order(pedido) == Decimal("10.00")


def test_base_zerada_nao_gera_linha_nenhuma(db):
    """Pedido inteiramente coberto por cupom: 5% de zero é zero.

    Linha de valor zero só sujaria o extrato do cliente.
    """
    cenario = Cenario(db)
    pedido = cenario.pedido(base="0.00")

    assert CashbackService(db).credit_for_order(pedido) == Decimal("0")
    assert CashbackRepository(db).get_by_idempotency_key(f"cashback:earn:{pedido.id}") is None


# ---------------------------------------------------------------------------
# Resgate
# ---------------------------------------------------------------------------


def test_resgate_usa_o_menor_entre_saldo_e_teto(db):
    cenario = Cenario(db)
    CashbackService(db).credit_for_order(cenario.pedido(base="400.00"))  # 20,00

    quanto = CashbackService(db).amount_to_redeem(
        customer=cenario.cliente,
        restaurant_id=cenario.restaurante.id,
        branch_id=cenario.filial.id,
        momento=TERCA,
        teto=Decimal("12.00"),
    )

    assert quanto == Decimal("12.00")


def test_saldo_abaixo_do_minimo_nao_resgata_nem_parcialmente(db):
    """R$ 3 acumulados com mínimo de R$ 5: não há resgate nenhum.

    O mínimo é do SALDO, não do valor resgatado.
    """
    cenario = Cenario(db, min_redeem_balance="5.00")
    CashbackService(db).credit_for_order(cenario.pedido(base="60.00"))  # 3,00

    quanto = CashbackService(db).amount_to_redeem(
        customer=cenario.cliente,
        restaurant_id=cenario.restaurante.id,
        branch_id=cenario.filial.id,
        momento=TERCA,
        teto=Decimal("50.00"),
    )

    assert quanto == Decimal("0")


def test_o_resgate_derruba_o_saldo_com_uma_linha_negativa(db):
    """Sem lote e sem FIFO: o saldo é a soma, e o resgate parcial é uma
    linha a mais."""
    cenario = Cenario(db)
    CashbackService(db).credit_for_order(cenario.pedido(base="400.00"))  # 20,00
    outro_pedido = cenario.pedido(base="100.00", concluido=False)

    CashbackService(db).register_redemption(outro_pedido, Decimal("12.00"))

    assert cenario.saldo == Decimal("8.00")
    linha = CashbackRepository(db).get_by_idempotency_key(
        f"cashback:redeem:{outro_pedido.id}"
    )
    assert linha.type == "redeemed"
    assert linha.amount == Decimal("-12.00")


def test_o_saldo_e_por_restaurante(db):
    """Acumulou no Júnior, não gasta no vizinho.

    Cashback de um restaurante gasto em outro seria quem concedeu pagando o
    marketing do concorrente, e não há compensação entre eles.
    """
    junior = Cenario(db)
    CashbackService(db).credit_for_order(junior.pedido(base="400.00"))  # 20,00
    vizinho = Cenario(db)

    quanto = CashbackService(db).amount_to_redeem(
        customer=junior.cliente,
        restaurant_id=vizinho.restaurante.id,
        branch_id=vizinho.filial.id,
        momento=TERCA,
        teto=Decimal("50.00"),
    )

    assert quanto == Decimal("0")


# ---------------------------------------------------------------------------
# Devolução
# ---------------------------------------------------------------------------


def test_cancelar_devolve_o_cashback_resgatado(db):
    """Sem isto o cliente cancela e PERDE o saldo — o pior chamado possível."""
    cenario = Cenario(db)
    CashbackService(db).credit_for_order(cenario.pedido(base="400.00"))  # 20,00
    pedido = cenario.pedido(base="100.00", concluido=False)
    CashbackService(db).register_redemption(pedido, Decimal("12.00"))
    pedido.cashback_redeemed_amount = Decimal("12.00")
    db.flush()

    devolvido = CashbackService(db).refund_redemption(pedido)

    assert devolvido == Decimal("12.00")
    assert cenario.saldo == Decimal("20.00")


def test_a_devolucao_e_linha_nova_e_nao_apagamento(db):
    """O extrato do cliente precisa mostrar as duas pontas."""
    cenario = Cenario(db)
    CashbackService(db).credit_for_order(cenario.pedido(base="400.00"))
    pedido = cenario.pedido(base="100.00", concluido=False)
    CashbackService(db).register_redemption(pedido, Decimal("12.00"))
    pedido.cashback_redeemed_amount = Decimal("12.00")
    db.flush()

    CashbackService(db).refund_redemption(pedido)

    devolucao = CashbackRepository(db).get_by_idempotency_key(f"cashback:refund:{pedido.id}")
    assert devolucao.type == "cancelled"
    assert devolucao.amount == Decimal("12.00")
    resgate = CashbackRepository(db).get_by_idempotency_key(f"cashback:redeem:{pedido.id}")
    assert resgate is not None


def test_devolver_duas_vezes_nao_duplica(db):
    cenario = Cenario(db)
    CashbackService(db).credit_for_order(cenario.pedido(base="400.00"))
    pedido = cenario.pedido(base="100.00", concluido=False)
    CashbackService(db).register_redemption(pedido, Decimal("12.00"))
    pedido.cashback_redeemed_amount = Decimal("12.00")
    db.flush()

    CashbackService(db).refund_redemption(pedido)
    segunda_vez = CashbackService(db).refund_redemption(pedido)

    assert segunda_vez == Decimal("0")
    assert cenario.saldo == Decimal("20.00")


def test_pedido_que_nao_resgatou_nada_nao_consulta_o_razao(db):
    """A saída antecipada pelo valor gravado no pedido.

    Quase todo cancelamento é de pedido sem cashback, e sem ela cada um
    pagaria uma consulta para descobrir que não havia o que devolver.
    """
    cenario = Cenario(db)
    pedido = cenario.pedido(concluido=False)
    pedido.cashback_redeemed_amount = Decimal("0")
    db.flush()

    assert CashbackService(db).refund_redemption(pedido) == Decimal("0")
