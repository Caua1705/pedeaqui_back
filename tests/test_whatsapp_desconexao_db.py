"""O lojista desconectou: o canal para, e para de um jeito visível.

Este é o arquivo que existe contra um **silêncio**. Sem ele, o dia em que o
Júnior abre o aplicativo dele e tira o acesso da Cloud API é um dia em que
nada muda do nosso lado: o canal continua ativo, o aviso continua sendo
tentado, e a Meta responde erro de autenticação a cada pedido aceito. Ninguém
descobre até um cliente reclamar que não foi avisado.

Três coisas, e as três só se veem juntas:

1. **o evento chega por outra chave.** `account_update` não tem
   `phone_number_id` — é da CONTA, e o que o identifica é o WABA. O parser de
   número o descartaria sem uma linha de log;
2. **ele derruba TODOS os números daquele WABA**, porque é o acesso inteiro
   que caiu. Marcar um só deixaria os outros tentando;
3. **desligado por nós e desconectado pela Meta são estados diferentes**, com
   saídas opostas: o primeiro se desfaz no nosso painel, o segundo só com o
   lojista reconectando.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.whatsapp_model import WhatsAppChannel
from src.repositories.whatsapp_repository import WhatsAppChannelRepository
from src.services.whatsapp_webhook_service import WhatsAppWebhookService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

APP_SECRET = "segredo-do-app-da-meta"


@pytest.fixture(autouse=True)
def app_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", APP_SECRET)


def entregar(db: Session, valor: dict, waba_id: str = "waba-do-junior") -> dict:
    envelope = {
        "object": "whatsapp_business_account",
        "entry": [
            {"id": waba_id, "changes": [{"field": "account_update", "value": valor}]}
        ],
    }
    corpo = json.dumps(envelope).encode("utf-8")
    digest = hmac.new(APP_SECRET.encode("utf-8"), corpo, hashlib.sha256).hexdigest()
    return WhatsAppWebhookService(db).handle(
        raw_body=corpo, headers={"x-hub-signature-256": f"sha256={digest}"}
    )


def partner_removed(waba_id: str = "waba-do-junior", motivo: str | None = "USER_INITIATED") -> dict:
    valor: dict = {"event": "PARTNER_REMOVED", "waba_info": {"waba_id": waba_id}}
    if motivo is not None:
        valor["disconnection_info"] = {"reason": motivo, "initiated_by": "BUSINESS"}
    return valor


def criar_canal(
    db: Session,
    restaurante,
    filial=None,
    phone_number_id: str = "pni-1",
    waba_id: str = "waba-do-junior",
) -> WhatsAppChannel:
    canal = WhatsAppChannel(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        display_phone_number=f"+55 85 9999-{phone_number_id[-4:]}",
        access_token_encrypted="gAAAAA-cifrado-de-teste",
        is_active=True,
    )
    db.add(canal)
    db.flush()
    return canal


class TestOEventoDerrubaOCanal:
    def test_partner_removed_marca_a_hora_e_o_motivo(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante)

        resultado = entregar(db, partner_removed())

        assert resultado == {"status": "ok"}
        db.refresh(canal)
        assert canal.disconnected_at is not None
        assert canal.disconnect_reason == "USER_INITIATED"

    def test_derruba_TODOS_os_numeros_do_mesmo_waba(self, db: Session) -> None:
        """O acesso que caiu é o da conta, não o de um número."""
        restaurante = fab.criar_restaurante(db)
        centro = fab.criar_filial(db, restaurante, nome="Centro")
        aldeota = fab.criar_filial(db, restaurante, nome="Aldeota")
        um = criar_canal(db, restaurante, centro, "pni-1111")
        outro = criar_canal(db, restaurante, aldeota, "pni-2222")

        entregar(db, partner_removed())

        db.refresh(um)
        db.refresh(outro)
        assert um.disconnected_at is not None
        assert outro.disconnected_at is not None

    def test_nao_derruba_canal_de_outro_waba(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        do_junior = criar_canal(db, restaurante, phone_number_id="pni-1111")
        outra_loja = fab.criar_restaurante(db)
        de_outro = criar_canal(
            db, outra_loja, phone_number_id="pni-2222", waba_id="waba-de-outro"
        )

        entregar(db, partner_removed())

        db.refresh(do_junior)
        db.refresh(de_outro)
        assert do_junior.disconnected_at is not None
        assert de_outro.disconnected_at is None

    def test_sem_disconnection_info_o_motivo_fica_nulo_e_o_canal_cai(
        self, db: Session
    ) -> None:
        """`disconnection_info` é condicional na Meta. Nulo é "desconectou e
        não disseram por quê" — não é razão para ignorar o evento."""
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante)

        entregar(db, partner_removed(motivo=None))

        db.refresh(canal)
        assert canal.disconnected_at is not None
        assert canal.disconnect_reason is None

    def test_reenvio_nao_move_a_hora_da_desconexao(self, db: Session) -> None:
        """A Meta reenvia. Sobrescrever apagaria a hora em que a desconexão de
        fato aconteceu, que é a única pista de quando os avisos pararam."""
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante)

        entregar(db, partner_removed())
        db.refresh(canal)
        primeira = canal.disconnected_at

        entregar(db, partner_removed())
        db.refresh(canal)

        assert canal.disconnected_at == primeira


