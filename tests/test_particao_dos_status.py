"""As listas positivas particionam o enum — sem sobra e sem sobreposição.

Este arquivo é a metade que faz o conserto da armadilha 47 ter dentes.

Trocar `status.notin_(NON_BILLABLE)` por `status.in_(BILLABLE)` move o estado
novo do lado permissivo para o restritivo, e isso já é ganho: um
`payment_status` novo deixa de nascer **cobrando comissão**. Mas ele passa a
nascer *fora de tudo*, e continua silencioso — ninguém decidiu nada, só o
default virou o outro.

**O que transforma esse silêncio em vermelho é a partição.** Cada valor de
`ORDER_STATUSES` e de `PAYMENT_STATUSES` tem que estar em exatamente uma das
duas listas. Um estado novo derruba estes testes no commit em que for criado,
e a mensagem diz qual é e onde ele precisa ser classificado — que é a decisão
que a negação tomava por omissão.

E há uma segunda camada, contra a armadilha 15: a partição vale sobre a
CONSTANTE, e a constante só significa alguma coisa se ela for a coluna. Os
testes marcados `db` conferem a constante contra o `CHECK` do Postgres depois
de `alembic upgrade head` — sem isso, um estado que existisse só no banco
ficaria fora das duas listas e ninguém notaria.

Três partições, e as três respondem perguntas DIFERENTES sobre os mesmos
status. Elas coincidem hoje e não são a mesma:

- "isto virou venda?" (`BILLABLE_ORDER_STATUSES`) — decide comissão;
- "o dinheiro entrou?" (`BILLABLE_PAYMENT_STATUSES`) — decide comissão também,
  pelo outro eixo;
- "esta pessoa já é cliente?" (`ORDER_STATUSES_THAT_COUNT_AS_PURCHASE`) —
  decide o cupom de primeira compra.
"""

import re
import unittest

import pytest
from sqlalchemy import text

from src.core.constants import ORDER_STATUSES, PAYMENT_STATUSES
from src.repositories.coupon_repository import ORDER_STATUSES_THAT_COUNT_AS_PURCHASE
from src.repositories.order_repository import (
    BILLABLE_ORDER_STATUSES,
    BILLABLE_PAYMENT_STATUSES,
    NON_BILLABLE_ORDER_STATUSES,
    NON_BILLABLE_PAYMENT_STATUSES,
)


# Os status que NÃO contam como compra do cliente. Mora aqui, e não numa
# constante do `src/`, porque nenhuma consulta precisa dele: o que existe é a
# lista positiva. O papel desta linha é ser o outro lado da partição, para um
# status novo aparecer na diferença e alguém ter que decidir.
NAO_CONTAM_COMO_COMPRA = {"cancelled", "rejected"}


class TestOFaturamentoClassificaTodoStatus(unittest.TestCase):
    """A comissão é dinheiro do lojista. Estado que não está em lista nenhuma
    é estado cujo faturamento ninguém decidiu."""

    def test_todo_status_de_pedido_esta_em_exatamente_uma_lista(self):
        faturaveis = set(BILLABLE_ORDER_STATUSES)
        nao_faturaveis = set(NON_BILLABLE_ORDER_STATUSES)

        self.assertEqual(faturaveis & nao_faturaveis, set(), "status nos DOIS lados")
        self.assertEqual(
            faturaveis | nao_faturaveis,
            set(ORDER_STATUSES),
            "status de ORDER_STATUSES sem lado: decida se ele vira venda",
        )

    def test_todo_payment_status_esta_em_exatamente_uma_lista(self):
        faturaveis = set(BILLABLE_PAYMENT_STATUSES)
        nao_faturaveis = set(NON_BILLABLE_PAYMENT_STATUSES)

        self.assertEqual(faturaveis & nao_faturaveis, set(), "payment_status nos DOIS lados")
        self.assertEqual(
            faturaveis | nao_faturaveis,
            set(PAYMENT_STATUSES),
            "payment_status de PAYMENT_STATUSES sem lado: decida se ele cobra",
        )

    def test_a_troca_para_a_forma_positiva_nao_mudou_o_conjunto(self):
        """A prova de que o conserto foi de FORMA e não de conteúdo.

        O que o `!=` produzia era "tudo menos o excluído". Se as listas
        positivas de hoje não forem exatamente isso, a base da comissão
        mudou — e mudar base de comissão não é refatoração.
        """
        self.assertEqual(
            set(BILLABLE_ORDER_STATUSES),
            set(ORDER_STATUSES) - set(NON_BILLABLE_ORDER_STATUSES),
        )
        self.assertEqual(
            set(BILLABLE_PAYMENT_STATUSES),
            set(PAYMENT_STATUSES) - {"refunded"},
        )


class TestOCupomDePrimeiraCompraClassificaTodoStatus(unittest.TestCase):
    """Aqui o erro não gera reclamação: o cliente não vê o cupom e não sabe
    que deveria ver. Só deixa de usar."""

    def test_todo_status_de_pedido_conta_ou_nao_conta_como_compra(self):
        contam = set(ORDER_STATUSES_THAT_COUNT_AS_PURCHASE)

        self.assertEqual(contam & NAO_CONTAM_COMO_COMPRA, set())
        self.assertEqual(
            set(ORDER_STATUSES) - contam,
            NAO_CONTAM_COMO_COMPRA,
            "status de ORDER_STATUSES sem lado: decida se ele conta como compra feita",
        )

    def test_a_troca_para_a_forma_positiva_nao_mudou_o_conjunto(self):
        self.assertEqual(
            set(ORDER_STATUSES_THAT_COUNT_AS_PURCHASE),
            set(ORDER_STATUSES) - {"cancelled", "rejected"},
        )

    def test_pending_conta_como_compra_de_proposito(self):
        """O pedido recém-criado conta. Tratá-lo como "não comprou" abriria a
        porta de usar o cupom de primeira compra duas vezes, fechando dois
        pedidos no mesmo minuto — e o pedido que morre sem pagamento vira
        `cancelled` em 30 minutos pela varredura da armadilha 48."""
        self.assertIn("pending", ORDER_STATUSES_THAT_COUNT_AS_PURCHASE)


@pytest.mark.db
class TestAConstanteEAColuna:
    """A partição vale sobre a constante; a constante só vale se for a coluna.

    É a armadilha 15 (`PAYMENT_METHODS` × o CHECK de
    `branch_payment_methods`) aplicada aos dois enums que decidem comissão:
    valor que exista só no banco fica fora das duas listas, e o WHERE positivo
    o deixa de fora do faturamento sem ninguém saber.
    """

    @staticmethod
    def _valores_do_check(db, constraint: str) -> set[str]:
        definicao = db.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :nome"),
            {"nome": constraint},
        ).scalar_one()
        return set(re.findall(r"'([^']*)'::text", definicao))

    def test_ORDER_STATUSES_e_o_check_de_orders_status(self, db):
        assert self._valores_do_check(db, "orders_status_check") == set(ORDER_STATUSES)

    def test_PAYMENT_STATUSES_e_o_check_de_orders_payment_status(self, db):
        assert self._valores_do_check(db, "ck_orders_payment_status") == set(PAYMENT_STATUSES)


if __name__ == "__main__":
    unittest.main()
