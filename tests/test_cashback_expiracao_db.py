"""A expiração do saldo — contra o Postgres.

Contra banco de verdade por três motivos, e os três são o mesmo motivo: aqui
se apaga dinheiro de cliente. O saldo **é** um `SUM` das linhas `available`,
quem sai da soma sai por `UPDATE ... SET status='expired'`, e o relógio é um
`MAX(created_at)` de `orders`. Dublar qualquer um seria testar o dublê
justamente onde o erro custa caro.

`2026-08-24` é uma SEGUNDA-feira, e é a data base de tudo aqui.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from scripts.expire_cashback import _expirar, _simular, _vencidos
from src.models.cashback_rule_model import CashbackRule
from src.models.cashback_transaction_model import CashbackTransaction
from src.repositories.cashback_repository import CashbackRepository
from src.repositories.cashback_rule_repository import CashbackRuleRepository
from src.services.cashback_service import CashbackService
from tests.fabricas_db import (
    criar_cliente,
    criar_filial,
    criar_pedido,
    criar_restaurante,
)


pytestmark = pytest.mark.db

SEGUNDA = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
# 60 dias depois do pedido, mais um: o saldo está vencido neste instante.
DEPOIS_DO_PRAZO = SEGUNDA + timedelta(days=61)


class Cenario:
    """Uma loja com campanha ligada, um cliente com saldo e um pedido."""

    def __init__(self, db, *, expiry_days=60, enabled=True, saldo="40.00", pedido_em=SEGUNDA):
        self.db = db
        self.restaurante = criar_restaurante(db)
        self.filial = criar_filial(db, self.restaurante)
        self.cliente = criar_cliente(db)
        db.add(
            CashbackRule(
                restaurant_id=self.restaurante.id,
                enabled=enabled,
                default_percent=Decimal("5.00"),
                min_redeem_balance=Decimal("5.00"),
                expiry_days=expiry_days,
            )
        )
        db.flush()
        if saldo is not None:
            self.creditar(saldo)
        if pedido_em is not None:
            self.pedir(pedido_em)

    def creditar(self, valor, *, tipo="earned"):
        linha = CashbackTransaction(
            customer_id=self.cliente.id,
            restaurant_id=self.restaurante.id,
            type=tipo,
            amount=Decimal(valor),
            status="available",
            idempotency_key=f"teste:{uuid.uuid4()}",
        )
        self.db.add(linha)
        self.db.flush()
        return linha

    def pedir(self, quando, *, status="completed"):
        return criar_pedido(
            self.db,
            self.restaurante,
            self.filial,
            cliente=self.cliente,
            status=status,
            created_at=quando,
        )

    def expirar(self, momento=DEPOIS_DO_PRAZO):
        return CashbackService(self.db).expire_balance(
            self.cliente.id, self.restaurante.id, momento
        )

    @property
    def saldo(self):
        return CashbackRepository(self.db).get_available_balance_for_restaurant(
            self.cliente.id, self.restaurante.id
        )

    def linhas(self):
        return (
            self.db.query(CashbackTransaction)
            .filter(CashbackTransaction.customer_id == self.cliente.id)
            .all()
        )


# ---------------------------------------------------------------------------
# O que vence
# ---------------------------------------------------------------------------


def test_saldo_parado_alem_do_prazo_vira_zero_de_uma_vez(db):
    """Não há FIFO nem lote: o saldo do par inteiro vence junto."""
    cenario = Cenario(db, saldo="40.00")

    vencido = cenario.expirar()

    assert vencido == Decimal("40.00")
    assert cenario.saldo == Decimal("0.00")


def test_as_linhas_saem_da_soma_sem_ter_valor_reescrito(db):
    """O `status` existe para tirar linha da soma de uma vez.

    O crédito de R$ 40 continua lá, com o valor que teve — o extrato precisa
    poder mostrar o que foi creditado um dia.
    """
    cenario = Cenario(db, saldo="40.00")

    cenario.expirar()

    creditos = [linha for linha in cenario.linhas() if linha.type == "earned"]
    assert [linha.amount for linha in creditos] == [Decimal("40.00")]
    assert [linha.status for linha in creditos] == ["expired"]


def test_entra_uma_linha_negativa_para_o_extrato_fechar(db):
    """Sem ela o extrato mostraria créditos somando R$ 40 e saldo zero, sem
    nada dizendo para onde o dinheiro foi."""
    cenario = Cenario(db, saldo="40.00")

    cenario.expirar()

    expiracoes = [linha for linha in cenario.linhas() if linha.type == "expired"]
    assert len(expiracoes) == 1
    assert expiracoes[0].amount == Decimal("-40.00")
    assert expiracoes[0].order_id is None


def test_a_linha_da_expiracao_nao_entra_no_saldo(db):
    """`status="expired"` na própria linha negativa, e não `available`.

    Com `available` ela seria a ÚNICA linha na soma, e o saldo do cliente
    ficaria NEGATIVO — uma dívida que ele descobriria no próximo pedido.
    """
    cenario = Cenario(db, saldo="40.00")

    cenario.expirar()

    assert cenario.saldo == Decimal("0.00")
    expiracao = [linha for linha in cenario.linhas() if linha.type == "expired"][0]
    assert expiracao.status == "expired"


def test_o_resgate_ja_feito_nao_e_devolvido_pela_expiracao(db):
    """O que vence é o SALDO, e o saldo já está líquido do que foi gasto."""
    cenario = Cenario(db, saldo="40.00")
    cenario.creditar("-15.00", tipo="redeemed")

    vencido = cenario.expirar()

    assert vencido == Decimal("25.00")
    assert cenario.saldo == Decimal("0.00")


# ---------------------------------------------------------------------------
# O que NÃO vence
# ---------------------------------------------------------------------------


def test_dentro_do_prazo_nao_vence_nada(db):
    cenario = Cenario(db, expiry_days=60, saldo="40.00")

    vencido = cenario.expirar(momento=SEGUNDA + timedelta(days=59))

    assert vencido == Decimal("0.00")
    assert cenario.saldo == Decimal("40.00")


def test_pedido_novo_salva_o_saldo_que_ia_vencer(db):
    """O ponto inteiro do mecanismo: pedir renova o saldo todo.

    O pedido de ontem empurra a validade sessenta dias para a frente, e a
    varredura de hoje não tem mais o que vencer.
    """
    cenario = Cenario(db, expiry_days=60, saldo="40.00")
    cenario.pedir(DEPOIS_DO_PRAZO - timedelta(days=1))

    vencido = cenario.expirar()

    assert vencido == Decimal("0.00")
    assert cenario.saldo == Decimal("40.00")


def test_pedido_que_nao_chegou_a_cozinha_nao_salva_o_saldo(db):
    """`pending` é pedido que a loja não aceitou. Ele não renova nada — e é o
    mesmo recorte que a tela de saldo usa para mostrar a data."""
    cenario = Cenario(db, expiry_days=60, saldo="40.00")
    cenario.pedir(DEPOIS_DO_PRAZO - timedelta(days=1), status="pending")

    vencido = cenario.expirar()

    assert vencido == Decimal("40.00")


def test_quem_nunca_pediu_naquela_loja_nao_vence(db):
    """Sem pedido não há de quando contar.

    Escolher a data do crédito seria criar o segundo relógio que este
    desenho existe para não ter — e o saldo aqui só chega por ajuste manual.
    """
    cenario = Cenario(db, saldo="40.00", pedido_em=None)

    vencido = cenario.expirar()

    assert vencido == Decimal("0.00")
    assert cenario.saldo == Decimal("40.00")


def test_campanha_desligada_congela_o_saldo_em_vez_de_vence_lo(db):
    """Loja que sai da campanha não apaga o que já prometeu.

    E a tela concorda: com a regra desligada ela responde `expires_at: null`.
    Se a varredura vencesse assim, o app teria mostrado "não vence" no dia
    anterior ao saldo sumir.
    """
    cenario = Cenario(db, enabled=False, saldo="40.00")

    vencido = cenario.expirar()

    assert vencido == Decimal("0.00")
    assert cenario.saldo == Decimal("40.00")


def test_o_saldo_de_outra_loja_nao_e_tocado(db):
    """Um relógio por (cliente, restaurante). Vencer no Júnior não pode
    encostar no saldo do Varjota."""
    cenario = Cenario(db, saldo="40.00")
    outra_loja = criar_restaurante(db, "Varjota Burger")
    db.add(
        CashbackTransaction(
            customer_id=cenario.cliente.id,
            restaurant_id=outra_loja.id,
            type="earned",
            amount=Decimal("7.00"),
            status="available",
            idempotency_key=f"teste:{uuid.uuid4()}",
        )
    )
    db.flush()

    cenario.expirar()

    repositorio = CashbackRepository(db)
    assert repositorio.get_available_balance_for_restaurant(
        cenario.cliente.id, outra_loja.id
    ) == Decimal("7.00")


def test_o_saldo_do_vizinho_nao_e_tocado(db):
    cenario = Cenario(db, saldo="40.00")
    vizinho = criar_cliente(db)
    db.add(
        CashbackTransaction(
            customer_id=vizinho.id,
            restaurant_id=cenario.restaurante.id,
            type="earned",
            amount=Decimal("9.00"),
            status="available",
            idempotency_key=f"teste:{uuid.uuid4()}",
        )
    )
    db.flush()

    cenario.expirar()

    assert CashbackRepository(db).get_available_balance_for_restaurant(
        vizinho.id, cenario.restaurante.id
    ) == Decimal("9.00")


# ---------------------------------------------------------------------------
# Rodar de novo
# ---------------------------------------------------------------------------


def test_a_segunda_execucao_nao_grava_nada(db):
    """O que impede a segunda gravação não é chave de idempotência: é não
    haver mais saldo `available` para vencer."""
    cenario = Cenario(db, saldo="40.00")
    cenario.expirar()

    de_novo = cenario.expirar()

    assert de_novo == Decimal("0.00")
    assert len([linha for linha in cenario.linhas() if linha.type == "expired"]) == 1


def test_saldo_lancado_depois_da_expiracao_ainda_pode_vencer(db):
    """O ajuste manual (só por SQL) cai num par que já venceu, sem mover o
    relógio.

    É por isso que a linha da expiração NÃO leva `idempotency_key` derivada
    da data: com ela, este saldo ficaria preso em `available` para sempre e
    sem erro em lugar nenhum.
    """
    cenario = Cenario(db, saldo="40.00")
    cenario.expirar()

    cenario.creditar("12.00", tipo="adjustment")
    vencido = cenario.expirar()

    assert vencido == Decimal("12.00")
    assert cenario.saldo == Decimal("0.00")


# ---------------------------------------------------------------------------
# A conta é a MESMA que a tela mostra
# ---------------------------------------------------------------------------


def test_a_data_que_a_tela_mostra_e_a_que_a_varredura_usa(db):
    """Duas contas de vencimento discordariam no dia em que uma fosse
    ajustada, e a divergência seria saldo apagado antes da data que o app
    prometeu."""
    cenario = Cenario(db, expiry_days=60, saldo="40.00")
    servico = CashbackService(db)

    da_tela = servico.get_balance(cenario.cliente).by_restaurant[0].expires_at
    da_varredura = servico.expires_at_for(cenario.cliente.id, cenario.restaurante.id)

    assert da_tela == da_varredura
    assert servico.expire_balance(
        cenario.cliente.id, cenario.restaurante.id, da_tela - timedelta(seconds=1)
    ) == Decimal("0.00")
    assert servico.expire_balance(
        cenario.cliente.id, cenario.restaurante.id, da_tela
    ) == Decimal("40.00")


def test_depois_de_vencer_a_tela_para_de_listar_a_loja(db):
    """Saldo zero sai de `by_restaurant[]` — não há o que gastar nem o que
    mostrar. O extrato continua com tudo."""
    cenario = Cenario(db, saldo="40.00")

    cenario.expirar()
    resposta = CashbackService(db).get_balance(cenario.cliente)

    assert resposta.balance == 0.0
    assert resposta.by_restaurant == []
    assert len(CashbackService(db).list_transactions(cenario.cliente, 20, 0).transactions) == 2


# ---------------------------------------------------------------------------
# A varredura do script
# ---------------------------------------------------------------------------


def regras_ligadas(db):
    return CashbackRuleRepository(db).list_enabled_restaurant_rules()


def test_a_varredura_so_carrega_quem_tem_saldo_vencido(db):
    """Três pessoas na mesma loja, e só uma vence.

    Quem pediu ontem está dentro do prazo e quem nunca pediu não tem relógio.
    Carregá-las seria pagar o `expire_balance` — com lock — de gente que não
    vai vencer nada.
    """
    cenario = Cenario(db, saldo="40.00")
    recente = criar_cliente(db)
    sem_pedido = criar_cliente(db)
    for cliente, quando in ((recente, DEPOIS_DO_PRAZO - timedelta(days=1)), (sem_pedido, None)):
        db.add(
            CashbackTransaction(
                customer_id=cliente.id,
                restaurant_id=cenario.restaurante.id,
                type="earned",
                amount=Decimal("5.00"),
                status="available",
                idempotency_key=f"teste:{uuid.uuid4()}",
            )
        )
        if quando is not None:
            criar_pedido(
                db,
                cenario.restaurante,
                cenario.filial,
                cliente=cliente,
                status="completed",
                created_at=quando,
            )
    db.flush()

    vencidos = _vencidos(db, regras_ligadas(db)[0], DEPOIS_DO_PRAZO)

    assert [(cid, saldo) for cid, saldo, _ in vencidos] == [
        (cenario.cliente.id, Decimal("40.00"))
    ]


def test_a_varredura_ignora_loja_com_campanha_desligada(db):
    """A regra desligada nem entra na lista que o script percorre."""
    Cenario(db, enabled=False, saldo="40.00")

    assert regras_ligadas(db) == []


def test_a_execucao_zera_o_saldo_vencido_e_deixa_o_resto(db):
    cenario = Cenario(db, saldo="40.00")
    poupado = criar_cliente(db)
    db.add(
        CashbackTransaction(
            customer_id=poupado.id,
            restaurant_id=cenario.restaurante.id,
            type="earned",
            amount=Decimal("5.00"),
            status="available",
            idempotency_key=f"teste:{uuid.uuid4()}",
        )
    )
    criar_pedido(
        db,
        cenario.restaurante,
        cenario.filial,
        cliente=poupado,
        status="completed",
        created_at=DEPOIS_DO_PRAZO - timedelta(days=1),
    )
    db.flush()

    assert _expirar(db, regras_ligadas(db), DEPOIS_DO_PRAZO) == 0

    repositorio = CashbackRepository(db)
    assert cenario.saldo == Decimal("0.00")
    assert repositorio.get_available_balance_for_restaurant(
        poupado.id, cenario.restaurante.id
    ) == Decimal("5.00")


def test_o_dry_run_nao_escreve_nada(db):
    """Ele existe porque aqui a escrita apaga dinheiro: dá para conferir a
    lista antes de deixar o laço diário rodar."""
    cenario = Cenario(db, saldo="40.00")

    assert _simular(db, regras_ligadas(db), DEPOIS_DO_PRAZO) == 0

    assert cenario.saldo == Decimal("40.00")
    assert [linha.type for linha in cenario.linhas()] == ["earned"]
