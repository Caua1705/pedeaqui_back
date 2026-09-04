"""As rotas de WhatsApp do painel: listar, conectar, desconectar.

O que estes testes travam, e cada um fecha uma porta que a tela não consegue
fechar sozinha:

- **a herança aparece explicitamente.** Uma filial sem número próprio herda o
  do restaurante e funciona. Sem `source`/`can_send` na resposta, ela apareceria
  como "sem WhatsApp" e o dono desligaria uma campanha que estava no ar;
- **"nunca conectou" e "conectou e caiu" são estados diferentes.** O primeiro é
  ausência de linha; o segundo é linha com `status` dizendo o quê. Colapsar os
  dois manda o dono conectar um número que já está conectado;
- **as três colisões respondem com FRASE.** O índice cobre, mas `IntegrityError`
  não diz ao lojista qual das três aconteceu nem o que fazer;
- **o token não sai por lugar nenhum**, e o `waba_id` sai mascarado;
- **desconectar NÃO apaga** — nem a linha, nem as mensagens gravadas. A FK sem
  `ON DELETE` existe para isso, e um DELETE de verdade nem passaria.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from main import app
from src.api.dependencies.database import get_db
from src.core.config import settings
from src.models.admin_user_model import AdminUser
from src.models.whatsapp_model import WhatsAppChannel, WhatsAppMessage
from src.repositories.whatsapp_repository import WhatsAppChannelRepository
from src.schemas.admin_whatsapp_schema import WhatsAppChannelStatus
from src.services.admin_auth_service import AdminAuthService
from src.services.admin_whatsapp_service import (
    _ESTADO_DO_CANAL,
    FAIXA_HERDA_DO_RESTAURANTE,
    FAIXA_NUMERO_PROPRIO,
    FAIXA_NUMERO_PROPRIO_FORA_DO_AR,
    FAIXA_SEM_NUMERO,
)
from src.utils.crypto import encrypt_whatsapp_token
from src.utils.security import utcnow
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def chave_de_cifra(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setattr(
        settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def criar_admin(db: Session, restaurante, role: str = "owner", filial=None) -> AdminUser:
    admin = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=None if filial is None else filial.id,
        name=f"Pessoa {role}",
        email=f"{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return admin


def auth(db: Session, admin: AdminUser) -> dict:
    token = AdminAuthService(db).create_access_token(admin)
    return {"Authorization": f"Bearer {token}"}


def canal(db: Session, restaurante, filial=None, **sobrescritas) -> WhatsAppChannel:
    campos = {
        "restaurant_id": restaurante.id,
        "branch_id": None if filial is None else filial.id,
        "waba_id": "1234567890",
        "phone_number_id": f"pni-{uuid.uuid4().hex[:8]}",
        "display_phone_number": "+55 85 99999-0000",
        "access_token_encrypted": encrypt_whatsapp_token("EAAG-token"),
        "is_active": True,
    }
    campos.update(sobrescritas)
    linha = WhatsAppChannel(**campos)
    db.add(linha)
    db.flush()
    return linha


@pytest.fixture
def rede(db: Session):
    """O cenário do piloto: um restaurante, duas filiais."""
    restaurante = fab.criar_restaurante(db, nome="Júnior da Picanha")
    centro = fab.criar_filial(db, restaurante, nome="Centro")
    aldeota = fab.criar_filial(db, restaurante, nome="Aldeota")
    db.flush()
    return restaurante, centro, aldeota


class TestAListagemMostraAHeranca:
    def test_a_linha_do_restaurante_atende_as_duas_filiais(
        self, db: Session, cliente_http, rede
    ) -> None:
        """O que a tela precisa e uma lista de canais não diz.

        Um número, `branch_id` nulo, duas lojas. Sem `source`, as duas
        apareceriam como "sem WhatsApp"."""
        restaurante, centro, aldeota = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        )

        assert resposta.status_code == 200
        corpo = resposta.json()
        assert len(corpo["channels"]) == 1
        assert corpo["channels"][0]["branch_id"] is None
        por_filial = {b["branch_id"]: b for b in corpo["branches"]}
        for filial in (centro, aldeota):
            vista = por_filial[str(filial.id)]
            assert vista["source"] == "restaurant"
            assert vista["display_phone_number"] == "+55 85 91111-0000"
            assert vista["can_send"] is True

    def test_a_filial_com_numero_proprio_nao_herda(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, aldeota = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        canal(db, restaurante, centro, display_phone_number="+55 85 92222-0000")
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        por_filial = {b["branch_id"]: b for b in corpo["branches"]}
        assert por_filial[str(centro.id)]["source"] == "branch"
        assert por_filial[str(centro.id)]["display_phone_number"] == "+55 85 92222-0000"
        assert por_filial[str(aldeota.id)]["source"] == "restaurant"

    def test_nunca_conectou_e_none_e_nao_um_canal_caido(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, _ = rede
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        assert corpo["channels"] == []
        vista = {b["branch_id"]: b for b in corpo["branches"]}[str(centro.id)]
        assert vista["source"] == "none"
        assert vista["channel_id"] is None
        assert vista["can_send"] is False

    def test_conectou_e_caiu_continua_na_lista_com_o_motivo(
        self, db: Session, cliente_http, rede
    ) -> None:
        """A distinção que o dono precisa fazer. Um canal derrubado pela Meta
        que sumisse da lista seria indistinguível de um que nunca existiu — e
        as duas situações pedem coisas opostas dele."""
        restaurante, centro, _ = rede
        canal(
            db,
            restaurante,
            centro,
            disconnected_at=utcnow(),
            disconnect_reason="PARTNER_REMOVED",
        )
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        assert corpo["channels"][0]["status"] == "disconnected_by_meta"
        assert corpo["channels"][0]["disconnect_reason"] == "PARTNER_REMOVED"
        vista = {b["branch_id"]: b for b in corpo["branches"]}[str(centro.id)]
        # A filial TEM canal próprio, e ele não funciona. E ela não cai no do
        # restaurante: número desligado significa que a loja para de mandar.
        assert vista["source"] == "branch"
        assert vista["can_send"] is False

    def test_o_desligado_por_nos_e_um_estado_diferente(
        self, db: Session, cliente_http, rede
    ) -> None:
        """`disabled` religa aqui; `disconnected_by_meta` exige o lojista
        reconectar na Meta antes. São dois consertos, e só um depende de
        outra pessoa."""
        restaurante, centro, _ = rede
        canal(db, restaurante, centro, is_active=False)
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        assert corpo["channels"][0]["status"] == "disabled"

    def test_nem_o_token_nem_o_waba_saem_em_claro(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, _ = rede
        canal(db, restaurante, centro, waba_id="1234567890")
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        )

        assert "EAAG" not in resposta.text
        assert "access_token" not in resposta.text
        assert "1234567890" not in resposta.text
        assert resposta.json()["channels"][0]["waba_id_masked"].endswith("7890")

    def test_o_gerente_le_e_o_atendente_nao(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, _, _ = rede
        gerente = criar_admin(db, restaurante, role="manager")
        atendente = criar_admin(db, restaurante, role="attendant")

        assert (
            cliente_http.get(
                "/admin/whatsapp/channels", headers=auth(db, gerente)
            ).status_code
            == 200
        )
        assert (
            cliente_http.get(
                "/admin/whatsapp/channels", headers=auth(db, atendente)
            ).status_code
            == 403
        )


class TestATabelaDeEstados:
    """O `status` sozinho não é mensagem — é a mesma regra do
    `provider_error_code` do pagamento (armadilha 49). A tela que monta a frase
    a partir do enum está adivinhando, e adivinha errado no dia em que entrar
    um quarto estado: ele cai no `default` do `switch` dela."""

    def test_conectado_nao_tem_acao(self, db: Session, cliente_http, rede) -> None:
        """`null` na ação é informação: quer dizer "está certo", e não "não sei
        o que dizer"."""
        restaurante, centro, _ = rede
        canal(db, restaurante, centro)
        dono = criar_admin(db, restaurante)

        linha = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()["channels"][0]

        assert linha["status"] == "connected"
        assert linha["status_label"] == "Conectado"
        assert linha["status_action"] is None

    def test_desligado_por_nos_diz_para_reconectar_aqui(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, _ = rede
        canal(db, restaurante, centro, is_active=False)
        dono = criar_admin(db, restaurante)

        linha = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()["channels"][0]

        assert linha["status"] == "disabled"
        assert linha["status_action"] is not None

    def test_desconectado_pela_meta_manda_reconectar_LA_primeiro(
        self, db: Session, cliente_http, rede
    ) -> None:
        """O que precisa gritar. É o único dos três em que o conserto NÃO é
        nosso: confundi-lo com `disabled` faz o dono clicar em "conectar" aqui
        e continuar sem receber nada, sem entender por quê."""
        restaurante, centro, _ = rede
        canal(
            db,
            restaurante,
            centro,
            disconnected_at=utcnow(),
            disconnect_reason="PARTNER_REMOVED",
        )
        dono = criar_admin(db, restaurante)

        linha = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()["channels"][0]

        assert linha["status"] == "disconnected_by_meta"
        assert "WhatsApp da loja" in linha["status_action"]
        # E a frase NAO e a do `disabled`: se as duas fossem iguais, o enum
        # teria tres valores e a tela continuaria com uma instrução só.
        assert linha["status_action"] != _ESTADO_DO_CANAL[
            WhatsAppChannelStatus.DISABLED
        ][1]

    def test_todo_estado_tem_linha_na_tabela(self) -> None:
        """A trava que faz um quarto estado nascer vermelho aqui, e não como
        `KeyError` na primeira listagem depois do deploy."""
        assert set(_ESTADO_DO_CANAL) == set(WhatsAppChannelStatus)


class TestAFaixaDaFilialTemFrase:
    """A faixa de cada loja vem escrita, e não montada pela tela a partir do
    `source`.

    É a mesma regra do `status_label` do canal, e aqui ela é o ponto inteiro
    da tela: o dono com duas filiais e um número só precisa entender que as
    duas estão cobertas. `source: "restaurant"` traduzido na tela vira "sem
    WhatsApp" no primeiro `switch` que esquecer o terceiro valor — e aí ele
    desliga uma campanha que estava no ar."""

    def test_a_filial_que_herda_diz_que_USA_o_do_restaurante(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, aldeota = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        por_filial = {b["branch_id"]: b for b in corpo["branches"]}
        for filial in (centro, aldeota):
            vista = por_filial[str(filial.id)]
            assert vista["source_label"] == FAIXA_HERDA_DO_RESTAURANTE
            assert vista["source_label"] != FAIXA_SEM_NUMERO
            # A frase e o numero andam juntos: dizer "usa o do restaurante"
            # sem mostrar qual e mandaria o dono procurar.
            assert vista["display_phone_number"] == "+55 85 91111-0000"

    def test_so_quem_nao_tem_numero_nenhum_le_sem_whatsapp(
        self, db: Session, cliente_http, rede
    ) -> None:
        """A discriminação que a tela precisa e o enum sozinho não dá: a loja
        sem linha própria e a loja sem cobertura nenhuma são a mesma ausência
        na lista de canais, e faixas opostas aqui."""
        restaurante, centro, _ = rede
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        vista = {b["branch_id"]: b for b in corpo["branches"]}[str(centro.id)]
        assert vista["source"] == "none"
        assert vista["source_label"] == FAIXA_SEM_NUMERO
        assert vista["can_send"] is False

    def test_numero_proprio_no_ar_e_fora_do_ar_sao_faixas_DIFERENTES(
        self, db: Session, cliente_http, rede
    ) -> None:
        """As duas são `source: "branch"`, e é `can_send` que as separa. Uma
        faixa só para as duas diria que a loja está mandando quando ela
        parou."""
        restaurante, centro, aldeota = rede
        canal(db, restaurante, centro, display_phone_number="+55 85 92222-0000")
        canal(
            db,
            restaurante,
            aldeota,
            display_phone_number="+55 85 93333-0000",
            is_active=False,
        )
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        por_filial = {b["branch_id"]: b for b in corpo["branches"]}
        assert por_filial[str(centro.id)]["source_label"] == FAIXA_NUMERO_PROPRIO
        assert por_filial[str(centro.id)]["can_send"] is True

        caida = por_filial[str(aldeota.id)]
        assert caida["source"] == "branch"
        assert caida["source_label"] == FAIXA_NUMERO_PROPRIO_FORA_DO_AR
        assert caida["can_send"] is False
        # E ela NAO herda: a faixa nao pode sugerir cobertura que nao ha.
        assert caida["source_label"] != FAIXA_HERDA_DO_RESTAURANTE

    def test_a_filial_com_numero_DESLIGADO_nao_le_a_frase_da_heranca(
        self, db: Session, cliente_http, rede
    ) -> None:
        """O caso que junta os dois regimes, e o único em que a frase errada
        seria plausível: existe número de restaurante, a filial tem o dela, e
        o dela está desligado. Ela para de mandar e NÃO cai no do restaurante
        (ver `disconnect`) — dizer "usa o WhatsApp do restaurante" aqui seria
        a tela afirmando um envio que não acontece."""
        restaurante, centro, _ = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        canal(db, restaurante, centro, is_active=False)
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        vista = {b["branch_id"]: b for b in corpo["branches"]}[str(centro.id)]
        assert vista["source_label"] == FAIXA_NUMERO_PROPRIO_FORA_DO_AR
        assert vista["can_send"] is False

    def test_as_quatro_faixas_sao_frases_distintas(self) -> None:
        """A trava que faz duas faixas iguais nascerem vermelhas aqui. Frases
        repetidas passariam em todos os testes acima — cada um olha a sua — e
        colapsariam dois estados na tela, que é o defeito que esta lista
        inteira existe para impedir."""
        frases = (
            FAIXA_NUMERO_PROPRIO,
            FAIXA_NUMERO_PROPRIO_FORA_DO_AR,
            FAIXA_HERDA_DO_RESTAURANTE,
            FAIXA_SEM_NUMERO,
        )
        assert len(set(frases)) == len(frases)


class TestOFiltroPorFilial:
    def test_sem_filtro_vem_a_rede_inteira(
        self, db: Session, cliente_http, rede
    ) -> None:
        """A forma principal. Trocar de filial para descobrir se cada uma tem
        WhatsApp é o oposto do que a tela existe para fazer."""
        restaurante, centro, aldeota = rede
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        assert {b["branch_id"] for b in corpo["branches"]} == {
            str(centro.id),
            str(aldeota.id),
        }

    def test_com_filtro_vem_so_aquela_loja(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, aldeota = rede
        canal(db, restaurante, centro, display_phone_number="+55 85 92222-0000")
        canal(db, restaurante, aldeota, display_phone_number="+55 85 93333-0000")
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            f"/admin/whatsapp/channels?branch_id={centro.id}", headers=auth(db, dono)
        ).json()

        assert [b["branch_id"] for b in corpo["branches"]] == [str(centro.id)]
        assert [c["display_phone_number"] for c in corpo["channels"]] == [
            "+55 85 92222-0000"
        ]

    def test_o_filtro_MANTEM_a_linha_do_restaurante(
        self, db: Session, cliente_http, rede
    ) -> None:
        """Sem a queda na lista, a tela de uma loja diria "você herda um
        número" sem ter o número para mostrar — a mesma adivinhação que o
        filtro deveria evitar."""
        restaurante, centro, aldeota = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        canal(db, restaurante, aldeota, display_phone_number="+55 85 93333-0000")
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            f"/admin/whatsapp/channels?branch_id={centro.id}", headers=auth(db, dono)
        ).json()

        assert [c["display_phone_number"] for c in corpo["channels"]] == [
            "+55 85 91111-0000"
        ]
        assert corpo["branches"][0]["source"] == "restaurant"

    def test_o_filtro_so_restringe_e_nunca_amplia(
        self, db: Session, cliente_http, rede
    ) -> None:
        """Lojista preso a uma filial que pede outra recebe 404, e não a lista
        alheia."""
        restaurante, centro, aldeota = rede
        gerente_do_centro = criar_admin(db, restaurante, role="manager", filial=centro)

        resposta = cliente_http.get(
            f"/admin/whatsapp/channels?branch_id={aldeota.id}",
            headers=auth(db, gerente_do_centro),
        )

        assert resposta.status_code == 404

    def test_filial_de_outro_restaurante_nao_vaza(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, _, _ = rede
        outro = fab.criar_restaurante(db, nome="Concorrente")
        alheia = fab.criar_filial(db, outro, nome="Alheia")
        db.flush()
        dono = criar_admin(db, restaurante)

        corpo = cliente_http.get(
            f"/admin/whatsapp/channels?branch_id={alheia.id}", headers=auth(db, dono)
        ).json()

        assert corpo["branches"] == []


class TestConectar:
    def _corpo(self, filial=None, **sobrescritas) -> dict:
        corpo = {
            "branch_id": None if filial is None else str(filial.id),
            "waba_id": "1234567890",
            "phone_number_id": "pni-novo",
            "display_phone_number": "+55 85 93333-0000",
            "access_token": "EAAG-token-do-lojista",
        }
        corpo.update(sobrescritas)
        return corpo

    def test_o_dono_conecta_e_o_token_vai_cifrado(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, _ = rede
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(centro),
            headers=auth(db, dono),
        )

        assert resposta.status_code == 200
        assert resposta.json()["status"] == "connected"
        linha = db.query(WhatsAppChannel).one()
        assert linha.access_token_encrypted != "EAAG-token-do-lojista"
        assert "EAAG" not in resposta.text

    def test_o_gerente_nao_conecta(self, db: Session, cliente_http, rede) -> None:
        restaurante, centro, _ = rede
        gerente = criar_admin(db, restaurante, role="manager")

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(centro),
            headers=auth(db, gerente),
        )

        assert resposta.status_code == 403

    def test_a_filial_que_ja_tem_numero_recusa_com_frase(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, _ = rede
        canal(db, restaurante, centro, display_phone_number="+55 85 92222-0000")
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(centro),
            headers=auth(db, dono),
        )

        assert resposta.status_code == 409
        detalhe = resposta.json()["detail"]
        assert "Centro" in detalhe
        assert "+55 85 92222-0000" in detalhe

    def test_a_segunda_linha_de_restaurante_recusa_dizendo_da_heranca(
        self, db: Session, cliente_http, rede
    ) -> None:
        """A frase precisa explicar a herança: o dono que tenta conectar um
        segundo número "do restaurante" não sabe que o primeiro já atende as
        filiais sem número próprio."""
        restaurante, _, _ = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels", json=self._corpo(), headers=auth(db, dono)
        )

        assert resposta.status_code == 409
        assert "herdam" in resposta.json()["detail"]

    def test_numero_de_outro_restaurante_recusa_sem_dizer_de_quem_e(
        self, db: Session, cliente_http, rede
    ) -> None:
        """Quem chegou aqui já tinha o `phone_number_id` em mãos, então a
        existência da linha não é novidade. De quem ela é, seria."""
        restaurante, centro, _ = rede
        outro = fab.criar_restaurante(db, nome="Concorrente")
        canal(db, outro, phone_number_id="pni-novo")
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(centro),
            headers=auth(db, dono),
        )

        assert resposta.status_code == 409
        assert "Concorrente" not in resposta.text

    def test_o_mesmo_numero_de_novo_e_RECONEXAO_e_troca_o_token(
        self, db: Session, cliente_http, rede
    ) -> None:
        """Sem este caminho, desconectar seria irreversível pelo painel:
        conectar responderia "já cadastrado" sobre a própria linha do dono,
        para sempre."""
        restaurante, centro, _ = rede
        linha = canal(
            db,
            restaurante,
            centro,
            phone_number_id="pni-novo",
            is_active=False,
            disconnected_at=utcnow(),
            disconnect_reason="PARTNER_REMOVED",
        )
        antigo = linha.access_token_encrypted
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(centro, access_token="EAAG-token-NOVO"),
            headers=auth(db, dono),
        )

        assert resposta.status_code == 200
        assert resposta.json()["status"] == "connected"
        assert db.query(WhatsAppChannel).count() == 1
        db.refresh(linha)
        assert linha.access_token_encrypted != antigo
        assert linha.disconnected_at is None

    def test_o_numero_que_ja_e_de_outra_filial_sua_nomeia_a_filial(
        self, db: Session, cliente_http, rede
    ) -> None:
        """Dentro do escopo, nomear não vaza nada — e é o que o dono precisa
        para saber onde desconectar."""
        restaurante, centro, aldeota = rede
        canal(db, restaurante, aldeota, phone_number_id="pni-novo")
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(centro),
            headers=auth(db, dono),
        )

        assert resposta.status_code == 409
        assert "Aldeota" in resposta.json()["detail"]

    def test_filial_de_outro_restaurante_e_404(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, _, _ = rede
        outro = fab.criar_restaurante(db, nome="Concorrente")
        filial_alheia = fab.criar_filial(db, outro, nome="Alheia")
        db.flush()
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.post(
            "/admin/whatsapp/channels",
            json=self._corpo(filial_alheia),
            headers=auth(db, dono),
        )

        assert resposta.status_code == 404


class TestDesconectar:
    def test_desativa_a_linha_e_NAO_apaga_as_mensagens(
        self, db: Session, cliente_http, rede
    ) -> None:
        """A FK de `whatsapp_messages` é sem `ON DELETE` de propósito: apagar
        o canal apagaria o registro de que o cliente foi avisado."""
        restaurante, centro, _ = rede
        linha = canal(db, restaurante, centro)
        pedido = fab.criar_pedido(db, restaurante, centro, status="accepted")
        db.add(
            WhatsAppMessage(
                order_id=pedido.id,
                channel_id=linha.id,
                kind="order_accepted",
                status="sent",
                wamid="wamid.ENVIADA",
            )
        )
        db.flush()
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.delete(
            f"/admin/whatsapp/channels/{linha.id}", headers=auth(db, dono)
        )

        assert resposta.status_code == 200
        assert resposta.json()["status"] == "disabled"
        assert db.query(WhatsAppChannel).count() == 1
        assert db.query(WhatsAppMessage).count() == 1

    def test_a_filial_para_de_poder_mandar_e_nao_cai_no_do_restaurante(
        self, db: Session, cliente_http, rede
    ) -> None:
        """O que se espera de um número desligado é que aquela loja pare de
        mandar — e não que ela passe a falar por outro número sem ninguém ter
        pedido."""
        restaurante, centro, aldeota = rede
        canal(db, restaurante, display_phone_number="+55 85 91111-0000")
        da_filial = canal(db, restaurante, centro)
        dono = criar_admin(db, restaurante)

        cliente_http.delete(
            f"/admin/whatsapp/channels/{da_filial.id}", headers=auth(db, dono)
        )
        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, dono)
        ).json()

        por_filial = {b["branch_id"]: b for b in corpo["branches"]}
        assert por_filial[str(centro.id)]["can_send"] is False
        assert por_filial[str(centro.id)]["source"] == "branch"
        # A outra loja, que herdava, continua intacta.
        assert por_filial[str(aldeota.id)]["can_send"] is True

    def test_canal_de_outro_restaurante_e_404(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, _, _ = rede
        outro = fab.criar_restaurante(db, nome="Concorrente")
        alheio = canal(db, outro)
        dono = criar_admin(db, restaurante)

        resposta = cliente_http.delete(
            f"/admin/whatsapp/channels/{alheio.id}", headers=auth(db, dono)
        )

        assert resposta.status_code == 404
        assert db.query(WhatsAppChannel).filter_by(id=alheio.id).one().is_active


class TestOLojistaPresoAUmaFilial:
    def test_nao_ve_as_outras_lojas_da_rede(
        self, db: Session, cliente_http, rede
    ) -> None:
        restaurante, centro, aldeota = rede
        canal(db, restaurante)
        gerente = criar_admin(db, restaurante, role="manager", filial=centro)

        corpo = cliente_http.get(
            "/admin/whatsapp/channels", headers=auth(db, gerente)
        ).json()

        assert [b["branch_id"] for b in corpo["branches"]] == [str(centro.id)]

    def test_nao_desconecta_nada_porque_desconectar_e_do_DONO(
        self, db: Session, cliente_http, rede
    ) -> None:
        """Quem para o gerente de filial aqui é o PAPEL, e não o escopo.

        Vale escrever porque o reflexo é o contrário: a guarda "esta queda
        atende lojas que você não enxerga" parece necessária e seria um ramo
        que nenhum dado alcança — `build_admin_scope` deixa o dono SEM filial
        (`UNRESTRICTED_ROLE`), então todo mundo que passa do 403 enxerga a rede
        inteira. Este teste é o que denuncia se `SOMENTE_DONO` virar
        `GERENCIA` um dia: ele fica vermelho pedindo a decisão que hoje não
        precisa existir."""
        restaurante, centro, _ = rede
        queda = canal(db, restaurante)
        gerente_da_loja = criar_admin(db, restaurante, role="manager", filial=centro)

        resposta = cliente_http.delete(
            f"/admin/whatsapp/channels/{queda.id}", headers=auth(db, gerente_da_loja)
        )

        assert resposta.status_code == 403
        assert db.query(WhatsAppChannel).filter_by(id=queda.id).one().is_active


class TestOsIndicesDeUnicidade:
    """O que cada índice de `whatsapp_channels` permite e o que ele recusa.

    ESTA CLASSE EXISTE PORQUE A LEITURA DELES ENGANA, e enganou. Em
    05/09/2026 o desenho foi lido como se `UNIQUE (branch_id)` fosse "um canal
    por restaurante" — o que faria a segunda filial de qualquer rede colidir —
    e quase virou uma revisão trocando a coluna por `(restaurant_id,
    branch_id)`.

    Não é: **`branch_id` é o UUID de `branches`, já único globalmente**, então
    a restrição diz "no máximo um canal POR FILIAL", que é a regra que se quer.
    E o par não seria mais forte, seria REDUNDANTE — a FK composta
    `(restaurant_id, branch_id) -> branches (restaurant_id, id)` já amarra a
    filial ao restaurante dela, então a segunda coluna não distingue nada que a
    primeira não distinga.

    O conserto proposto era um `DROP`, e é a armadilha 15: um `DROP` na
    constraint errada não dá erro nenhum no dia em que roda. O que fecha essa
    porta não é a conferência (que já foi feita duas vezes e se perdeu as
    duas): é este arquivo ficar vermelho.

    **O risco REAL fica no terceiro índice, e é o inverso do que se temia.**
    `ux_whatsapp_channels_restaurant_default` é
    `UNIQUE (restaurant_id) WHERE branch_id IS NULL`. Quem o escrevesse sem o
    `restaurant_id` — pensando "só pode haver uma queda" — deixaria a
    plataforma inteira com UMA queda, e o SEGUNDO restaurante não conseguiria
    cadastrar a dele. É o primeiro teste abaixo que pega isso, e ele só pega
    porque usa DOIS restaurantes: com um só, os três índices passam iguais.
    """

    @pytest.fixture
    def duas_redes(self, db: Session):
        """Duas redes de duas lojas. O cenário que hoje não existe em produção.

        O Júnior é o único restaurante, e a linha dele tem `branch_id` nulo —
        ou seja, a produção de hoje exercita UM dos três índices. O segundo
        cliente é que estreia os outros dois, e estrear em produção é o que
        este fixture existe para evitar.
        """
        primeira = fab.criar_restaurante(db, nome="Júnior da Picanha")
        segunda = fab.criar_restaurante(db, nome="Outro Restaurante")
        lojas = {
            primeira.id: (
                fab.criar_filial(db, primeira, nome="Centro"),
                fab.criar_filial(db, primeira, nome="Aldeota"),
            ),
            segunda.id: (
                fab.criar_filial(db, segunda, nome="Praia"),
                fab.criar_filial(db, segunda, nome="Bezerra"),
            ),
        }
        db.flush()
        return primeira, segunda, lojas

    def test_dois_restaurantes_com_duas_filiais_cada_convivem(
        self, db: Session, duas_redes
    ) -> None:
        """SEIS canais: duas quedas e quatro números de filial.

        É a afirmação inteira desta classe, e ela é positiva de propósito — o
        modo de falha que importa não é uma escrita indevida passar, é uma
        escrita LEGÍTIMA ser recusada quando o segundo cliente chegar.
        """
        primeira, segunda, lojas = duas_redes

        for restaurante in (primeira, segunda):
            canal(db, restaurante)
            for filial in lojas[restaurante.id]:
                canal(db, restaurante, filial)

        db.flush()

        assert db.query(WhatsAppChannel).count() == 6
        for restaurante in (primeira, segunda):
            canais = (
                db.query(WhatsAppChannel).filter_by(restaurant_id=restaurante.id).all()
            )
            assert len(canais) == 3
            assert sum(1 for item in canais if item.branch_id is None) == 1

    def test_cada_rede_resolve_para_a_PROPRIA_queda(
        self, db: Session, duas_redes
    ) -> None:
        """A pergunta que o índice sozinho não responde.

        Índice recusa escrita; ele não impede a LEITURA de atravessar o
        restaurante. E aqui atravessar significa o segundo cliente mandando
        mensagem pelo número do primeiro — em nome dele, para o cliente dele.

        Sai de `resolve_for_branch`, que é a consulta do envio (armadilha 54),
        e não de um `filter` escrito aqui: duas formas da mesma pergunta
        divergem no dia em que alguém mexe numa.
        """
        primeira, segunda, lojas = duas_redes
        queda_da_primeira = canal(db, primeira, display_phone_number="+55 85 91111-0000")
        queda_da_segunda = canal(db, segunda, display_phone_number="+55 85 92222-0000")
        db.flush()

        repositorio = WhatsAppChannelRepository(db)
        for restaurante, esperado in (
            (primeira, queda_da_primeira),
            (segunda, queda_da_segunda),
        ):
            for filial in lojas[restaurante.id]:
                escolhido = repositorio.resolve_for_branch(restaurante.id, filial.id)
                assert escolhido is not None
                assert escolhido.id == esperado.id

    def test_a_segunda_queda_do_MESMO_restaurante_e_recusada(
        self, db: Session, duas_redes
    ) -> None:
        """`ux_whatsapp_channels_restaurant_default`, a trava que o `UNIQUE`
        não dá: no Postgres `NULL` é distinto de `NULL`, então sem o índice
        parcial o mesmo restaurante teria duas quedas — e `resolve_for_branch`
        escolheria uma das duas sem critério.

        O par positivo é o primeiro teste desta classe: a queda da SEGUNDA rede
        entra normal, e é ela que prova que a recusa aqui é do restaurante
        repetido e não da segunda linha nula da tabela.
        """
        primeira, _, _ = duas_redes
        canal(db, primeira)

        with pytest.raises(IntegrityError):
            canal(db, primeira)

    def test_o_segundo_canal_da_MESMA_filial_e_recusado(
        self, db: Session, duas_redes
    ) -> None:
        """`uq_whatsapp_channels_branch_id`, lido pelo que ele de fato faz.

        Duas linhas para a mesma loja fariam `resolve_for_branch` devolver uma
        das duas sem critério — a filial falaria por um número em cada
        requisição, e o cliente receberia o aviso de um número que nunca viu.

        O par positivo é o primeiro teste: as quatro filiais das duas redes têm
        canal próprio ao mesmo tempo. Se a recusa daqui fosse "um canal por
        restaurante", aquele seria vermelho.
        """
        primeira, _, lojas = duas_redes
        centro, _aldeota = lojas[primeira.id]
        canal(db, primeira, centro)

        with pytest.raises(IntegrityError):
            canal(db, primeira, centro)
