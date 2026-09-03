"""Fumaça do "entrar com Google" contra o Postgres, nos três casos.

A suíte rápida dubla os repositórios; aqui não há dublê de banco nenhum — as
constraints, o UNIQUE do `sub` e as colunas `NOT NULL` são as de verdade. É o
teste que o CLAUDE.md descreve como o que denunciou o `serves_people`: o rápido
fica verde sobre um objeto que a aplicação nunca produz, e o de banco não.

O que só aparece aqui:

- o cliente do caso (c) **cabe na tabela** — telefone, nascimento e o
  `password_hash` com o prefixo `!` passam pelas colunas de verdade;
- o `sub` do caso (b) é ligado ao cliente que já existe, e **nenhum segundo
  cliente nasce** (a contagem de `customers` é a prova);
- entrar de novo cai no caso (a) e devolve sessão sem escrever identidade.

O único dublê é o `EmailService` — colaborador externo, e sem
`RESEND_API_KEY` ele levanta 500. Ele guarda o código, que é o que o teste
precisa para seguir para a tela seguinte.
"""

from dataclasses import replace
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from src.integrations.google_identity_client import GoogleIdentity
from src.models.customer_model import Customer
from src.models.customer_social_identity_model import CustomerSocialIdentity
from src.schemas.auth_schema import (
    GOOGLE_AUTHENTICATED,
    GOOGLE_LINK_CONFIRMATION_REQUIRED,
    GOOGLE_PROFILE_REQUIRED,
    GoogleCompleteSignupRequest,
    GoogleSignInRequest,
    VerifyEmailCodeRequest,
)
from src.services import google_signin_tickets as tickets
from src.services.auth_service import AuthService, password_is_set
from src.services.customer_anonymization_service import CustomerAnonymizationService
from src.services.google_auth_service import GoogleAuthService
from src.utils.security import decode_signed_token, hash_password
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


SUB = "104829173829173829173"
EMAIL = "smoke-google@exemplo.com"
SENHA = "senha-forte-123"


class EmailServiceFalso:
    """Colaborador externo. Guarda o codigo que sairia no e-mail."""

    def __init__(self) -> None:
        self.codigos: list[tuple[str, str]] = []

    def send_email_verification_code(self, to_email: str, code: str) -> None:
        self.codigos.append((to_email, code))

    def send_password_reset_code(self, to_email: str, code: str) -> None:
        self.codigos.append((to_email, code))


class IdentityClientFalso:
    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity

    def verify_id_token(self, id_token: str) -> GoogleIdentity:
        return self.identity


def _identidade(sub: str = SUB, email: str = EMAIL) -> GoogleIdentity:
    return GoogleIdentity(subject=sub, email=email, name="Pessoa de Fumaça")


def _servico(db: Session, identity: GoogleIdentity) -> GoogleAuthService:
    """O servico, com o par do nonce ja casado em `servico.pedido`.

    O `nonce` nasce junto da identidade porque e assim que ele existe: o
    navegador pede o par, leva um ao Google e devolve os dois. Montar so um
    lado descreveria uma sessao que nao acontece.
    """
    nonce, nonce_token = tickets.create_nonce()
    servico = GoogleAuthService(
        db, identity_client=IdentityClientFalso(replace(identity, nonce=nonce))
    )
    servico.auth_service.email_service = EmailServiceFalso()
    servico.pedido = GoogleSignInRequest(id_token="qualquer", nonce_token=nonce_token)
    return servico


def _quantos_clientes(db: Session, email: str) -> int:
    return db.scalar(
        select(func.count()).select_from(Customer).where(Customer.email == email)
    )


class TestOCasoCContraOBanco:
    """`sub` novo e e-mail sem conta: o cliente nasce, e cabe na tabela."""

    def test_o_cliente_e_a_identidade_nascem_juntos(self, db: Session) -> None:
        servico = _servico(db, _identidade())

        pedido_de_perfil = servico.sign_in(servico.pedido)
        assert pedido_de_perfil.status == GOOGLE_PROFILE_REQUIRED
        assert _quantos_clientes(db, EMAIL) == 0

        sessao = servico.complete_signup(
            GoogleCompleteSignupRequest(
                signup_ticket=pedido_de_perfil.signup_ticket,
                phone="85988887777",
                birth_date=date(1990, 5, 20),
                privacy_accepted=True,
            )
        )

        criado = db.scalar(select(Customer).where(Customer.email == EMAIL))
        assert criado is not None
        assert criado.phone == "85988887777"
        assert criado.birth_date == date(1990, 5, 20)
        # O Google ja provou o e-mail.
        assert criado.email_verified_at is not None
        # Sem senha utilizavel, e a tela consegue perguntar isso.
        assert password_is_set(criado) is False

        identidade = db.scalar(
            select(CustomerSocialIdentity).where(
                CustomerSocialIdentity.customer_id == criado.id
            )
        )
        assert identidade.provider == SOCIAL_PROVIDER_GOOGLE
        assert identidade.provider_user_id == SUB
        assert decode_signed_token(sessao.access_token, "customer_access")["sub"] == str(
            criado.id
        )

    def test_entrar_de_novo_cai_no_caso_a(self, db: Session) -> None:
        servico = _servico(db, _identidade())
        pedido = servico.sign_in(servico.pedido)
        servico.complete_signup(
            GoogleCompleteSignupRequest(
                signup_ticket=pedido.signup_ticket,
                phone="85988886666",
                birth_date=date(1990, 5, 20),
                privacy_accepted=True,
            )
        )

        de_volta_servico = _servico(db, _identidade())
        de_volta = de_volta_servico.sign_in(de_volta_servico.pedido)

        assert de_volta.status == GOOGLE_AUTHENTICATED
        assert de_volta.access_token is not None
        assert _quantos_clientes(db, EMAIL) == 1


