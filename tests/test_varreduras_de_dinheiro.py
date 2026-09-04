"""As duas varreduras de dinheiro saem com 0 — e o `--dry-run` não paga nada.

`scripts/estorna_pedidos_cancelados.py` e `scripts/cancela_pedidos_sem_pagamento.py`
rodam no container `estorno`, encadeadas:

    while true; do
      if python scripts/estorna_pedidos_cancelados.py && python scripts/cancela_pedidos_sem_pagamento.py; then
        sleep 900
      else
        sleep 300
      fi
    done

**Esse `&&` transforma o código de saída da primeira numa dependência da
segunda**, e o contrato que o sustenta está escrito em TRÊS lugares — os dois
docstrings e o comentário do `docker-compose.yml` — e cobrado em NENHUM. Até
aqui, `estorna_pedidos_cancelados.py` não era importado por teste nenhum do
repositório; o irmão era, mas só pelo `_cancel_one`, nunca pelo `main`.

Os dois estragos que a falta disso deixava passar, e são diferentes:

- **o laço gira para sempre.** Sair diferente de 0 porque um pedido não
  resolveu — restaurante sem credencial, prazo de estorno vencido no gateway —
  faz o container dormir 5 min em vez de 15 e voltar em cima do MESMO pedido
  irrecuperável, para sempre, gastando chamada paga a cada volta;
- **a segunda varredura para de rodar.** É o de segunda ordem, e é o caro: com
  o `&&`, um vermelho da primeira impede a segunda. E a segunda é a que tira
  do ar o pedido `pending`+`failed` que está **cobrando comissão do lojista,
  segurando o cupom e o cashback do cliente e travando a exclusão de conta**
  (armadilha 48). Uma falha do Mercado Pago passaria a parar um trabalho que
  não fala com o Mercado Pago.

Por isso a trava tem DUAS metades, e meia trava é pior que nenhuma porque
parece uma: que os dois `main` devolvem 0 com pendência, e que o compose
continua encadeando com `&&`. Trocar o `&&` por `;` sem saber disso é o outro
jeito de chegar no mesmo lugar — só que aí o certo é o `;`, e o que precisa
sumir é esta docstring.

E o `--dry-run`: é o que uma pessoa roda quando há dinheiro preso e ela quer
OLHAR antes de agir. Um dry-run que paga é a pior falha possível dessa
bandeira, e nada provava que ele não chama o gateway.

Colaborador é dublê solto (sessão, repositório, service); **pedido é o model
de verdade**, transiente, por `fabricas.pedido()` — CLAUDE.md.
"""

import uuid

import pytest

from scripts import cancela_pedidos_sem_pagamento, estorna_pedidos_cancelados
from src.services.payment_refund_service import (
    ACTION_FAILED,
    ACTION_REFUNDED,
    RefundOutcome,
)
from tests import fabricas


