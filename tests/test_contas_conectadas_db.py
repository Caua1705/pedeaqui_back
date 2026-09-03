"""Contas conectadas contra o Postgres: conectar, desconectar, e a trava.

A suíte rápida dubla o repositório — e é justamente ali que mora o buraco
desta frente: `CustomerSocialAccountService.unlink` chama
`social_identity_repository.delete(alvo)`, um método que **não existia** no
repositório de verdade quando o teste rápido já estava verde. O dublê
respondia; o Postgres não responderia.

O que só aparece aqui:

- o `delete` de UMA identidade existe e apaga só ela;
- o UNIQUE `(provider, provider_user_id)` deixa o `sub` livre depois de
  desconectar — quem desconecta pode conectar aquele Google noutra conta;
- desconectar **não** leva pedido, endereço nem cashback junto: eles pendem
  do cliente, e nenhuma FK os liga ao provedor.
"""

from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from src.integrations.google_identity_client import GoogleIdentity
from src.models.customer_social_identity_model import CustomerSocialIdentity
from src.schemas.customer_schema import (
    LinkGoogleAccountRequest,
    UnlinkSocialAccountRequest,
)
from src.services import google_signin_tickets as tickets
from src.services.auth_service import unusable_password_hash
from src.services.customer_social_service import CustomerSocialAccountService
from src.utils.security import hash_password
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


SUB = "104829173829173829173"
SENHA = "senha-forte-123"


class IdentityClientFalso:
    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity

    def verify_id_token(self, id_token: str) -> GoogleIdentity:
        return self.identity


def _servico(db: Session, sub: str = SUB) -> CustomerSocialAccountService:
    nonce, nonce_token = tickets.create_nonce()
    servico = CustomerSocialAccountService(db)
    servico.identity_client = IdentityClientFalso(
        replace(
            GoogleIdentity(subject=sub, email="pessoa@gmail.com", name="Pessoa"),
            nonce=nonce,
        )
    )
    servico.nonce_token = nonce_token
    return servico


def _pedido(servico, senha=SENHA) -> LinkGoogleAccountRequest:
    return LinkGoogleAccountRequest(
        id_token="qualquer", nonce_token=servico.nonce_token, password=senha
    )


def _cliente_com_senha(db: Session, **kwargs):
    cliente = fab.criar_cliente(db, **kwargs)
    cliente.password_hash = hash_password(SENHA)
    db.flush()
    return cliente


def _ligadas(db: Session, customer_id) -> int:
    return db.scalar(
        select(func.count())
        .select_from(CustomerSocialIdentity)
        .where(CustomerSocialIdentity.customer_id == customer_id)
    )


class TestConectarELDesconectar:
    def test_conecta_e_a_linha_cabe_na_tabela(self, db: Session) -> None:
        cliente = _cliente_com_senha(db)
        servico = _servico(db)

        contas = servico.link_google(cliente, _pedido(servico))

        assert [c.provider for c in contas] == [SOCIAL_PROVIDER_GOOGLE]
        assert contas[0].linked_at is not None
        assert _ligadas(db, cliente.id) == 1

    def test_desconecta_e_a_linha_some(self, db: Session) -> None:
        """O `delete` de UMA identidade — o metodo que o dublê da suite rapida
        respondia e o repositorio de verdade nao tinha."""
        cliente = _cliente_com_senha(db)
        servico = _servico(db)
        servico.link_google(cliente, _pedido(servico))

        restantes = servico.unlink(
            cliente, SOCIAL_PROVIDER_GOOGLE, UnlinkSocialAccountRequest(password=SENHA)
        )

        assert restantes == []
        assert _ligadas(db, cliente.id) == 0

    def test_o_sub_fica_livre_para_outra_conta(self, db: Session) -> None:
        """O UNIQUE `(provider, provider_user_id)` e por LINHA, nao historico:
        desconectar libera aquele Google de verdade."""
        primeiro = _cliente_com_senha(db, email="um@exemplo.com", phone="85911110000")
        segundo = _cliente_com_senha(db, email="dois@exemplo.com", phone="85922220000")

        servico = _servico(db)
        servico.link_google(primeiro, _pedido(servico))
        servico.unlink(
            primeiro, SOCIAL_PROVIDER_GOOGLE, UnlinkSocialAccountRequest(password=SENHA)
        )

        outro = _servico(db)
        outro.link_google(segundo, _pedido(outro))

        assert _ligadas(db, primeiro.id) == 0
        assert _ligadas(db, segundo.id) == 1

    def test_o_mesmo_google_em_duas_contas_e_409(self, db: Session) -> None:
        primeiro = _cliente_com_senha(db, email="um@exemplo.com", phone="85911110000")
        segundo = _cliente_com_senha(db, email="dois@exemplo.com", phone="85922220000")
        servico = _servico(db)
        servico.link_google(primeiro, _pedido(servico))

        outro = _servico(db)
        with pytest.raises(HTTPException) as erro:
            outro.link_google(segundo, _pedido(outro))

        assert erro.value.status_code == 409
        assert _ligadas(db, segundo.id) == 0

    def test_desconectar_nao_leva_o_pedido_junto(self, db: Session) -> None:
        """Nenhuma FK liga pedido a provedor. O teste existe porque a pergunta
        aparece toda vez que alguem le "desconectar"."""
        cliente = _cliente_com_senha(db)
        restaurante = fab.criar_restaurante(db)
        filial = fab.filial_padrao(db, restaurante)
        fab.criar_pedido(db, restaurante, filial, cliente=cliente, status="completed")
        db.flush()
        servico = _servico(db)
        servico.link_google(cliente, _pedido(servico))

        servico.unlink(
            cliente, SOCIAL_PROVIDER_GOOGLE, UnlinkSocialAccountRequest(password=SENHA)
        )

        from src.repositories.order_repository import OrderRepository

        assert len(OrderRepository(db).list_all_by_customer(cliente.id)) == 1


class TestATravaContraOBanco:
    def test_a_conta_sem_senha_nao_desconecta_a_unica_porta(self, db: Session) -> None:
        cliente = fab.criar_cliente(db)
        cliente.password_hash = unusable_password_hash()
        db.flush()
        # A ligacao entra por baixo: pela rota ela seria recusada (a conta nao
        # tem senha para provar), e o que se exercita aqui e o DESCONECTAR.
        CustomerSocialAccountService(db).social_identity_repository.create(
            customer_id=cliente.id,
            provider=SOCIAL_PROVIDER_GOOGLE,
            provider_user_id=SUB,
        )
        db.flush()

        with pytest.raises(HTTPException) as erro:
            CustomerSocialAccountService(db).unlink(
                cliente,
                SOCIAL_PROVIDER_GOOGLE,
                UnlinkSocialAccountRequest(password=SENHA),
            )

        assert erro.value.status_code == 400
        assert "única" in erro.value.detail.lower()
        assert _ligadas(db, cliente.id) == 1

    def test_a_conta_sem_senha_nao_conecta_um_segundo(self, db: Session) -> None:
        cliente = fab.criar_cliente(db)
        cliente.password_hash = unusable_password_hash()
        db.flush()
        servico = _servico(db)

        with pytest.raises(HTTPException) as erro:
            servico.link_google(cliente, _pedido(servico))

        assert erro.value.status_code == 400
        assert _ligadas(db, cliente.id) == 0