class TestOCasoBContraOBanco:
    """O caso que erra: `sub` novo cujo e-mail JA TEM conta."""

    def _cliente_existente(self, db: Session) -> Customer:
        cliente = fab.criar_cliente(db, email=EMAIL, phone="85977776666")
        cliente.password_hash = hash_password(SENHA)
        db.flush()
        return cliente

    def test_nao_loga_e_nao_liga_nada(self, db: Session) -> None:
        existente = self._cliente_existente(db)
        servico = _servico(db, _identidade())

        resposta = servico.sign_in(servico.pedido)

        assert resposta.status == GOOGLE_LINK_CONFIRMATION_REQUIRED
        assert resposta.access_token is None
        assert resposta.link_ticket is not None
        ligadas = db.scalar(
            select(func.count())
            .select_from(CustomerSocialIdentity)
            .where(CustomerSocialIdentity.customer_id == existente.id)
        )
        assert ligadas == 0

    def test_o_codigo_certo_liga_ao_cliente_que_ja_existe(self, db: Session) -> None:
        existente = self._cliente_existente(db)
        servico = _servico(db, _identidade())
        pedido = servico.sign_in(servico.pedido)
        _, codigo = servico.auth_service.email_service.codigos[-1]

        auth = AuthService(db)
        auth.email_service = EmailServiceFalso()
        confirmada = auth.verify_email_code(
            VerifyEmailCodeRequest(
                email=EMAIL, code=codigo, google_link_ticket=pedido.link_ticket
            )
        )

        # NENHUM cliente novo: e o erro que o enunciado aponta.
        assert _quantos_clientes(db, EMAIL) == 1
        identidade = db.scalar(
            select(CustomerSocialIdentity).where(
                CustomerSocialIdentity.provider_user_id == SUB
            )
        )
        assert identidade.customer_id == existente.id
        assert decode_signed_token(confirmada.access_token, "customer_access")[
            "sub"
        ] == str(existente.id)

    def test_o_codigo_errado_nao_liga_nada(self, db: Session) -> None:
        self._cliente_existente(db)
        servico = _servico(db, _identidade())
        pedido = servico.sign_in(servico.pedido)

        auth = AuthService(db)
        auth.email_service = EmailServiceFalso()
        with pytest.raises(HTTPException) as erro:
            auth.verify_email_code(
                VerifyEmailCodeRequest(
                    email=EMAIL, code="000000", google_link_ticket=pedido.link_ticket
                )
            )

        assert erro.value.status_code == 400
        ligadas = db.scalar(
            select(func.count()).select_from(CustomerSocialIdentity)
        )
        assert ligadas == 0

    def test_o_historico_do_cliente_sobrevive_a_ligacao(self, db: Session) -> None:
        """O que o caso (b) existe para nao perder.

        Se ele criasse cliente novo, o pedido antigo ficaria no id velho: o
        historico some da tela, o cashback fica inalcancavel e o cupom de
        primeira compra volta a valer para quem ja comprou.
        """
        existente = self._cliente_existente(db)
        restaurante = fab.criar_restaurante(db)
        filial = fab.filial_padrao(db, restaurante)
        fab.criar_pedido(db, restaurante, filial, cliente=existente, status="completed")
        db.flush()

        servico = _servico(db, _identidade())
        pedido = servico.sign_in(servico.pedido)
        _, codigo = servico.auth_service.email_service.codigos[-1]
        auth = AuthService(db)
        auth.email_service = EmailServiceFalso()
        auth.verify_email_code(
            VerifyEmailCodeRequest(
                email=EMAIL, code=codigo, google_link_ticket=pedido.link_ticket
            )
        )

        do_cliente = CustomerAnonymizationService(db).order_repository.list_all_by_customer(
            existente.id
        )
        assert len(do_cliente) == 1


class TestOSubENoBancoUnico:
    def test_o_mesmo_sub_nao_liga_a_dois_clientes(self, db: Session) -> None:
        """A garantia que o UNIQUE da e que o service traduz em 409."""
        primeiro = fab.criar_cliente(db, email=EMAIL, phone="85977776666")
        outro = fab.criar_cliente(db, email="outro@exemplo.com", phone="85955554444")
        outro.password_hash = hash_password(SENHA)
        primeiro.password_hash = hash_password(SENHA)
        db.flush()

        servico = _servico(db, _identidade())
        pedido = servico.sign_in(servico.pedido)
        _, codigo = servico.auth_service.email_service.codigos[-1]
        auth = AuthService(db)
        auth.email_service = EmailServiceFalso()
        auth.verify_email_code(
            VerifyEmailCodeRequest(
                email=EMAIL, code=codigo, google_link_ticket=pedido.link_ticket
            )
        )

        # O mesmo `sub` chegando com o e-mail do OUTRO cliente.
        segundo_servico = _servico(db, _identidade(email="outro@exemplo.com"))
        resposta = segundo_servico.sign_in(segundo_servico.pedido)

        # `sub` conhecido ganha do e-mail: entra na conta a que ele ja pertence.
        assert resposta.status == GOOGLE_AUTHENTICATED
        assert resposta.customer.id == primeiro.id
