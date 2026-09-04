"""Coexistência: o número atendendo no celular e na API ao mesmo tempo.

Com ela ligada, a Meta passa a mandar para o MESMO webhook três campos que
hoje não existem — `smb_message_echoes` (o que o atendente humano respondeu),
`smb_app_state_sync` (os contatos dele) e `history` (**até seis meses de
conversa passada da loja**).

Nenhum deles é para nós nesta rodada, e o que estes testes travam é que a
gente **continua ignorando os três de propósito** — não por acidente de um
`if` que um dia alguém "melhora".

## O que a Meta garante, e por que isso faz o nosso desenho fechar

> *"Messages sent from the WhatsApp Business app are not subject to the
> customer service window and do not create, extend, or affect Cloud API
> conversation windows."*

Ou seja: a resposta do atendente pelo celular **não abre nem estende** a
janela de 24h da API. `whatsapp_contact_windows` é sobre conversa da Cloud
API, e só. Ignorar o eco não é tolerância — é a leitura certa da regra deles.

## O `history` é o que mais assusta, e ele é o mais fácil

As mensagens antigas vêm em `value.history[].threads[].messages`, e o nosso
parser lê `value.messages` — o nível de cima, e só ele. Seis meses de
conversa de cliente chegam e não entram em processo nenhum. **Isso é decisão,
não sorte:** guardar aquilo seria dado pessoal de gente que nunca pediu nada
pelo app, numa tabela sem prazo e sem dono (armadilha 38).

Os formatos abaixo são os da documentação da Meta (conferidos em 04/09/2026),
e não invenção — é a mesma regra do CLAUDE.md sobre dublê: um payload que
ninguém envia descreveria um sistema que não existe.
"""

import json

from src.integrations.whatsapp_client import parse_webhook_changes


PNI = "111222333444555"


def _envelope(field: str, value: dict) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"id": "waba-1", "changes": [{"field": field, "value": value}]}],
        }
    ).encode("utf-8")


def _metadata() -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "5585999990000", "phone_number_id": PNI},
    }


class TestOEcoDoAtendenteHumano:
    def test_o_eco_nao_vira_mensagem_recebida(self) -> None:
        """`smb_message_echoes` chega com `message_echoes`, e não `messages`.

        Lê-lo como mensagem do cliente abriria uma janela de 24h que a Meta
        diz que não existe — e o próximo texto livre sairia achando que pode."""
        corpo = _envelope(
            "smb_message_echoes",
            _metadata()
            | {
                "message_echoes": [
                    {
                        "from": "5585999990000",
                        "to": "5585988887777",
                        "id": "wamid.DO_ATENDENTE",
                        "timestamp": "1757000000",
                        "type": "text",
                        "text": {"body": "ja estou separando, ok?"},
                    }
                ]
            },
        )

        mudanca = parse_webhook_changes(corpo)[0]

        assert mudanca.phone_number_id == PNI
        assert mudanca.inbound == ()
        assert mudanca.statuses == ()

    def test_o_texto_do_atendente_nao_entra_no_processo(self) -> None:
        corpo = _envelope(
            "smb_message_echoes",
            _metadata()
            | {
                "message_echoes": [
                    {
                        "from": "5585999990000",
                        "id": "wamid.X",
                        "timestamp": "1757000000",
                        "type": "text",
                        "text": {"body": "o endereco dela e rua tal, 100"},
                    }
                ]
            },
        )

        assert "rua tal" not in repr(parse_webhook_changes(corpo))


class TestOHistoricoDeSeisMeses:
    def test_conversa_antiga_nao_e_lida(self) -> None:
        """As mensagens vêm em `value.history[].threads[].messages`; nós lemos
        `value.messages`, o nível de cima. Seis meses de conversa da loja
        chegam e não entram em lugar nenhum."""
        corpo = _envelope(
            "history",
            _metadata()
            | {
                "history": [
                    {
                        "metadata": {"phase": "0", "chunk_order": 1, "progress": 100},
                        "threads": [
                            {
                                "id": "5585988887777",
                                "messages": [
                                    {
                                        "from": "5585988887777",
                                        "id": "wamid.ANTIGA",
                                        "timestamp": "1740000000",
                                        "type": "text",
                                        "text": {"body": "mensagem de seis meses atras"},
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )

        mudanca = parse_webhook_changes(corpo)[0]

        assert mudanca.inbound == ()
        assert "seis meses atras" not in repr(mudanca)


class TestOsContatosDoCelular:
    def test_sincronizacao_de_contato_nao_vira_nada(self) -> None:
        corpo = _envelope(
            "smb_app_state_sync",
            _metadata()
            | {
                "state_sync": [
                    {
                        "type": "contact",
                        "contact": {"full_name": "Maria Aparecida", "phone_number": "5585988887777"},
                        "action": "add",
                        "metadata": {"timestamp": "1757000000"},
                    }
                ]
            },
        )

        mudanca = parse_webhook_changes(corpo)[0]

        assert mudanca.inbound == ()
        assert mudanca.statuses == ()


class TestOQueContinuaValendo:
    def test_a_mensagem_do_cliente_continua_sendo_lida(self) -> None:
        """O par dos testes acima: sem ele, um parser que parasse de ler
        `messages` ficaria verde em todos eles."""
        corpo = _envelope(
            "messages",
            _metadata()
            | {
                "messages": [
                    {
                        "from": "5585988887777",
                        "id": "wamid.DO_CLIENTE",
                        "timestamp": "1757000000",
                        "type": "text",
                        "text": {"body": "oi"},
                    }
                ]
            },
        )

        mudanca = parse_webhook_changes(corpo)[0]

        assert len(mudanca.inbound) == 1
        assert mudanca.inbound[0].from_phone == "5585988887777"