class TestOQueNaoEDesconexao:
    def test_outro_evento_de_conta_nao_derruba_nada(self, db: Session) -> None:
        """Só `PARTNER_REMOVED` age. Agir sobre um evento que ninguém estudou
        seria inventar comportamento — o log é o que dirá se algum dia outro
        importa."""
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante)

        resultado = entregar(
            db, {"event": "PARTNER_ADDED", "waba_info": {"waba_id": "waba-do-junior"}}
        )

        assert resultado == {"status": "ok"}
        db.refresh(canal)
        assert canal.disconnected_at is None

    def test_waba_que_nao_e_nosso_e_atendido_sem_derrubar_nada(self, db: Session) -> None:
        """200, porque o webhook FOI atendido. `unknown_number` mentiria: não
        houve número nenhum nesta requisição."""
        resultado = entregar(db, partner_removed(waba_id="waba-de-ninguem"), "waba-de-ninguem")

        assert resultado == {"status": "ok"}


class TestOCanalDesconectadoParaDeSerEscolhido:
    def test_o_webhook_deixa_de_rotear_por_ele(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        criar_canal(db, restaurante, phone_number_id="pni-1111")

        entregar(db, partner_removed())

        assert WhatsAppChannelRepository(db).get_by_phone_number_id("pni-1111") is None

    def test_a_filial_deixa_de_ter_de_onde_falar(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        criar_canal(db, restaurante, filial)

        entregar(db, partner_removed())

        achado = WhatsAppChannelRepository(db).resolve_for_branch(restaurante.id, filial.id)
        assert achado is None

    def test_a_filial_com_numero_desconectado_nao_cai_no_do_restaurante(
        self, db: Session
    ) -> None:
        """A mesma regra do número desligado: parar é o esperado, passar a
        falar por outro número sem ninguém pedir não é.

        E neste caso o do restaurante estaria desconectado junto, porque o
        WABA é o mesmo — mas a regra não pode depender disso."""
        restaurante = fab.criar_restaurante(db)
        filial = fab.criar_filial(db, restaurante)
        criar_canal(db, restaurante, phone_number_id="pni-do-restaurante", waba_id="waba-outro")
        criar_canal(db, restaurante, filial, phone_number_id="pni-da-filial")

        entregar(db, partner_removed())

        assert WhatsAppChannelRepository(db).resolve_for_branch(restaurante.id, filial.id) is None


class TestReconectar:
    def test_recadastrar_o_numero_limpa_a_desconexao(self, db: Session) -> None:
        """Rodar o script de cadastro de novo é o caminho de volta: quem tem um
        token novo em mãos só o tem porque o lojista religou o acesso."""
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante, phone_number_id="pni-1111")
        entregar(db, partner_removed())
        db.refresh(canal)
        assert canal.disconnected_at is not None

        WhatsAppChannelRepository(db).upsert(
            restaurant_id=restaurante.id,
            branch_id=None,
            waba_id="waba-do-junior",
            phone_number_id="pni-1111",
            display_phone_number="+55 85 9999-1111",
            access_token_encrypted="gAAAAA-token-novo",
        )
        db.flush()
        db.refresh(canal)

        assert canal.disconnected_at is None
        assert canal.disconnect_reason is None


class TestOEstadoDesligadoContinuaSendoOutro:
    def test_desligado_por_nos_nao_vira_desconectado(self, db: Session) -> None:
        """As duas colunas respondem perguntas diferentes, e é por isso que são
        duas: `is_active` se desfaz no nosso painel, `disconnected_at` só com o
        lojista reconectando."""
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante)
        canal.is_active = False
        db.flush()

        entregar(db, partner_removed())
        db.refresh(canal)

        assert canal.disconnected_at is not None
        assert canal.is_active is False

    def test_a_hora_gravada_e_de_agora(self, db: Session) -> None:
        restaurante = fab.criar_restaurante(db)
        canal = criar_canal(db, restaurante)
        antes = datetime.now(timezone.utc) - timedelta(seconds=5)

        entregar(db, partner_removed())
        db.refresh(canal)

        assert canal.disconnected_at >= antes
