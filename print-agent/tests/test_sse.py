"""Leitura do protocolo SSE.

O parser recebe linhas e devolve eventos, sem rede: e o que permite provar o
tratamento do heartbeat e da linha em branco sem subir a API.

O que estes testes protegem:

1. `: ping` nao vira evento. Ele chega a cada 20 segundos a noite inteira;
   tratado como evento, o agente tentaria imprimir um comentario.
2. O `id:` continua valendo para os eventos seguintes. E ele que vira o
   `Last-Event-ID` da reconexao — zerado no lugar errado, o agente pediria
   o replay do lugar errado e perderia pedido.
3. `data:` quebrado nao derruba o agente.
"""

import unittest

from print_agent.sse import parse_events


def lines(raw: str):
    return raw.split("\n")


class ParserTests(unittest.TestCase):
    def test_it_reads_a_complete_event(self):
        events = list(parse_events(lines(
            "id: 2026-08-09T17:32:00+00:00\n"
            "event: order.created\n"
            'data: {"type": "order.created"}\n'
            "\n"
        )))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "order.created")
        self.assertEqual(events[0].event_id, "2026-08-09T17:32:00+00:00")
        self.assertEqual(events[0].json(), {"type": "order.created"})

    def test_the_heartbeat_is_not_an_event(self):
        # Chega a cada 20s para o proxy nao derrubar a conexao ociosa.
        events = list(parse_events(lines(": ping\n\n: ping\n\n")))

        self.assertEqual(events, [])

    def test_the_retry_hint_is_not_an_event(self):
        events = list(parse_events(lines("retry: 3000\n\n")))

        self.assertEqual(events, [])

    def test_an_unterminated_event_is_not_emitted(self):
        # Metade de um evento na hora em que a conexao caiu. Emiti-lo
        # significaria agir sobre um JSON cortado ao meio.
        events = list(parse_events(lines('event: order.created\ndata: {"a": 1}')))

        self.assertEqual(events, [])

    def test_the_last_id_keeps_applying_to_later_events(self):
        # Regra do protocolo, e aqui ela e o cursor da reconexao.
        events = list(parse_events(lines(
            "id: cursor-1\n"
            "event: order.created\n"
            "data: {}\n"
            "\n"
            "event: order.status_changed\n"
            "data: {}\n"
            "\n"
        )))

        self.assertEqual([event.event_id for event in events], ["cursor-1", "cursor-1"])

    def test_broken_json_does_not_raise(self):
        events = list(parse_events(lines("event: order.created\ndata: {nao-e-json\n\n")))

        self.assertIsNone(events[0].json())

    def test_a_data_that_is_not_an_object_is_discarded(self):
        events = list(parse_events(lines("event: order.created\ndata: [1, 2]\n\n")))

        self.assertIsNone(events[0].json())

    def test_multiline_data_is_joined(self):
        events = list(parse_events(lines('data: {"a":\ndata: 1}\n\n')))

        self.assertEqual(events[0].json(), {"a": 1})

    def test_the_leading_space_after_the_colon_is_dropped_only_once(self):
        # "data:  x" (dois espacos) carrega um espaco de verdade no valor.
        events = list(parse_events(lines("data:  x\n\n")))

        self.assertEqual(events[0].data, " x")


if __name__ == "__main__":
    unittest.main()
