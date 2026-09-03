"""A decisão entre texto livre e template, contra a janela gravada no banco.

O que estes testes travam é que **a chamada à Meta nem acontece** quando a
janela está fechada. Não basta a mensagem falhar: falhar custa um `131047`
deles, e o sintoma seria um cliente não avisado com o motivo escondido na
resposta de terceiro.

E travam o outro lado: **template não consulta janela nenhuma.** É o que faz
o aviso de pedido chegar a quem nunca escreveu para a loja — que é
praticamente todo mundo.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppContactWindow
from src.services.whatsapp_send_service import WhatsAppSendRefused, WhatsAppSender
from src.utils.crypto import encrypt_whatsapp_token
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

AGORA = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
TELEFONE_DIGITADO = "(85) 98888-7777"
TELEFONE_E164 = "5585988887777"


@pytest.fixture(autouse=True)
def chave_de_cifra(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture
def canal(db: Session) -> WhatsAppChannel:
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    linha = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        waba_id="waba-1",
        phone_number_id="pni-1",
        display_phone_number="+55 85 99999-0000",
        access_token_encrypted=encrypt_whatsapp_token("EAAG-token"),
        is_active=True,
    )
    db.add(linha)
    db.flush()
    return linha


@pytest.fixture
def transporte():
    """Dubla o TRANSPORTE, e não o sender: dublar mais alto testaria o dublê."""
    with patch("src.services.whatsapp_send_service.send_text_message") as texto, patch(
        "src.services.whatsapp_send_service.send_template_message"
    ) as template:
        texto.return_value = "wamid.TEXTO"
        template.return_value = "wamid.TEMPLATE"
        yield {"texto": texto, "template": template}


def abrir_janela(db: Session, canal: WhatsAppChannel, expira_em: datetime) -> None:
    db.add(
        WhatsAppContactWindow(
            channel_id=canal.id,
            phone_e164=TELEFONE_E164,
            window_expires_at=expira_em,
        )
    )
    db.flush()


def sender(db: Session) -> WhatsAppSender:
    enviador = WhatsAppSender(db)
    enviador.clock = lambda: AGORA
    return enviador


class TestOTextoLivreDependeDaJanela:
    def test_dentro_da_janela_o_texto_sai(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        abrir_janela(db, canal, AGORA + timedelta(hours=1))

        wamid = sender(db).send_text(
            channel=canal, to_phone=TELEFONE_DIGITADO, body="chegando em 10 min"
        )

        assert wamid == "wamid.TEXTO"
        assert transporte["texto"].call_count == 1
        assert transporte["texto"].call_args.kwargs["to"] == TELEFONE_E164

    def test_janela_vencida_recusa_SEM_chamar_a_meta(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        abrir_janela(db, canal, AGORA - timedelta(minutes=1))

        with pytest.raises(WhatsAppSendRefused) as erro:
            sender(db).send_text(channel=canal, to_phone=TELEFONE_DIGITADO, body="oi")

        assert erro.value.reason == "window"
        assert transporte["texto"].call_count == 0

    def test_sem_janela_nenhuma_recusa(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        """O estado de quase todo cliente: pediu pelo app, nunca escreveu."""
        with pytest.raises(WhatsAppSendRefused) as erro:
            sender(db).send_text(channel=canal, to_phone=TELEFONE_DIGITADO, body="oi")

        assert erro.value.reason == "window"
        assert transporte["texto"].call_count == 0

    def test_a_janela_de_outro_canal_nao_vale(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        """Duas lojas, dois números: escrever para uma não abre a outra."""
        restaurante = fab.criar_restaurante(db)
        outra_filial = fab.criar_filial(db, restaurante)
        outro = WhatsAppChannel(
            restaurant_id=restaurante.id,
            branch_id=outra_filial.id,
            waba_id="waba-2",
            phone_number_id="pni-2",
            display_phone_number="+55 85 99999-1111",
            access_token_encrypted=encrypt_whatsapp_token("EAAG-outro"),
            is_active=True,
        )
        db.add(outro)
        db.flush()
        abrir_janela(db, outro, AGORA + timedelta(hours=1))

        with pytest.raises(WhatsAppSendRefused):
            sender(db).send_text(channel=canal, to_phone=TELEFONE_DIGITADO, body="oi")


class TestOTemplateNaoDependeDaJanela:
    def test_sai_com_a_janela_fechada(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        wamid = sender(db).send_template(
            channel=canal,
            to_phone=TELEFONE_DIGITADO,
            template_name="pedido_aceito",
            language="pt_BR",
            parameters=("Maria", "5471", "Júnior da Picanha"),
        )

        assert wamid == "wamid.TEMPLATE"
        assert transporte["template"].call_count == 1

    def test_o_token_do_canal_e_decifrado_na_hora(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        """O que vai para a Meta é o token ORIGINAL, e ele só existe em claro
        dentro desta chamada."""
        sender(db).send_template(
            channel=canal,
            to_phone=TELEFONE_DIGITADO,
            template_name="pedido_aceito",
            language="pt_BR",
            parameters=("Maria", "5471", "Júnior"),
        )

        assert transporte["template"].call_args.kwargs["access_token"] == "EAAG-token"


class TestOTelefoneQueNaoDaParaAfirmar:
    def test_recusa_sem_chamar_a_meta(
        self, db: Session, canal: WhatsAppChannel, transporte
    ) -> None:
        with pytest.raises(WhatsAppSendRefused) as erro:
            sender(db).send_template(
                channel=canal,
                to_phone="99999999",
                template_name="pedido_aceito",
                language="pt_BR",
                parameters=("Maria", "5471", "Júnior"),
            )

        assert erro.value.reason == "phone"
        assert transporte["template"].call_count == 0
