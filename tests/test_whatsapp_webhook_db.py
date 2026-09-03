"""O webhook depois da assinatura: rotear, abrir janela, anotar o desfecho.

O que este arquivo trava, e nenhuma das três é óbvia:

1. **`phone_number_id` desconhecido responde 200 e não grava nada.** Não é
   descuido: a Meta retenta em cima de qualquer resposta que não seja 2xx e
   desativa o webhook do app depois de falha sustentada — e o webhook é o
   mesmo para TODOS os restaurantes. Devolver 5xx por um número que não é
   nosso troca um evento inútil por um canal derrubado.
2. **A janela de 24h conta do instante da MENSAGEM, não do nosso relógio.**
   Um webhook que chega meia hora atrasado não pode esticar a janela em meia
   hora — e, ao contrário, um reenvio antigo não pode encolhê-la.
3. **O status só avança.** A Meta reenvia e entrega fora de ordem: um `sent`
   chegando depois de um `read` faria a linha mentir sobre o que aconteceu.

E o status que a Meta tem e nós não (`deleted`, `warning`) é **ignorado, não
gravado**: é a armadilha 15 pelo lado de fora — valor que existe só do lado
deles morre no INSERT contra o CHECK, e o webhook inteiro cairia por causa de
um status que não decide nada.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppContactWindow, WhatsAppMessage
from src.services.whatsapp_webhook_service import WhatsAppWebhookService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

APP_SECRET = "segredo-do-app-da-meta"

TELEFONE = "5585988887777"


@pytest.fixture(autouse=True)
def app_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", APP_SECRET)


def corpo_e_cabecalhos(valor: dict) -> tuple[bytes, dict[str, str]]:
    envelope = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": valor}]}],
    }
    corpo = json.dumps(envelope).encode("utf-8")
    digest = hmac.new(APP_SECRET.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return corpo, {"x-hub-signature-256": f"sha256={digest}"}


def metadados(phone_number_id: str) -> dict:
    return {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "+55 85 99999-0000",
            "phone_number_id": phone_number_id,
        },
    }


def mensagem_recebida(phone_number_id: str, quando: datetime) -> dict:
    return metadados(phone_number_id) | {
        "messages": [
            {
                "from": TELEFONE,
                "id": f"wamid.recebida.{int(quando.timestamp())}",
                "timestamp": str(int(quando.timestamp())),
                "type": "text",
                "text": {"body": "oi"},
            }
        ]
    }


def status_de(phone_number_id: str, wamid: str, status: str, erro: str | None = None) -> dict:
    linha: dict = {"id": wamid, "status": status, "timestamp": "1757000100"}
    if erro is not None:
        linha["errors"] = [{"code": int(erro), "title": "seja o que for"}]
    return metadados(phone_number_id) | {"statuses": [linha]}


def criar_canal(db: Session, restaurante, filial, phone_number_id: str) -> WhatsAppChannel:
    canal = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        waba_id="waba-1",
        phone_number_id=phone_number_id,
        display_phone_number="+55 85 99999-0000",
        access_token_encrypted="gAAAAA-cifrado-de-teste",
        is_active=True,
    )
    db.add(canal)
    db.flush()
    return canal


def criar_mensagem(
    db: Session, pedido, canal, wamid: str, status: str = "sent"
) -> WhatsAppMessage:
    mensagem = WhatsAppMessage(
        order_id=pedido.id,
        channel_id=canal.id,
        kind="order_accepted",
        status=status,
        wamid=wamid,
    )
    db.add(mensagem)
    db.flush()
    return mensagem


def entregar(db: Session, valor: dict) -> dict:
    corpo, cabecalhos = corpo_e_cabecalhos(valor)
    return WhatsAppWebhookService(db).handle(raw_body=corpo, headers=cabecalhos)


class TestONumeroDesconhecido:
    def test_responde_ignorado_sem_gravar_nada(self, db: Session) -> None:
        resultado = entregar(
            db, mensagem_recebida("pni-que-ninguem-cadastrou", datetime.now(timezone.utc))
        )

        assert resultado == {"status": "ignored", "reason": "unknown_number"}
        assert db.query(WhatsAppContactWindow).count() == 0

    def test_o_canal_desligado_cai_no_mesmo_caminho(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        canal = criar_canal(db, restaurante, filial, "pni-desligado")
        canal.is_active = False
        db.flush()

        resultado = entregar(db, mensagem_recebida("pni-desligado", datetime.now(timezone.utc)))

        assert resultado == {"status": "ignored", "reason": "unknown_number"}


class TestAJanelaDeVinteQuatroHoras:
    def test_a_mensagem_do_cliente_abre_a_janela(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        canal = criar_canal(db, restaurante, filial, "pni-1")
        enviada_em = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

        resultado = entregar(db, mensagem_recebida("pni-1", enviada_em))

        assert resultado == {"status": "ok"}
        janela = db.query(WhatsAppContactWindow).one()
        assert janela.channel_id == canal.id
        assert janela.phone_e164 == TELEFONE
        assert janela.window_expires_at == enviada_em + timedelta(hours=24)

    def test_a_segunda_mensagem_estende_a_mesma_janela(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        criar_canal(db, restaurante, filial, "pni-1")
        primeira = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

        entregar(db, mensagem_recebida("pni-1", primeira))
        entregar(db, mensagem_recebida("pni-1", primeira + timedelta(hours=3)))

        janela = db.query(WhatsAppContactWindow).one()
        assert janela.window_expires_at == primeira + timedelta(hours=27)

    def test_webhook_atrasado_nao_encolhe_a_janela(self, db: Session) -> None:
        """A Meta reenvia. Um reenvio de mensagem antiga chegando depois da
        nova não pode desfazer a janela que a nova abriu."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        criar_canal(db, restaurante, filial, "pni-1")
        antiga = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        nova = antiga + timedelta(hours=3)

        entregar(db, mensagem_recebida("pni-1", nova))
        entregar(db, mensagem_recebida("pni-1", antiga))

        janela = db.query(WhatsAppContactWindow).one()
        assert janela.window_expires_at == nova + timedelta(hours=24)

    def test_duas_lojas_tem_janelas_separadas_para_o_mesmo_telefone(
        self, db: Session
    ) -> None:
        restaurante = fab.criar_restaurante(db)
        centro = fab.criar_filial(db, restaurante, nome="Centro")
        aldeota = fab.criar_filial(db, restaurante, nome="Aldeota")
        criar_canal(db, restaurante, centro, "pni-do-centro")
        criar_canal(db, restaurante, aldeota, "pni-da-aldeota")
        quando = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

        entregar(db, mensagem_recebida("pni-do-centro", quando))
        entregar(db, mensagem_recebida("pni-da-aldeota", quando))

        assert db.query(WhatsAppContactWindow).count() == 2


