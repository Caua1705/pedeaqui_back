"""Como o texto da API chega ao agente.

O `requests` decide sozinho em que encoding decodificar uma resposta, e para
`text/*` **sem** `charset` ele aplica a regra velha do HTTP: ISO-8859-1.

A API hoje NAO cai nisso — ela responde `text/event-stream; charset=utf-8`,
porque o Starlette acrescenta o charset em todo media_type `text/*`. Estes
testes exercitam o cabecalho sem charset de proposito: e o que chegaria se um
proxy reescrevesse o content-type, e o custo de descobrir isso em producao e
uma comanda com "PraÃ§a" na bobina.

O que estes testes protegem:

1. **O stream e lido como UTF-8** aconteca o que acontecer com o cabecalho.
   SSE e UTF-8 por especificacao; nao existe caso em que forcar esteja
   errado.
2. **O caso normal continua normal**: com o charset declarado (o que a API
   manda de verdade), nada muda.
3. **O JSON das vias**, que e de onde sai o `content` que vai para a bobina e
   o `sector_name` que escolhe a impressora.
"""

import io
import json
import unittest

import requests

from print_agent.api_client import ApiClient


COMANDA = "1x Picanha à Moda\n1x Filé à Parmegiana\n1x Sortidão"


def make_response(body: bytes, content_type: str, status: int = 200):
    """Uma resposta do `requests` de verdade, com corpo cru e cabecalho.

    Montada na mao e nao com um dublê porque o defeito mora justamente na
    decisao de encoding do proprio `requests`: um dublê que devolvesse `str`
    pularia o unico passo que interessa.
    """
    response = requests.Response()
    response.status_code = status
    response.raw = io.BytesIO(body)
    response.headers["Content-Type"] = content_type
    # Quem preenche `encoding` numa resposta de verdade e o adapter, nao o
    # construtor. Sem esta linha o fixture ficaria com `encoding = None`, que
    # e um TERCEIRO comportamento (o requests devolve bytes crus) e nao o
    # ISO-8859-1 que a API real provoca — o teste passaria a exercitar um
    # caminho que nao existe em producao.
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


class FakeSession:
    def __init__(self, stream_response=None, json_response=None):
        self.stream_response = stream_response
        self.json_response = json_response
        self.requested = []
        self.bodies = []

    def post(self, url, **kwargs):
        self.requested.append(("POST", url))
        return self._ticket_response()

    def get(self, url, **kwargs):
        self.requested.append(("GET", url))
        return self.stream_response

    def request(self, method, url, **kwargs):
        self.requested.append((method, url))
        self.bodies.append(kwargs.get("json"))
        # `stream_ticket` passa por aqui (é um `_request`), e ele roda antes
        # de todo `open_stream`. Sem o ticket o teste do stream nem chega no
        # que quer testar.
        if self.json_response is None or url.endswith("/stream-ticket"):
            return self._ticket_response()
        return self.json_response

    def _ticket_response(self):
        return make_response(
            json.dumps({"ticket": "tkt-123", "access_token": "tok"}).encode("utf-8"),
            "application/json",
        )


class StreamEncodingTests(unittest.TestCase):
    def build_stream(self, payload: dict) -> list[str]:
        body = (
            "retry: 3000\n\n"
            "id: 2026-08-09T17:32:00+00:00\n"
            "event: order.created\n"
            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        ).encode("utf-8")

        session = FakeSession(
            stream_response=make_response(body, "text/event-stream")
        )
        client = ApiClient("https://api.exemplo.com", token="tok", session=session)
        return list(client.open_stream())

    def test_the_stream_is_decoded_as_utf8_even_without_the_charset(self):
        # Cabecalho sem charset (proxy que reescreveu, API que trocou de
        # framework): o requests escolheria ISO-8859-1 e "Praça" chegaria
        # como "PraÃ§a".
        lines = self.build_stream({"order": {"id": "abc"}, "setor": "Praça Quente"})

        self.assertTrue(any("Praça Quente" in line for line in lines))
        self.assertFalse(any("Ã" in line for line in lines))

    def test_the_event_json_parses_with_its_accents_intact(self):
        payload = {
            "type": "order.created",
            "order": {"id": "abc", "order_number": 5471, "status": "accepted"},
            "observacao": "Sem cebola, à parte",
        }

        lines = self.build_stream(payload)
        data = [line for line in lines if line.startswith("data: ")][0]

        self.assertEqual(json.loads(data[len("data: "):]), payload)

    def test_the_header_the_api_actually_sends_keeps_working(self):
        # `text/event-stream; charset=utf-8` e o que sai da API de verdade
        # (o Starlette poe o charset sozinho). Este e o caso NORMAL: o teste
        # existe para garantir que forcar UTF-8 no cliente nao brigou com o
        # cabecalho que ja estava certo.
        body = "data: {\"setor\": \"Praça Quente\"}\n\n".encode("utf-8")
        response = make_response(body, "text/event-stream; charset=utf-8")
        session = FakeSession(stream_response=response)
        client = ApiClient("https://api.exemplo.com", token="tok", session=session)

        lines = list(client.open_stream())

        self.assertIn("data: {\"setor\": \"Praça Quente\"}", lines)


class PrintJobsEncodingTests(unittest.TestCase):
    def test_the_comanda_arrives_with_its_accents(self):
        # Este e o texto que vai virar bytes ESC/POS. Mojibake aqui imprime
        # mojibake na bobina.
        body = json.dumps(
            {"jobs": [{"sector_name": "Praça Quente", "content": COMANDA}]},
            ensure_ascii=False,
        ).encode("utf-8")
        session = FakeSession(json_response=make_response(body, "application/json"))
        client = ApiClient("https://api.exemplo.com", token="tok", session=session)

        jobs = client.print_jobs("abc")

        self.assertEqual(jobs[0]["content"], COMANDA)
        self.assertEqual(jobs[0]["sector_name"], "Praça Quente")


class AgentReportingTests(unittest.TestCase):
    """As duas rotas que so contam o que esta acontecendo nesta maquina.

    Nenhuma delas imprime nada. Elas existem para o painel poder responder
    "o agente do Centro esta no ar?" sem alguem ligar para a loja.
    """

    def _client(self):
        session = FakeSession(
            json_response=make_response(b'{"is_online": true}', "application/json")
        )
        return ApiClient("https://api.exemplo.com", token="tok", session=session), session

    def test_the_heartbeat_sends_the_version(self):
        client, session = self._client()

        client.heartbeat("1.4.0")

        self.assertEqual(session.requested[-1][0], "POST")
        self.assertTrue(session.requested[-1][1].endswith("/admin/print-agent/heartbeat"))
        self.assertEqual(session.bodies[-1], {"agent_version": "1.4.0"})

    def test_the_printer_report_keeps_the_name_byte_for_byte(self):
        """O nome tem que casar com o do Windows exatamente. Um acento
        alterado no caminho faz o painel oferecer uma impressora que nao
        existe, e a via nao sai."""
        client, session = self._client()

        client.report_printers([("Impressora Cozinha Ação", True), ("PDF", False)])

        self.assertEqual(
            session.bodies[-1],
            {
                "printers": [
                    {"name": "Impressora Cozinha Ação", "is_default": True},
                    {"name": "PDF", "is_default": False},
                ]
            },
        )

    def test_an_empty_list_is_still_a_valid_report(self):
        """Maquina sem impressora instalada e um estado que o painel precisa
        poder mostrar — nao um erro."""
        client, session = self._client()

        client.report_printers([])

        self.assertEqual(session.bodies[-1], {"printers": []})


if __name__ == "__main__":
    unittest.main()
