"""O rotulo de origem e o lote do funil, antes de qualquer banco.

Tres coisas se protegem aqui, e nenhuma delas precisa de Postgres:

1. **O rotulo normaliza para UMA forma.** `qr-mesa-4`, `QR mesa 04` e
   `qrmesa4` sao a mesma mesa; sem isso o relatorio ganha uma linha por jeito
   de escrever, e o defeito so aparece depois de os imas estarem impressos.
2. **Rotulo ruim nunca vira erro.** Ele vira `direct`. Recusar transformaria
   um QR impresso com defeito em pedido perdido.
3. **O par de listas da armadilha 15.** `MENU_EVENT_TYPES` espelha o CHECK do
   banco; este arquivo prova a metade do schema, e
   `test_funil_do_cardapio_db.py` prova a do banco.
4. **`source` fica FORA da impressao digital da idempotencia.** E a metade
   menos obvia da frente inteira, e a que custaria 24h de 409 em producao se
   quebrasse. Ver `ImpressaoDigitalTests`.
"""

import unittest

from pydantic import ValidationError

from src.core.constants import (
    DEFAULT_TRAFFIC_SOURCE,
    MAX_TRAFFIC_SOURCE_LENGTH,
    MENU_EVENT_TYPES,
)
from src.schemas.menu_event_schema import (
    MAX_EVENTS_PER_BATCH,
    MAX_SESSION_ID_LENGTH,
    MenuEventBatchRequest,
)
from src.schemas.order_schema import CreateOrderRequest
from src.services.order_service import OrderService
from src.utils.normalization import normalize_traffic_source


UM_UUID = "11111111-1111-1111-1111-111111111111"


def _lote(**campos):
    corpo = {
        "restaurant_id": UM_UUID,
        "branch_id": UM_UUID,
        "session_id": "sessao-de-teste",
        "events": [{"event_type": "menu_view"}],
    }
    corpo.update(campos)
    return MenuEventBatchRequest(**corpo)


class RotuloDeOrigemTests(unittest.TestCase):
    def test_maiuscula_espaco_e_acento_caem_na_mesma_forma(self):
        """As tres grafias que o mesmo lojista usa em tres lugares."""
        for escrito in ("QR Mesa 04", "qr mesa 04", "  QR-MESA-04  "):
            with self.subTest(escrito=escrito):
                self.assertEqual(normalize_traffic_source(escrito), "qr-mesa-04")

    def test_acento_nao_vira_linha_separada(self):
        """Armadilha 31 pelo lado do relatorio: composto e decomposto sao a
        mesma origem, e `slugify` colapsa os dois de graca."""
        composto = "promoção"
        decomposto = "promoc\u0327ão"

        self.assertEqual(
            normalize_traffic_source(composto),
            normalize_traffic_source(decomposto),
        )

    def test_ausente_vazio_e_impronunciavel_viram_direct(self):
        for entrada in (None, "", "   ", "🍖", "---"):
            with self.subTest(entrada=entrada):
                self.assertEqual(normalize_traffic_source(entrada), DEFAULT_TRAFFIC_SOURCE)

    def test_rotulo_longo_e_cortado_sem_sobrar_hifen(self):
        """O corte no teto nao pode deixar o rotulo terminando em traco:
        `promo-` e `promo` seriam duas linhas do relatorio."""
        longo = "promocao-" * 10

        rotulo = normalize_traffic_source(longo)

        self.assertLessEqual(len(rotulo), MAX_TRAFFIC_SOURCE_LENGTH)
        self.assertFalse(rotulo.endswith("-"))

    def test_nunca_levanta(self):
        """A funcao inteira nao tem caminho de excecao, e e isso que permite
        chama-la no meio da criacao do pedido sem try."""
        for entrada in (None, "", "\x00", "a" * 5000, "../../etc/passwd"):
            with self.subTest(entrada=entrada):
                self.assertIsInstance(normalize_traffic_source(entrada), str)


