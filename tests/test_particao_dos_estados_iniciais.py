"""O pedido nasce num estado que o grafo CONHECE.

Irmão de `tests/test_particao_dos_status.py`. Aquele cobra que todo status
esteja classificado para o **faturamento**; este cobra que o status e o
`payment_status` de **nascimento** existam nas duas máquinas de estado.

## O buraco que isto fecha

`OrderService.create_order` grava o primeiro estado direto, sem passar por
`ensure_order_transition_allowed`. **Isso está certo** — nascimento não é
transição, não há aresta chegando em `pending`, e inventar uma só para o INSERT
passar abriria no grafo um caminho que nenhuma porta usa.

O que não havia era qualquer garantia de que o valor gravado fosse um estado
que o grafo conhece. Um pedido pode nascer num estado do qual não se sai, e o
sintoma disso não é erro: é um pedido parado para sempre, que o lojista não
consegue aceitar nem cancelar.

## Por que isto exigiu duas linhas em `src/`

A recomendação original dizia "1 teste, zero `src/`". Não dá: enquanto os
valores fossem literais dentro de `create_order`, o teste teria que repeti-los,
e um teste que repete o valor que ele deveria vigiar passa para sempre —
inclusive no commit que muda o literal. É a asserção tautológica da armadilha
52, na forma mais fácil de escrever sem perceber.

`INITIAL_ORDER_STATUS` e `INITIAL_PAYMENT_STATUS_BY_FLOW` moram em
`order_state_machine.py` (o vocabulário do grafo é dele) e são LIDOS por
`create_order`. É isso, e só isso, que dá dentes ao que está abaixo.

## O `if/else` que virou dicionário, e por que a troca importa

Era `"pending" if payment_flow == "online" else "on_delivery"`. A decisão por
EXCLUSÃO é a armadilha 47 no ponto mais caro possível: um `payment_flow` novo
cairia calado no `else`, o pedido nasceria como **pago na entrega** e iria
direto para a cozinha sem ninguém ter cobrado.

Com o dicionário ele levanta `KeyError` no primeiro pedido daquele fluxo —
barulhento, imediato, e no ambiente de quem está mexendo. `test_fluxo_novo_...`
abaixo é a prova de que essa é a falha, e não outra.
"""

import re
import unittest

import pytest
from sqlalchemy import text

from src.core.constants import ORDER_STATUSES, PAYMENT_STATUSES
from src.services.order_state_machine import (
    INITIAL_ORDER_STATUS,
    INITIAL_PAYMENT_STATUS_BY_FLOW,
    ORDER_STATUS_TRANSITIONS,
    PAYMENT_STATUS_TRANSITIONS,
    TERMINAL_ORDER_STATUSES,
)


#: O único `payment_status` inicial que é um SUMIDOURO no grafo, e é assim de
#: propósito: *"pago na entrega nunca muda: o pedido nasce e morre assim"*.
#:
#: Está escrito aqui, e não deduzido, porque é o contrário do que a intuição
#: pede. Sem esta linha, o teste abaixo cobraria "todo estado inicial tem
#: saída" e alguém abriria uma aresta a partir de `on_delivery` para deixá-lo
#: verde — abrindo, com ela, o caminho para um pedido pago na entrega virar
#: `refunded` sem nunca ter tido cobrança.
SUMIDOUROS_INICIAIS_DE_PROPOSITO = {"on_delivery"}


class TestOStatusDeNascimento(unittest.TestCase):
    def test_esta_em_ORDER_STATUSES(self):
        self.assertIn(INITIAL_ORDER_STATUS, ORDER_STATUSES)

    def test_e_um_estado_do_grafo(self):
        """Nascer fora do grafo é nascer sem transição nenhuma: nem
        `ensure_order_transition_allowed` sabe para onde deixar ir, e ele
        responde 409 `Status atual desconhecido`."""
        self.assertIn(INITIAL_ORDER_STATUS, ORDER_STATUS_TRANSITIONS)

    def test_nao_e_terminal(self):
        """O pedido tem que poder sair de onde nasceu.

        Terminal aqui seria um pedido que entra e não sai: o lojista não
        aceita, não recusa e não cancela, e a única saída é SQL na mão.
        """
        self.assertNotIn(INITIAL_ORDER_STATUS, TERMINAL_ORDER_STATUSES)


