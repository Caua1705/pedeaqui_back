"""A camada que fala com a Meta: assinatura e leitura do corpo.

Aqui não há banco e não há decisão — é o transporte. A assinatura é sobre os
BYTES CRUS (reserializar o JSON muda espaços e ordem de chaves e derruba a
conferência), e a leitura do corpo tem que aguentar o que a Meta manda de
verdade: **vários `entry`, de números diferentes, no mesmo POST.**

Um corpo com dois números atendido como se fosse um é o defeito que não dá
erro: as mensagens da segunda loja seriam creditadas à primeira.
"""

import hashlib
import hmac
import json

import pytest

from src.integrations.whatsapp_client import (
    WhatsAppNotConfiguredError,
    WhatsAppWebhookPayloadError,
    parse_webhook_changes,
    verify_webhook_signature,
)


APP_SECRET = "segredo-do-app-da-meta"


def assinar(corpo: bytes, secret: str = APP_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={digest}"}


def corpo_de(valor: dict, waba_id: str = "waba-1") -> bytes:
    envelope = {
        "object": "whatsapp_business_account",
        "entry": [{"id": waba_id, "changes": [{"field": "messages", "value": valor}]}],
    }
    return json.dumps(envelope).encode("utf-8")


def metadados(phone_number_id: str = "pni-1") -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "5585999990000",
            "phone_number_id": phone_number_id,
        },
    }


class TestAAssinatura:
    def test_corpo_assinado_com_o_app_secret_passa(self) -> None:
        corpo = corpo_de(metadados())

        assert verify_webhook_signature(
            raw_body=corpo, headers=assinar(corpo), app_secret=APP_SECRET
        )

    def test_um_byte_trocado_no_corpo_derruba(self) -> None:
        corpo = corpo_de(metadados())
        cabecalhos = assinar(corpo)

        assert not verify_webhook_signature(
            raw_body=corpo + b" ", headers=cabecalhos, app_secret=APP_SECRET
        )

    def test_assinatura_de_outro_segredo_nao_passa(self) -> None:
        corpo = corpo_de(metadados())

        assert not verify_webhook_signature(
            raw_body=corpo,
            headers=assinar(corpo, secret="segredo-de-outro-app"),
            app_secret=APP_SECRET,
        )

    def test_sem_o_cabecalho_nao_passa(self) -> None:
        corpo = corpo_de(metadados())

        assert not verify_webhook_signature(raw_body=corpo, headers={}, app_secret=APP_SECRET)

    def test_cabecalho_sem_o_prefixo_sha256_nao_passa(self) -> None:
        """A Meta manda `sha256=<hex>`. Um hex pelado não é o que ela manda, e
        aceitá-lo seria aceitar um formato que ninguém produz."""
        corpo = corpo_de(metadados())
        so_o_hex = assinar(corpo)["x-hub-signature-256"].removeprefix("sha256=")

        assert not verify_webhook_signature(
            raw_body=corpo, headers={"x-hub-signature-256": so_o_hex}, app_secret=APP_SECRET
        )

    def test_o_cabecalho_e_case_insensitive(self) -> None:
        corpo = corpo_de(metadados())
        digest = assinar(corpo)["x-hub-signature-256"]

        assert verify_webhook_signature(
            raw_body=corpo, headers={"X-Hub-Signature-256": digest}, app_secret=APP_SECRET
        )

    def test_sem_app_secret_configurado_levanta(self) -> None:
        """Não é `False`: `False` é "não veio da Meta" e vira 401. Isto é
        configuração faltando, e vira 503 — quem tem que agir é quem opera, e
        não quem mandou."""
        corpo = corpo_de(metadados())

        with pytest.raises(WhatsAppNotConfiguredError):
            verify_webhook_signature(raw_body=corpo, headers=assinar(corpo), app_secret=None)