class TestOStatusDaMensagem:
    def test_delivered_atualiza_a_linha_pelo_wamid(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = criar_canal(db, restaurante, filial, "pni-1")
        mensagem = criar_mensagem(db, pedido, canal, "wamid.AAA", status="sent")

        resultado = entregar(db, status_de("pni-1", "wamid.AAA", "delivered"))

        assert resultado == {"status": "ok"}
        db.refresh(mensagem)
        assert mensagem.status == "delivered"

    def test_failed_grava_o_codigo_de_erro(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = criar_canal(db, restaurante, filial, "pni-1")
        mensagem = criar_mensagem(db, pedido, canal, "wamid.AAA", status="sent")

        entregar(db, status_de("pni-1", "wamid.AAA", "failed", erro="131047"))

        db.refresh(mensagem)
        assert mensagem.status == "failed"
        assert mensagem.error_code == "131047"

    def test_status_atrasado_nao_regride(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = criar_canal(db, restaurante, filial, "pni-1")
        mensagem = criar_mensagem(db, pedido, canal, "wamid.AAA", status="read")

        entregar(db, status_de("pni-1", "wamid.AAA", "sent"))

        db.refresh(mensagem)
        assert mensagem.status == "read"

    def test_status_que_a_meta_tem_e_nos_nao_e_ignorado(self, db: Session) -> None:
        """`deleted` existe do lado deles e não está no nosso CHECK. Gravá-lo
        derrubaria o POST inteiro por um valor que não decide nada."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        pedido = fab.criar_pedido(db, restaurante, filial)
        canal = criar_canal(db, restaurante, filial, "pni-1")
        mensagem = criar_mensagem(db, pedido, canal, "wamid.AAA", status="delivered")

        resultado = entregar(db, status_de("pni-1", "wamid.AAA", "deleted"))

        assert resultado == {"status": "ok"}
        db.refresh(mensagem)
        assert mensagem.status == "delivered"

    def test_wamid_que_nao_e_nosso_e_ignorado(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        criar_canal(db, restaurante, filial, "pni-1")

        resultado = entregar(db, status_de("pni-1", "wamid.de-ninguem", "delivered"))

        assert resultado == {"status": "ok"}
        assert db.query(WhatsAppMessage).count() == 0