class SessaoFalsa:
    """O `with SessionLocal() as db:` dos dois scripts."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class RepositorioFalso:
    def __init__(self, pedidos, registro):
        self._pedidos = pedidos
        self._registro = registro

    def __call__(self, _db):
        return self

    def list_orders_awaiting_refund(self, since, limit):
        self._registro.append(("estorno", since, limit))
        return self._pedidos

    # A assinatura é a de verdade, `older_than` incluso: a carência de 30 min
    # é o que separa "cobrança recusada e abandonada" de "pix que o cliente
    # ainda vai pagar" (armadilha 48). Um dublê que aceitasse `**kwargs`
    # deixaria de acusar o dia em que ela sumisse da chamada.
    def list_orders_abandoned_after_payment_failure(self, older_than, since, limit):
        self._registro.append(("cancelamento", since, limit))
        return self._pedidos


def pedido_com_dinheiro_presa(**sobrescritas):
    return fabricas.pedido(
        id=uuid.uuid4(),
        status="cancelled",
        payment_flow="online",
        payment_status="paid",
        **sobrescritas,
    )


@pytest.fixture
def registro():
    return []


def montar_estorno(monkeypatch, pedidos, registro, outcome):
    monkeypatch.setattr(estorna_pedidos_cancelados, "SessionLocal", SessaoFalsa)
    monkeypatch.setattr(
        estorna_pedidos_cancelados, "OrderRepository", RepositorioFalso(pedidos, registro)
    )
    chamadas = []

    class ServicoFalso:
        def __init__(self, _db):
            pass

        def refund_terminal_order(self, order_id, restaurant_id):
            chamadas.append((order_id, restaurant_id))
            return outcome

    monkeypatch.setattr(estorna_pedidos_cancelados, "PaymentRefundService", ServicoFalso)
    return chamadas


# `RefundOutcome` de verdade, e não um `SimpleNamespace` com os mesmos três
# campos: ele é uma dataclass de `src/`, e dublá-lo é o que o CLAUDE.md
# proíbe. Escrevi o dublê solto primeiro e quem denunciou foi o
# `test_dubles_de_dado.py` — o tipo real ainda trouxe de graça as constantes
# de `action`, que os literais `"failed"`/`"refunded"` que eu tinha escrito
# não são: as de verdade são `falhou` e `estornado`, em português.
FALHOU = RefundOutcome(ACTION_FAILED, resolved=False, detail="gateway fora do ar")
ESTORNOU = RefundOutcome(ACTION_REFUNDED, resolved=True)


class TestOCodigoDeSaidaDoEstorno:
    def test_pedido_que_nao_resolve_ainda_sai_com_zero(self, monkeypatch, registro, capsys):
        """O gateway recusou e o dinheiro continua preso. Sair diferente de 0
        aqui faria o laço voltar em 5 min no mesmo pedido, para sempre."""
        montar_estorno(monkeypatch, [pedido_com_dinheiro_presa()], registro, FALHOU)
        monkeypatch.setattr("sys.argv", ["estorna_pedidos_cancelados.py"])

        assert estorna_pedidos_cancelados.main() == 0

    def test_o_pedido_preso_deixa_o_warning_com_o_numero(self, monkeypatch, registro, caplog):
        """Sair com 0 só é aceitável porque a pendência fica VISÍVEL. O número
        do pedido é por onde uma pessoa vai atrás dele no painel."""
        montar_estorno(
            monkeypatch, [pedido_com_dinheiro_presa(order_number=5471)], registro, FALHOU
        )
        monkeypatch.setattr("sys.argv", ["estorna_pedidos_cancelados.py"])

        with caplog.at_level("WARNING"):
            estorna_pedidos_cancelados.main()

        assert "5471" in caplog.text

    def test_fila_vazia_sai_com_zero(self, monkeypatch, registro):
        montar_estorno(monkeypatch, [], registro, ESTORNOU)
        monkeypatch.setattr("sys.argv", ["estorna_pedidos_cancelados.py"])

        assert estorna_pedidos_cancelados.main() == 0


class TestODryRunNaoPaga:
    def test_dry_run_nao_chama_o_service_de_estorno(self, monkeypatch, registro, capsys):
        """A bandeira que existe para OLHAR antes de agir. Se um dia
        `_report_dry_run` passar a compartilhar caminho com `_refund_all`,
        este teste é o que percebe."""
        chamadas = montar_estorno(
            monkeypatch, [pedido_com_dinheiro_presa()], registro, ESTORNOU
        )
        monkeypatch.setattr("sys.argv", ["estorna_pedidos_cancelados.py", "--dry-run"])

        assert estorna_pedidos_cancelados.main() == 0
        assert chamadas == []

    def test_sem_dry_run_ele_chama(self, monkeypatch, registro):
        """O par positivo: sem ele, o teste acima passaria com um script que
        não faz nada em modo nenhum."""
        pedido = pedido_com_dinheiro_presa()
        chamadas = montar_estorno(monkeypatch, [pedido], registro, ESTORNOU)
        monkeypatch.setattr("sys.argv", ["estorna_pedidos_cancelados.py"])

        estorna_pedidos_cancelados.main()

        assert chamadas == [(pedido.id, pedido.restaurant_id)]


class TestOQueChegaAoRepositorio:
    def test_o_teto_do_lote_e_a_janela_chegam_na_consulta(self, monkeypatch, registro):
        """`BATCH_LIMIT` existe para drenar fila grande em várias passadas —
        cada pedido custa uma ou duas chamadas pagas ao gateway. Se ele parar
        de ser passado, uma execução segura o container por meia hora."""
        montar_estorno(monkeypatch, [], registro, ESTORNOU)
        monkeypatch.setattr(
            "sys.argv", ["estorna_pedidos_cancelados.py", "--desde-dias", "7"]
        )

        estorna_pedidos_cancelados.main()

        (_, since, limit) = registro[0]
        assert limit == estorna_pedidos_cancelados.BATCH_LIMIT
        assert 6 < (estorna_pedidos_cancelados.utcnow() - since).days + 1 <= 8


class TestOEncadeamentoDoCompose:
    """As duas metades da mesma garantia. Uma sozinha não vale."""

    def test_o_irmao_tambem_sai_com_zero_com_pendencia(self, monkeypatch, registro):
        """A segunda varredura não fala com o gateway em chamada nenhuma, mas
        ela também não pode derrubar o laço: os dois motivos de sair 0 são o
        mesmo motivo."""
        monkeypatch.setattr(cancela_pedidos_sem_pagamento, "SessionLocal", SessaoFalsa)
        monkeypatch.setattr(
            cancela_pedidos_sem_pagamento,
            "OrderRepository",
            RepositorioFalso([], registro),
        )
        monkeypatch.setattr("sys.argv", ["cancela_pedidos_sem_pagamento.py"])

        assert cancela_pedidos_sem_pagamento.main() == 0

    def test_o_compose_continua_encadeando_as_duas_com_e_comercial(self):
        """A outra metade: o `&&` é o que torna o código de saída da primeira
        uma dependência da segunda.

        Se este teste cair porque alguém trocou o `&&` por `;`, a mudança pode
        estar certa — só que aí a docstring deste arquivo é que precisa mudar,
        e não o compose de volta."""
        from pathlib import Path

        compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
        texto = compose.read_text(encoding="utf-8")

        assert (
            "python scripts/estorna_pedidos_cancelados.py "
            "&& python scripts/cancela_pedidos_sem_pagamento.py" in texto
        )