class LoteDeEventosTests(unittest.TestCase):
    def test_lote_sem_source_grava_direct(self):
        """O caso mais comum de todos: entrou pelo link, sem parametro.

        Sem `validate_default=True` no campo, o validador nao roda e este
        lote levaria `None` para uma coluna NOT NULL.
        """
        self.assertEqual(_lote().source, DEFAULT_TRAFFIC_SOURCE)

    def test_source_e_normalizado_no_schema(self):
        self.assertEqual(_lote(source="QR Mesa 04").source, "qr-mesa-04")

    def test_todo_tipo_da_constante_e_aceito(self):
        """A metade de schema da armadilha 15. A do banco esta no teste `db`."""
        for tipo in MENU_EVENT_TYPES:
            with self.subTest(tipo=tipo):
                lote = _lote(events=[{"event_type": tipo}])
                self.assertEqual(lote.events[0].event_type, tipo)

    def test_tipo_desconhecido_e_recusado(self):
        with self.assertRaises(ValidationError):
            _lote(events=[{"event_type": "scrolled"}])

    def test_lote_vazio_e_recusado(self):
        """Requisicao sem evento nenhum e so custo: uma linha de log e uma
        conexao de banco para gravar nada."""
        with self.assertRaises(ValidationError):
            _lote(events=[])

    def test_lote_acima_do_teto_e_recusado_inteiro(self):
        """Recusado, nunca truncado: meio lote gravado e um funil
        silenciosamente errado, que e pior que o 422."""
        with self.assertRaises(ValidationError):
            _lote(events=[{"event_type": "cart_add"}] * (MAX_EVENTS_PER_BATCH + 1))

    def test_session_id_longo_e_recusado(self):
        """`session_id` vem do cliente. Sem teto, ele e a porta para encher a
        maior tabela do banco com texto arbitrario."""
        with self.assertRaises(ValidationError):
            _lote(session_id="s" * (MAX_SESSION_ID_LENGTH + 1))

    def test_campo_desconhecido_nao_derruba_o_lote(self):
        """`extra="ignore"`, ao contrario da avaliacao.

        A ultima leva sai por `sendBeacon` no fechamento da aba, onde nao ha
        ninguem para ler um 422 — e um front mais novo mandando um campo a
        mais nao pode custar a sessao inteira.
        """
        lote = _lote(schema_version=7)

        self.assertEqual(len(lote.events), 1)

    def test_instante_mandado_pelo_cliente_e_ignorado_sem_perder_o_lote(self):
        """O tempo e do servidor: um `occurred_at` no corpo seria o relogio do
        celular decidindo em que dia o evento cai.

        Ignorado e nao recusado, de proposito. Recusar perderia o lote inteiro
        num 422 que ninguem le — a ultima leva sai por `sendBeacon`, que nem
        entrega a resposta a pagina — e o campo ignorado nao muda resultado
        nenhum.
        """
        lote = _lote(events=[{"event_type": "menu_view", "occurred_at": "2027-01-01T00:00:00Z"}])

        self.assertEqual(len(lote.events), 1)
        self.assertFalse(hasattr(lote.events[0], "occurred_at"))


def _pedido(**campos):
    corpo = {
        "branch_id": UM_UUID,
        "customer": {"name": "Cliente", "phone": "85999999999"},
        "order_type": "pickup",
        "items": [{"product_id": UM_UUID, "quantity": 1}],
    }
    corpo.update(campos)
    return CreateOrderRequest(**corpo)


class ImpressaoDigitalTests(unittest.TestCase):
    """`source` nao entra no fingerprint da idempotencia.

    O fingerprint separa retry ("mesma chave, mesmo corpo": devolve a resposta
    gravada) de conflito ("mesma chave, corpo diferente": 409). Origem nao
    pertence a nenhum dos dois lados dessa pergunta — ela nao muda item,
    preco, endereco nem forma de pagamento.

    E ha o custo de deploy, que e a razao pratica: o fingerprint sai de
    `model_dump()`, entao QUALQUER campo novo no corpo muda o hash de todos.
    Uma chave reservada antes do deploy e retentada depois calcularia um hash
    diferente do gravado e receberia 409 por um pedido identico, pelas 24h de
    vida da chave. E a armadilha 7 pelo outro lado.
    """

    def test_a_origem_nao_muda_a_impressao_digital(self):
        sem_origem = OrderService._idempotency_fingerprint(_pedido())
        com_origem = OrderService._idempotency_fingerprint(_pedido(source="qr-mesa-04"))
        com_outra = OrderService._idempotency_fingerprint(_pedido(source="ima"))

        self.assertEqual(sem_origem, com_origem)
        self.assertEqual(sem_origem, com_outra)

    def test_o_corpo_assinado_nao_tem_a_chave_source(self):
        """A prova direta de que o corpo canonicalizado de um pedido sem
        origem e byte a byte o mesmo de antes desta revisao — que e o que faz
        as chaves em voo no deploy continuarem valendo."""
        corpo = _pedido(source="qr-mesa-04").model_dump(mode="json", exclude={"source"})

        self.assertNotIn("source", corpo)

    def test_mudanca_de_verdade_no_pedido_ainda_muda_a_impressao(self):
        """O outro lado: a exclusao vale so para `source`. Trocar o que foi
        PEDIDO continua sendo conflito, que e a resposta certa."""
        um = OrderService._idempotency_fingerprint(_pedido(source="qr-mesa-04"))
        outro = OrderService._idempotency_fingerprint(
            _pedido(source="qr-mesa-04", order_type="delivery")
        )

        self.assertNotEqual(um, outro)