class TestALeituraDoCorpo:
    def test_uma_mensagem_recebida(self) -> None:
        valor = metadados() | {
            "messages": [
                {
                    "from": "5585988887777",
                    "id": "wamid.AAA",
                    "timestamp": "1757000000",
                    "type": "text",
                    "text": {"body": "oi"},
                }
            ]
        }

        mudancas = parse_webhook_changes(corpo_de(valor))

        assert len(mudancas) == 1
        assert mudancas[0].phone_number_id == "pni-1"
        assert mudancas[0].display_phone_number == "5585999990000"
        assert len(mudancas[0].inbound) == 1
        assert mudancas[0].inbound[0].from_phone == "5585988887777"
        assert mudancas[0].inbound[0].sent_at.timestamp() == 1757000000

    def test_o_texto_da_mensagem_nao_e_lido(self) -> None:
        """Dado pessoal que ninguém vai ler nesta rodada não entra no processo.

        Se um dia entrar, entra com prazo — é a armadilha 38, e o lugar de
        decidir isso não é aqui dentro por descuido."""
        valor = metadados() | {
            "messages": [
                {
                    "from": "5585988887777",
                    "id": "wamid.AAA",
                    "timestamp": "1757000000",
                    "type": "text",
                    "text": {"body": "meu endereco e rua tal, 100"},
                }
            ]
        }

        mudanca = parse_webhook_changes(corpo_de(valor))[0]

        assert not hasattr(mudanca.inbound[0], "text")
        assert "rua tal" not in repr(mudanca)

    def test_um_status_de_entrega(self) -> None:
        valor = metadados() | {
            "statuses": [
                {
                    "id": "wamid.BBB",
                    "status": "delivered",
                    "timestamp": "1757000100",
                    "recipient_id": "5585988887777",
                }
            ]
        }

        mudanca = parse_webhook_changes(corpo_de(valor))[0]

        assert len(mudanca.statuses) == 1
        assert mudanca.statuses[0].wamid == "wamid.BBB"
        assert mudanca.statuses[0].status == "delivered"
        assert mudanca.statuses[0].error_code is None

    def test_o_codigo_de_erro_do_status_falho_atravessa(self) -> None:
        valor = metadados() | {
            "statuses": [
                {
                    "id": "wamid.CCC",
                    "status": "failed",
                    "timestamp": "1757000100",
                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                }
            ]
        }

        mudanca = parse_webhook_changes(corpo_de(valor))[0]

        assert mudanca.statuses[0].status == "failed"
        assert mudanca.statuses[0].error_code == "131047"

    def test_dois_numeros_no_mesmo_post_sao_duas_mudancas(self) -> None:
        """O caso que o roteamento por requisição erraria em silêncio."""
        envelope = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [{"field": "messages", "value": metadados("pni-do-centro")}],
                },
                {
                    "id": "waba-1",
                    "changes": [{"field": "messages", "value": metadados("pni-da-aldeota")}],
                },
            ],
        }

        mudancas = parse_webhook_changes(json.dumps(envelope).encode("utf-8"))

        assert [m.phone_number_id for m in mudancas] == ["pni-do-centro", "pni-da-aldeota"]

    def test_mudanca_sem_phone_number_id_e_descartada(self) -> None:
        """Sem ele não há como rotear. Descartar é o que sobra — e o corpo
        inteiro não pode ser recusado por causa de uma mudança torta."""
        envelope = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {"field": "messages", "value": {"metadata": {}}},
                        {"field": "messages", "value": metadados("pni-boa")},
                    ],
                }
            ],
        }

        mudancas = parse_webhook_changes(json.dumps(envelope).encode("utf-8"))

        assert [m.phone_number_id for m in mudancas] == ["pni-boa"]

    def test_corpo_que_nao_e_json_levanta(self) -> None:
        with pytest.raises(WhatsAppWebhookPayloadError):
            parse_webhook_changes(b"nao sou json")

    def test_objeto_que_nao_e_de_whatsapp_levanta(self) -> None:
        """A Meta usa o mesmo formato de webhook para outros produtos. Um
        `object` de Instagram chegando aqui é configuração errada no painel,
        não mensagem para processar."""
        corpo = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")

        with pytest.raises(WhatsAppWebhookPayloadError):
            parse_webhook_changes(corpo)

    def test_corpo_sem_mudanca_nenhuma_e_lista_vazia(self) -> None:
        corpo = json.dumps(
            {"object": "whatsapp_business_account", "entry": []}
        ).encode("utf-8")

        assert parse_webhook_changes(corpo) == []