class TestOPaymentStatusDeNascimento(unittest.TestCase):
    def test_todo_fluxo_tem_um_estado_inicial(self):
        """Fluxo sem estado inicial é `KeyError` em `create_order`.

        A lista de fluxos que o banco aceita é conferida contra este
        dicionário no teste `db` abaixo — aqui só se garante que ele não está
        vazio nem meio preenchido.
        """
        self.assertTrue(INITIAL_PAYMENT_STATUS_BY_FLOW)
        for fluxo, inicial in INITIAL_PAYMENT_STATUS_BY_FLOW.items():
            self.assertTrue(fluxo, "fluxo sem nome")
            self.assertTrue(inicial, f"fluxo '{fluxo}' sem estado inicial")

    def test_todo_estado_inicial_esta_em_PAYMENT_STATUSES(self):
        for fluxo, inicial in INITIAL_PAYMENT_STATUS_BY_FLOW.items():
            with self.subTest(fluxo=fluxo):
                self.assertIn(inicial, PAYMENT_STATUSES)

    def test_todo_estado_inicial_e_um_estado_do_grafo(self):
        for fluxo, inicial in INITIAL_PAYMENT_STATUS_BY_FLOW.items():
            with self.subTest(fluxo=fluxo):
                self.assertIn(inicial, PAYMENT_STATUS_TRANSITIONS)

    def test_so_on_delivery_nasce_sem_saida(self):
        """O sumidouro é UM e é conhecido. Um segundo é a decisão que este
        teste existe para trazer à mesa: um fluxo cujo pagamento nasce e
        nunca muda é um fluxo em que o webhook não tem o que fazer."""
        sem_saida = {
            inicial
            for inicial in INITIAL_PAYMENT_STATUS_BY_FLOW.values()
            if not PAYMENT_STATUS_TRANSITIONS[inicial]
        }
        self.assertEqual(sem_saida, SUMIDOUROS_INICIAIS_DE_PROPOSITO)

    def test_fluxo_novo_levanta_em_vez_de_cair_no_lado_permissivo(self):
        """A prova de que a troca do `if/else` pelo dicionário vale.

        O `else` fazia o fluxo desconhecido nascer `on_delivery` — o pedido
        indo para a cozinha sem cobrança. Aqui ele levanta.
        """
        with self.assertRaises(KeyError):
            INITIAL_PAYMENT_STATUS_BY_FLOW["carteira-digital"]

        # E a MESMA leitura com o fluxo certo não levanta (armadilha 52: todo
        # `raises` novo confere que o caminho bom continua passando).
        self.assertEqual(INITIAL_PAYMENT_STATUS_BY_FLOW["online"], "pending")
        self.assertEqual(INITIAL_PAYMENT_STATUS_BY_FLOW["delivery"], "on_delivery")


@pytest.mark.db
class TestOsFluxosSaoOsDoBanco:
    """Armadilha 15, aplicada ao `payment_flow`.

    Isto **não** é uma declaração de espelho: `scripts/espelhos_de_enum.py`
    registra `ck_orders_payment_flow` como `SEM_ESPELHO`, com o motivo escrito
    (declarar a constante tornaria os dois literais visíveis para
    `filtros_por_exclusao.py`, que hoje não os vê), e nada aqui muda isso.

    O que se cobra é mais estreito e é uma consequência do dicionário: **todo
    fluxo que o banco aceita precisa ter um estado inicial.** Um valor novo no
    CHECK sem a linha correspondente aqui não é um enum divergente — é
    `KeyError` no primeiro pedido daquele fluxo, em produção.
    """

    @staticmethod
    def _valores_do_check(db, constraint: str) -> set[str]:
        definicao = db.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :nome"),
            {"nome": constraint},
        ).scalar_one()
        return set(re.findall(r"'([^']*)'::text", definicao))

    def test_todo_fluxo_de_orders_tem_estado_inicial(self, db):
        assert self._valores_do_check(db, "ck_orders_payment_flow") == set(
            INITIAL_PAYMENT_STATUS_BY_FLOW
        )

    def test_todo_fluxo_que_a_filial_oferece_tem_estado_inicial(self, db):
        """O par da anterior, na tabela que OFERECE o método.

        As duas listas divergirem entre si já seria defeito; o que este teste
        acrescenta é que a filial não consegue oferecer um fluxo que o pedido
        não saberia nascer.
        """
        assert self._valores_do_check(db, "branch_payment_methods_payment_flow_check") == set(
            INITIAL_PAYMENT_STATUS_BY_FLOW
        )


if __name__ == "__main__":
    unittest.main()
