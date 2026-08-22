"""O saldo por restaurante — contra o Postgres.

Contra banco de verdade porque as três coisas que esta tela publica **são**
consultas: o `SUM(amount)` agrupado por restaurante, o `HAVING > 0` que tira
a loja já gasta da lista, e o `MAX(created_at)` do último pedido que dá a
data de validade. Dublar qualquer uma seria testar o dublê.

`2026-08-24` é uma SEGUNDA-feira, e é a data base de tudo aqui.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.models.cashback_rule_model import CashbackRule
from src.models.cashback_transaction_model import CashbackTransaction
from src.repositories.cashback_repository import CashbackRepository
from src.services.cashback_service import CashbackService
from tests.fabricas_db import (
    criar_cliente,
    criar_filial,
    criar_pedido,
    criar_restaurante,
)


pytestmark = pytest.mark.db

SEGUNDA = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)


def criar_regra(db, restaurante, *, expiry_days=60, enabled=True, filial=None):
    regra = CashbackRule(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial else None,
        enabled=enabled,
        default_percent=Decimal("5.00"),
        min_redeem_balance=Decimal("5.00"),
        expiry_days=expiry_days,
    )
    db.add(regra)
    db.flush()
    return regra


def creditar(db, cliente, restaurante, valor, *, tipo="earned"):
    """Uma linha no razão, sem passar pelo pedido.

    O crédito de verdade tem caminho próprio e teste próprio
    (`test_cashback_credito_e_resgate_db.py`); aqui o que está sob teste é a
    LEITURA, e montar um pedido concluído para cada linha só esconderia o
    que cada teste está dizendo.
    """
    linha = CashbackTransaction(
        customer_id=cliente.id,
        restaurant_id=restaurante.id if restaurante else None,
        type=tipo,
        amount=Decimal(valor),
        status="available",
        idempotency_key=f"teste:{uuid.uuid4()}",
    )
    db.add(linha)
    db.flush()
    return linha


def saldo(db, cliente):
    return CashbackService(db).get_balance(cliente)


# ---------------------------------------------------------------------------
# A quebra por restaurante
# ---------------------------------------------------------------------------


def test_saldo_de_cada_restaurante_sai_separado(db):
    """O motivo inteiro desta mudança de contrato.

    Somar R$ 40 do Júnior com R$ 2,50 da outra loja e publicar "R$ 42,50"
    faz a tela prometer um dinheiro que não se gasta junto: cashback é de
    quem o concedeu, e não há compensação entre restaurantes.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    varjota = criar_restaurante(db, "Varjota Burger")
    creditar(db, cliente, junior, "40.00")
    creditar(db, cliente, varjota, "2.50")

    resposta = saldo(db, cliente)

    assert resposta.balance == 42.50
    assert [(linha.restaurant_name, linha.balance) for linha in resposta.by_restaurant] == [
        ("Júnior da Picanha", 40.0),
        ("Varjota Burger", 2.5),
    ]


def test_o_resgate_entra_negativo_e_o_saldo_da_loja_e_a_soma(db):
    """O razão é assinado: não há lote, não há FIFO, não há linha partida.

    O resgate parcial é uma linha nova negativa, e o saldo da loja continua
    sendo `SUM(amount)`.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    creditar(db, cliente, junior, "40.00")
    creditar(db, cliente, junior, "-15.00", tipo="redeemed")

    resposta = saldo(db, cliente)

    assert resposta.balance == 25.0
    assert resposta.by_restaurant[0].balance == 25.0


def test_loja_gasta_ate_o_fim_sai_da_lista(db):
    """Saldo zero não é linha de tela: não há o que gastar nem o que mostrar.

    E é o `HAVING > 0` que garante isso — a loja continua no extrato, que é
    onde o histórico vive.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    varjota = criar_restaurante(db, "Varjota Burger")
    creditar(db, cliente, junior, "10.00")
    creditar(db, cliente, junior, "-10.00", tipo="redeemed")
    creditar(db, cliente, varjota, "2.50")

    resposta = saldo(db, cliente)

    assert [linha.restaurant_name for linha in resposta.by_restaurant] == ["Varjota Burger"]


def test_saldo_sem_restaurante_conta_no_total_e_nao_na_lista(db):
    """`restaurant_id` é nullable com `ON DELETE SET NULL`.

    Restaurante apagado deixa saldo órfão: não tem nome para mostrar nem
    cardápio onde gastar, então ele fica de fora da lista — mas continua no
    acumulado, que é o número que a pessoa perde ao excluir a conta.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    creditar(db, cliente, junior, "10.00")
    creditar(db, cliente, None, "7.00", tipo="adjustment")

    resposta = saldo(db, cliente)

    assert resposta.balance == 17.0
    assert [linha.balance for linha in resposta.by_restaurant] == [10.0]


def test_o_saldo_e_de_quem_pediu_e_nao_do_vizinho(db):
    cliente = criar_cliente(db)
    outro = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    creditar(db, cliente, junior, "10.00")
    creditar(db, outro, junior, "99.00")

    resposta = saldo(db, cliente)

    assert resposta.balance == 10.0
    assert resposta.by_restaurant[0].balance == 10.0


def test_cliente_sem_saldo_nenhum_recebe_lista_vazia(db):
    cliente = criar_cliente(db)

    resposta = saldo(db, cliente)

    assert resposta.balance == 0.0
    assert resposta.by_restaurant == []


# ---------------------------------------------------------------------------
# A validade — o relógio é o último pedido
# ---------------------------------------------------------------------------


def test_a_validade_conta_do_ultimo_pedido_daquele_restaurante(db):
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    filial = criar_filial(db, junior)
    criar_regra(db, junior, expiry_days=60)
    creditar(db, cliente, junior, "10.00")
    criar_pedido(db, junior, filial, cliente=cliente, status="completed", created_at=SEGUNDA)

    resposta = saldo(db, cliente)

    assert resposta.by_restaurant[0].expires_at == datetime(
        2026, 10, 23, 12, 0, tzinfo=timezone.utc
    )


def test_pedido_novo_empurra_a_validade_para_frente(db):
    """O ponto do mecanismo: pedir de novo renova o saldo inteiro.

    É por isso que a data tem que aparecer na tela — sem ela o cliente não
    tem como saber que um pedido devolve o prazo.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    filial = criar_filial(db, junior)
    criar_regra(db, junior, expiry_days=30)
    creditar(db, cliente, junior, "10.00")
    criar_pedido(db, junior, filial, cliente=cliente, status="completed", created_at=SEGUNDA)
    criar_pedido(
        db,
        junior,
        filial,
        cliente=cliente,
        status="preparing",
        created_at=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )

    resposta = saldo(db, cliente)

    assert resposta.by_restaurant[0].expires_at == datetime(
        2026, 9, 30, 12, 0, tzinfo=timezone.utc
    )


