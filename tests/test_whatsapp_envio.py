"""Mandar mensagem: o telefone em E.164 e a chamada ao Graph.

O que é dublado aqui é o **transporte** (o `httpx.Client` dentro do módulo), e
não a função de envio: dublar mais alto que o transporte testaria o dublê
(armadilha 42).

**A conversão do telefone não inventa.** `normalize_digits` deixa só dígitos, e
a armadilha 27 registra o resíduo: `+55 85 9...` vira `5585...` e `85 9...`
vira `859...` — os dois são o mesmo telefone escritos diferente, e o segundo
não tem DDI. A regra é fechada, e o que não casa **não é enviado**: chutar um
DDI é mandar a mensagem de um cliente para o telefone de outra pessoa.
"""

import unittest

import httpx
import pytest

from src.integrations.whatsapp_client import (
    WhatsAppRejectedError,
    WhatsAppTransportError,
    send_template_message,
    send_text_message,
)
from src.services.whatsapp_send_service import to_whatsapp_phone


TOKEN = "EAAG-token-de-sistema"
PNI = "1234567890"


class TestOTelefoneEmE164:
    @pytest.mark.parametrize(
        "digitado,esperado",
        [
            ("85999999999", "5585999999999"),
            ("(85) 99999-9999", "5585999999999"),
            ("+55 85 99999-9999", "5585999999999"),
            ("5585999999999", "5585999999999"),
            # Fixo, com e sem DDI. O número pode não ter WhatsApp, e quem
            # responde isso é a Meta — não nós, chutando.
            ("8533334444", "558533334444"),
            ("558533334444", "558533334444"),
        ],
    )
    def test_as_formas_que_o_checkout_produz(self, digitado: str, esperado: str) -> None:
        assert to_whatsapp_phone(digitado) == esperado

    @pytest.mark.parametrize(
        "digitado",
        [
            None,
            "",
            "99999999",  # sem DDD
            "999999999",  # nove dígitos: nem telefone com DDD, nem sem
            "1185999999999",  # 13 dígitos que NÃO começam com 55
            "005585999999999",
        ],
    )
    def test_o_que_nao_da_para_afirmar_nao_e_enviado(self, digitado: str | None) -> None:
        assert to_whatsapp_phone(digitado) is None


class _RespostaFalsa:
    def __init__(self, corpo: dict, status_code: int = 200) -> None:
        self._corpo = corpo
        self.status_code = status_code
        self.text = str(corpo)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "erro", request=httpx.Request("POST", "https://graph.facebook.com"), response=self
            )

    def json(self) -> dict:
        return self._corpo


class _ClienteFalso:
    """Substitui `httpx.Client` dentro do módulo. Guarda a última chamada."""

    resposta: _RespostaFalsa | None = None
    erro: Exception | None = None
    chamadas: list[dict] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_ClienteFalso":
        return self

    def __exit__(self, *args) -> None:
        return None

    def post(self, url: str, json: dict, headers: dict) -> _RespostaFalsa:
        type(self).chamadas.append({"url": url, "json": json, "headers": headers})
        if type(self).erro is not None:
            raise type(self).erro
        return type(self).resposta


class _ComTransporteFalso(unittest.TestCase):
    def setUp(self) -> None:
        from unittest.mock import patch

        _ClienteFalso.chamadas = []
        _ClienteFalso.erro = None
        _ClienteFalso.resposta = _RespostaFalsa(
            {"messages": [{"id": "wamid.ENVIADA"}]}
        )
        patcher = patch("src.integrations.whatsapp_client.httpx.Client", _ClienteFalso)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def chamada(self) -> dict:
        return _ClienteFalso.chamadas[-1]


class AChamadaAoGraphTests(_ComTransporteFalso):
    def test_o_template_vai_com_nome_idioma_e_parametros(self) -> None:
        wamid = send_template_message(
            access_token=TOKEN,
            phone_number_id=PNI,
            to="5585999999999",
            template_name="pedido_aceito",
            language="pt_BR",
            parameters=("Maria", "5471"),
        )

        self.assertEqual(wamid, "wamid.ENVIADA")
        corpo = self.chamada["json"]
        self.assertEqual(corpo["type"], "template")
        self.assertEqual(corpo["to"], "5585999999999")
        self.assertEqual(corpo["template"]["name"], "pedido_aceito")
        self.assertEqual(corpo["template"]["language"], {"code": "pt_BR"})
        self.assertEqual(
            corpo["template"]["components"],
            [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Maria"},
                        {"type": "text", "text": "5471"},
                    ],
                }
            ],
        )

    def test_a_url_leva_a_versao_e_o_phone_number_id(self) -> None:
        send_text_message(
            access_token=TOKEN, phone_number_id=PNI, to="5585999999999", body="oi"
        )

        self.assertTrue(self.chamada["url"].endswith(f"/{PNI}/messages"))
        self.assertIn("/v", self.chamada["url"])

    def test_o_token_vai_no_authorization_e_nao_na_url(self) -> None:
        """Token em query string entra em log de proxy e em histórico. O da
        Meta é credencial do lojista."""
        send_text_message(
            access_token=TOKEN, phone_number_id=PNI, to="5585999999999", body="oi"
        )

        self.assertEqual(self.chamada["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, self.chamada["url"])

    def test_o_texto_livre_vai_como_type_text(self) -> None:
        send_text_message(
            access_token=TOKEN, phone_number_id=PNI, to="5585999999999", body="tudo certo"
        )

        corpo = self.chamada["json"]
        self.assertEqual(corpo["type"], "text")
        self.assertEqual(corpo["text"], {"body": "tudo certo"})


class OErroDaMetaTests(_ComTransporteFalso):
    def test_recusa_da_meta_traz_o_codigo_e_nao_e_retentavel(self) -> None:
        _ClienteFalso.resposta = _RespostaFalsa(
            {"error": {"code": 132001, "message": "Template name does not exist"}},
            status_code=400,
        )

        with self.assertRaises(WhatsAppRejectedError) as erro:
            send_template_message(
                access_token=TOKEN,
                phone_number_id=PNI,
                to="5585999999999",
                template_name="pedido_aceito",
                language="pt_BR",
                parameters=("Maria", "5471"),
            )

        self.assertEqual(erro.exception.error_code, "132001")
        self.assertFalse(erro.exception.retryable)

    def test_queda_de_rede_e_retentavel(self) -> None:
        """`retryable` sai do TIPO da exceção, nunca do código — a mesma regra
        da armadilha 49."""
        _ClienteFalso.erro = httpx.ConnectError("sem rede")

        with self.assertRaises(WhatsAppTransportError) as erro:
            send_text_message(
                access_token=TOKEN, phone_number_id=PNI, to="5585999999999", body="oi"
            )

        self.assertTrue(erro.exception.retryable)

    def test_resposta_sem_wamid_nao_vira_envio_bem_sucedido(self) -> None:
        """Sem `wamid` não há como o webhook de status achar a linha depois.
        Chamar isso de sucesso gravaria uma mensagem que ninguém consegue
        acompanhar."""
        _ClienteFalso.resposta = _RespostaFalsa({"messages": []})

        with self.assertRaises(WhatsAppRejectedError):
            send_text_message(
                access_token=TOKEN, phone_number_id=PNI, to="5585999999999", body="oi"
            )
