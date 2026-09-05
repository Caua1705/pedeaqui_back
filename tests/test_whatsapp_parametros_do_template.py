"""Quantos `{{n}}` o código manda, e quantos os templates aprovados declaram.

Os quatro templates foram aprovados na Meta com **duas** variáveis — `{{1}}` o
nome do cliente e `{{2}}` o número do pedido. O nome do restaurante saiu do
corpo: a mensagem chega pelo número da loja, então o próprio WhatsApp já diz de
quem é a conversa.

**Parâmetro a mais não é ignorado.** A Meta compara os componentes do envio com
os do template aprovado e recusa a mensagem inteira quando eles divergem — o
mesmo desfecho de não avisar ninguém, com a diferença de que ele aparece só na
resposta dela, num `error_code` que alguém precisa estar lendo. É o oposto do
erro da ORDEM (que a §5 do `docs/whatsapp.md` registra): trocar dois de lugar
manda o número do pedido onde vai o nome e **não dá erro nenhum**; mandar um a
mais dá erro em todo envio, sempre.

Por isso o número vive aqui como constante, e não como um `len()` do que o
código produz — um teste que contasse o que o próprio código monta concordaria
com ele qualquer que fosse o número.
"""

import unittest
from unittest.mock import patch

from src.integrations.whatsapp_client import send_template_message
from src.services.whatsapp_notification_service import (
    _TEMPLATE_POR_KIND,
    IDIOMA_DO_TEMPLATE,
    _parameters,
)
from tests import fabricas as fab


# O que a Meta aprovou, e é a razão de este arquivo existir. Mudar este
# número é mudar o template LÁ primeiro, e esperar a reaprovação.
VARIAVEIS_APROVADAS = 2

# Os nomes aprovados, em pt_BR, categoria Utilidade, sem cabeçalho, sem
# rodapé e sem botão.
TEMPLATES_APROVADOS = {
    "pedido_aceito",
    "pedido_pronto_para_retirada",
    "pedido_saiu_para_entrega",
    "pedido_entregue",
}


class OsParametrosDoAvisoTests(unittest.TestCase):
    def test_sao_dois_e_nessa_ordem(self) -> None:
        pedido = fab.pedido(customer_name_snapshot="Maria Aparecida", order_number=5471)

        self.assertEqual(_parameters(pedido), ("Maria", "5471"))

    def test_continuam_dois_com_o_nome_vazio(self) -> None:
        """Nome vazio devolve string vazia, e não um parâmetro a menos.

        A Meta aceita parâmetro vazio e recusa a CONTAGEM errada: sumir com o
        `{{1}}` de quem não tem nome no cadastro trocaria uma saudação feia
        por um cliente não avisado."""
        pedido = fab.pedido(customer_name_snapshot="", order_number=12)

        self.assertEqual(_parameters(pedido), ("", "12"))

    def test_a_contagem_bate_com_a_que_a_meta_aprovou(self) -> None:
        pedido = fab.pedido()

        self.assertEqual(len(_parameters(pedido)), VARIAVEIS_APROVADAS)


class OsNomesDosTemplatesTests(unittest.TestCase):
    def test_sao_os_quatro_aprovados(self) -> None:
        """`kind` é nosso; o nome do template é o da Meta. Esta é a única
        costura entre os dois, e um nome errado aqui é `132001` em todo
        envio daquele aviso."""
        self.assertEqual(set(_TEMPLATE_POR_KIND.values()), TEMPLATES_APROVADOS)

    def test_o_idioma_e_o_da_aprovacao(self) -> None:
        self.assertEqual(IDIOMA_DO_TEMPLATE, "pt_BR")


class _RespostaFalsa:
    status_code = 200

    @staticmethod
    def raise_for_status() -> None:
        return None

    @staticmethod
    def json() -> dict:
        return {"messages": [{"id": "wamid.ENVIADA"}]}


class _ClienteFalso:
    """Dublê do TRANSPORTE, e não da função de envio (armadilha 42)."""

    chamadas: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def post(self, url, **kwargs):
        type(self).chamadas.append(kwargs)
        return _RespostaFalsa()


class OCorpoQueChegaNaMetaTests(unittest.TestCase):
    """O que os `_parameters` viram na rede — porque é a divergência entre o
    corpo e o template aprovado que a Meta recusa, e não a tupla."""

    def setUp(self) -> None:
        _ClienteFalso.chamadas = []
        patcher = patch("src.integrations.whatsapp_client.httpx.Client", _ClienteFalso)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_o_body_leva_exatamente_as_variaveis_aprovadas(self) -> None:
        pedido = fab.pedido(customer_name_snapshot="Maria Aparecida", order_number=5471)

        send_template_message(
            access_token="EAAG-token",
            phone_number_id="1234567890",
            to="5585999999999",
            template_name="pedido_aceito",
            language=IDIOMA_DO_TEMPLATE,
            parameters=_parameters(pedido),
        )

        componentes = _ClienteFalso.chamadas[-1]["json"]["template"]["components"]
        self.assertEqual(len(componentes), 1)
        self.assertEqual(componentes[0]["type"], "body")
        self.assertEqual(
            componentes[0]["parameters"],
            [
                {"type": "text", "text": "Maria"},
                {"type": "text", "text": "5471"},
            ],
        )

    def test_nao_ha_cabecalho_nem_botao_no_corpo_enviado(self) -> None:
        """Os templates foram aprovados sem cabeçalho, sem rodapé e sem
        botão. Um componente a mais é a mesma recusa que uma variável a
        mais."""
        send_template_message(
            access_token="EAAG-token",
            phone_number_id="1234567890",
            to="5585999999999",
            template_name="pedido_entregue",
            language=IDIOMA_DO_TEMPLATE,
            parameters=_parameters(fab.pedido()),
        )

        tipos = [
            componente["type"]
            for componente in _ClienteFalso.chamadas[-1]["json"]["template"]["components"]
        ]
        self.assertEqual(tipos, ["body"])