def test_pedido_que_nao_chegou_a_cozinha_nao_renova_nada(db):
    """`pending` é pedido que a loja ainda não aceitou, e `cancelled` é
    pedido que não aconteceu. Nenhum dos dois estende a validade do saldo."""
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    filial = criar_filial(db, junior)
    criar_regra(db, junior, expiry_days=30)
    creditar(db, cliente, junior, "10.00")
    criar_pedido(db, junior, filial, cliente=cliente, status="completed", created_at=SEGUNDA)
    criar_pedido(
        db,
        junior,
        filial,
        cliente=cliente,
        status="cancelled",
        created_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
    )
    criar_pedido(
        db,
        junior,
        filial,
        cliente=cliente,
        status="pending",
        created_at=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
    )

    resposta = saldo(db, cliente)

    assert resposta.by_restaurant[0].expires_at == datetime(
        2026, 9, 23, 12, 0, tzinfo=timezone.utc
    )


def test_pedido_em_outra_loja_nao_renova_a_primeira(db):
    """Um relógio por (cliente, restaurante).

    Pedir no Varjota não pode segurar o saldo do Júnior: são dois saldos e
    duas validades, e é justamente isso que a lista permite dizer.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    varjota = criar_restaurante(db, "Varjota Burger")
    filial_varjota = criar_filial(db, varjota)
    criar_regra(db, junior, expiry_days=30)
    criar_regra(db, varjota, expiry_days=30)
    creditar(db, cliente, junior, "10.00")
    creditar(db, cliente, varjota, "5.00")
    criar_pedido(
        db, varjota, filial_varjota, cliente=cliente, status="completed", created_at=SEGUNDA
    )

    resposta = saldo(db, cliente)

    por_loja = {linha.restaurant_name: linha.expires_at for linha in resposta.by_restaurant}
    assert por_loja["Júnior da Picanha"] is None
    assert por_loja["Varjota Burger"] == datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def test_restaurante_sem_regra_nao_expira_saldo(db):
    """Ninguém configurou prazo nenhum, e apagar dinheiro de cliente por
    ausência de configuração é o lado errado do erro."""
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    filial = criar_filial(db, junior)
    creditar(db, cliente, junior, "10.00")
    criar_pedido(db, junior, filial, cliente=cliente, status="completed", created_at=SEGUNDA)

    resposta = saldo(db, cliente)

    assert resposta.by_restaurant[0].expires_at is None


def test_a_validade_sai_da_regra_do_restaurante_e_nao_da_filial(db):
    """O saldo é do restaurante inteiro, então a validade dele também tem que
    ser: duas filiais com prazos diferentes dariam duas respostas para
    "quando este saldo vence", e o saldo é um só.

    A regra de filial continua mandando no que ela existe para mandar —
    quanto gera e a partir de quanto se resgata (`resolve_cashback_terms`).
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    filial = criar_filial(db, junior)
    criar_regra(db, junior, expiry_days=60)
    criar_regra(db, junior, expiry_days=1, filial=filial)
    creditar(db, cliente, junior, "10.00")
    criar_pedido(db, junior, filial, cliente=cliente, status="completed", created_at=SEGUNDA)

    resposta = saldo(db, cliente)

    assert resposta.by_restaurant[0].expires_at == datetime(
        2026, 10, 23, 12, 0, tzinfo=timezone.utc
    )


# ---------------------------------------------------------------------------
# O total continua sendo o total
# ---------------------------------------------------------------------------


def test_o_acumulado_e_o_que_se_perde_ao_excluir_a_conta(db):
    """A soma de todos os restaurantes tem um uso legítimo, e é este.

    A anonimização leva o saldo de todas as lojas de uma vez, então o número
    que a tela de confirmação mostra é o mesmo `get_available_balance`.
    """
    cliente = criar_cliente(db)
    junior = criar_restaurante(db, "Júnior da Picanha")
    varjota = criar_restaurante(db, "Varjota Burger")
    creditar(db, cliente, junior, "40.00")
    creditar(db, cliente, varjota, "2.50")

    resposta = saldo(db, cliente)

    assert resposta.balance == float(
        CashbackRepository(db).get_available_balance(cliente.id)
    )
