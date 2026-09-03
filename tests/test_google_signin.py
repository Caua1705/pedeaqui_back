"""Entrar com Google: os tres casos, e o do meio com o detalhe que erra.

O `id_token` ja tem arquivo proprio (`test_google_identity_client.py`, com
chave RSA de verdade e o PyJWT do lock). Aqui o cliente de identidade e
dublado — ele e COLABORADOR, nao dado nosso — e o que se exercita e a decisao:
que caso e este, e o que ele faz com o banco.

## O teste que justifica o arquivo

`TestOCasoB` inteiro. Um `sub` novo cujo e-mail ja tem conta parece "a mesma
pessoa voltando", e tratar assim entrega a conta de quem se cadastrou com o
e-mail da vitima. Os testes cobram as tres metades disso:

- o `POST /auth/google` **nao loga** e **nao liga** — so manda codigo;
- o codigo certo liga ao cliente que JA EXISTE e **nao cria outro**;
- o codigo errado nao liga nada.

`GoogleIdentity` e o dataclass de verdade, e nao um `SimpleNamespace`: e o
contrato que a funcao sob teste recebe (CLAUDE.md). O `Customer` e o
`CustomerSocialIdentity` sao instancias transientes das fabricas.
"""

import unittest
import uuid
from datetime import date, datetime, timezone

from fastapi import HTTPException

from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from src.integrations.google_identity_client import (
    GoogleIdentity,
    GoogleIdentityInvalidTokenError,
    GoogleIdentityNotConfiguredError,
    GoogleIdentityUnavailableError,
    GoogleIdentityUnverifiedEmailError,
)
from src.schemas.auth_schema import (
    GOOGLE_AUTHENTICATED,
    GOOGLE_LINK_CONFIRMATION_REQUIRED,
    GOOGLE_PROFILE_REQUIRED,
    GoogleCompleteSignupRequest,
    GoogleSignInRequest,
    VerifyEmailCodeRequest,
)
from src.services import google_signin_tickets as tickets
from src.services.auth_service import AuthService
from src.services.google_auth_service import GoogleAuthService
from src.utils.security import decode_signed_token, hash_verification_code, utcnow
from tests import fabricas
from tests.test_auth_service import (
    CODIGO,
    FakeCustomerRepository,
    FakeDb,
    FakeEmailService,
    make_code_row,
    make_customer,
)


SUB = "104829173829173829173"
EMAIL = "pessoa@gmail.com"


def identidade_do_google(**sobrescritas) -> GoogleIdentity:
    campos = {"subject": SUB, "email": EMAIL, "name": "Pessoa de Teste"}
    campos.update(sobrescritas)
    return GoogleIdentity(**campos)


class FakeIdentityClient:
    """Colaborador: devolve a identidade, ou levanta o que o Google levantaria."""

    def __init__(self, identity: GoogleIdentity | None = None, erro=None):
        self.identity = identity or identidade_do_google()
        self.erro = erro
        self.tokens_conferidos: list[str] = []

    def verify_id_token(self, id_token: str) -> GoogleIdentity:
        self.tokens_conferidos.append(id_token)
        if self.erro is not None:
            raise self.erro
        return self.identity


class FakeSocialIdentityRepository:
    def __init__(self, identity=None):
        self.identity = identity
        self.created: list[dict] = []
        self.logins: list = []

    def get_by_provider_user(self, provider, provider_user_id):
        if self.identity is None:
            return None
        casa = (
            self.identity.provider == provider
            and self.identity.provider_user_id == provider_user_id
        )
        return self.identity if casa else None

    def create(self, **values):
        self.created.append(values)
        return fabricas.identidade_social(**values)

    def mark_login(self, identity, now):
        self.logins.append((identity, now))
        identity.last_login_at = now
        return identity


def montar_servico(
    identity_client=None,
    customer=None,
    social=None,
    email_code=None,
    recent_codes=0,
) -> GoogleAuthService:
    servico = GoogleAuthService.__new__(GoogleAuthService)
    servico.db = FakeDb()
    servico.customer_repository = FakeCustomerRepository(
        customer=customer, email_code=email_code, recent_codes=recent_codes
    )
    servico.social_identity_repository = social or FakeSocialIdentityRepository()
    servico.identity_client = identity_client or FakeIdentityClient()
    servico.auth_service = montar_auth(
        customer=customer, email_code=email_code, recent_codes=recent_codes
    )
    return servico


def montar_auth(
    customer=None, email_code=None, recent_codes=0, social=None
) -> AuthService:
    auth = AuthService.__new__(AuthService)
    auth.db = FakeDb()
    auth.customer_repository = FakeCustomerRepository(
        customer=customer, email_code=email_code, recent_codes=recent_codes
    )
    auth.social_identity_repository = social or FakeSocialIdentityRepository()
    auth.email_service = FakeEmailService()
    return auth


def cliente_do_token(token: str) -> str:
    return decode_signed_token(token, "customer_access")["sub"]


# ============================================================== caso (a)


class TestOCasoA(unittest.TestCase):
    """`sub` conhecido: loga, e nada muda."""

    def test_devolve_sessao(self) -> None:
        cliente = make_customer()
        vinculo = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepository(vinculo)
        servico = montar_servico(customer=cliente, social=social)

        resposta = servico.sign_in(GoogleSignInRequest(id_token="qualquer"))

        self.assertEqual(resposta.status, GOOGLE_AUTHENTICATED)
        self.assertEqual(cliente_do_token(resposta.access_token), str(cliente.id))
        self.assertEqual(resposta.token_type, "bearer")
        self.assertEqual(resposta.customer.id, cliente.id)

    def test_o_token_e_o_mesmo_do_login_por_email(self) -> None:
        """`purpose` e `type` iguais aos de `AuthService.login`. Um token com
        outro `purpose` seria recusado por `get_customer_from_token` — a pessoa
        entraria e a sessao nao valeria em rota nenhuma."""
        cliente = make_customer()
        vinculo = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        servico = montar_servico(
            customer=cliente, social=FakeSocialIdentityRepository(vinculo)
        )

        resposta = servico.sign_in(GoogleSignInRequest(id_token="qualquer"))

        payload = decode_signed_token(resposta.access_token, "customer_access")
        self.assertEqual(payload["type"], "customer")

    def test_marca_o_ultimo_login(self) -> None:
        cliente = make_customer()
        vinculo = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepository(vinculo)
        montar_servico(customer=cliente, social=social).sign_in(
            GoogleSignInRequest(id_token="qualquer")
        )

        self.assertEqual(len(social.logins), 1)
        self.assertIsNotNone(vinculo.last_login_at)

    def test_nao_cria_identidade_nenhuma(self) -> None:
        cliente = make_customer()
        vinculo = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        social = FakeSocialIdentityRepository(vinculo)
        montar_servico(customer=cliente, social=social).sign_in(
            GoogleSignInRequest(id_token="qualquer")
        )

        self.assertEqual(social.created, [])

    def test_conta_inativa_e_403(self) -> None:
        cliente = make_customer(is_active=False)
        vinculo = fabricas.identidade_social(
            customer_id=cliente.id, provider_user_id=SUB
        )
        servico = montar_servico(
            customer=cliente, social=FakeSocialIdentityRepository(vinculo)
        )

        with self.assertRaises(HTTPException) as erro:
            servico.sign_in(GoogleSignInRequest(id_token="qualquer"))
        self.assertEqual(erro.exception.status_code, 403)


# ============================================================== caso (b)


class TestOCasoB(unittest.TestCase):
    """`sub` novo com e-mail que JA TEM conta. O caso que erra."""

    def setUp(self) -> None:
        self.cliente = make_customer(email=EMAIL)
        self.social = FakeSocialIdentityRepository()
        self.servico = montar_servico(customer=self.cliente, social=self.social)

    def _entrar(self):
        return self.servico.sign_in(GoogleSignInRequest(id_token="qualquer"))

    def test_nao_loga(self) -> None:
        """A metade que entrega a conta da vitima se for feita errado."""
        resposta = self._entrar()
        self.assertEqual(resposta.status, GOOGLE_LINK_CONFIRMATION_REQUIRED)
        self.assertIsNone(resposta.access_token)
        self.assertIsNone(resposta.customer)

    def test_nao_liga_a_identidade(self) -> None:
        self._entrar()
        self.assertEqual(self.social.created, [])

    def test_manda_o_codigo_para_o_email(self) -> None:
        self._entrar()
        enviados = self.servico.auth_service.email_service.verification_codes
        self.assertEqual(len(enviados), 1)
        self.assertEqual(enviados[0][0], EMAIL)

    def test_devolve_um_ticket_para_a_conta_certa(self) -> None:
        resposta = self._entrar()
        ticket = tickets.read_link_ticket(resposta.link_ticket)
        self.assertEqual(ticket.subject, SUB)
        self.assertEqual(ticket.customer_id, self.cliente.id)

    def test_o_ticket_de_ligacao_nao_vale_como_sessao(self) -> None:
        """`purpose` proprio (armadilha 32): a mesma chave assina os dois, e o
        que os separa e o `purpose`. Sem ele, o ticket abriria a conta."""
        resposta = self._entrar()
        with self.assertRaises(Exception):
            decode_signed_token(resposta.link_ticket, "customer_access")

    def test_o_teto_de_reenvio_segura_a_metralhadora_de_email(self) -> None:
        """Quem tem um Google com o e-mail de alguem pode chamar esta rota em
        laco. O cooldown e o teto POR E-MAIL do `AuthService` sao o que impede
        cada chamada de virar uma mensagem na caixa de entrada da pessoa."""
        servico = montar_servico(customer=self.cliente, recent_codes=3)
        resposta = servico.sign_in(GoogleSignInRequest(id_token="qualquer"))

        self.assertEqual(resposta.status, GOOGLE_LINK_CONFIRMATION_REQUIRED)
        self.assertEqual(servico.auth_service.email_service.verification_codes, [])

    def test_a_resposta_nao_muda_quando_o_teto_fecha(self) -> None:
        """Silenciosa de proposito, como `resend_email_code`: nao ha nada que o
        app faca de diferente, e variar a resposta so contaria quantos codigos
        ja sairam para aquele e-mail."""
        com_folga = self._entrar()
        no_teto = montar_servico(customer=self.cliente, recent_codes=3).sign_in(
            GoogleSignInRequest(id_token="qualquer")
        )
        self.assertEqual(com_folga.status, no_teto.status)
        self.assertEqual(com_folga.message, no_teto.message)


class TestOCodigoQueLiga(unittest.TestCase):
    """A segunda metade do caso (b): `verify_email_code` com o ticket."""

    def setUp(self) -> None:
        self.cliente = make_customer(email=EMAIL, email_verified_at=None)
        self.social = FakeSocialIdentityRepository()
        self.auth = montar_auth(
            customer=self.cliente,
            email_code=make_code_row(),
            social=self.social,
        )
        self.ticket = tickets.create_link_ticket(
            identidade_do_google(), self.cliente.id
        )

    def _confirmar(self, code=CODIGO, ticket=None):
        return self.auth.verify_email_code(
            VerifyEmailCodeRequest(
                email=EMAIL,
                code=code,
                google_link_ticket=self.ticket if ticket is None else ticket,
            )
        )

    def test_liga_ao_cliente_QUE_JA_EXISTE(self) -> None:
        """O erro que o enunciado aponta: neste caminho a rota NAO pode criar
        cliente. Se criasse, seria a segunda conta com o mesmo e-mail."""
        self._confirmar()

        self.assertEqual(len(self.social.created), 1)
        self.assertEqual(self.social.created[0]["customer_id"], self.cliente.id)
        self.assertEqual(self.social.created[0]["provider_user_id"], SUB)
        self.assertIsNone(self.auth.customer_repository.created)

    def test_devolve_a_sessao(self) -> None:
        resposta = self._confirmar()

        self.assertTrue(resposta.verified)
        self.assertEqual(resposta.linked_provider, SOCIAL_PROVIDER_GOOGLE)
        self.assertEqual(cliente_do_token(resposta.access_token), str(self.cliente.id))

    def test_marca_o_email_como_verificado(self) -> None:
        self._confirmar()
        self.assertIsNotNone(self.cliente.email_verified_at)

    def test_codigo_errado_nao_liga_nada(self) -> None:
        with self.assertRaises(HTTPException) as erro:
            self._confirmar(code="000000")

        self.assertEqual(erro.exception.status_code, 400)
        self.assertEqual(self.social.created, [])

    def test_ticket_de_outra_conta_e_409(self) -> None:
        """O e-mail mudou de dono entre a emissao e a volta do codigo."""
        de_outra = tickets.create_link_ticket(identidade_do_google(), uuid.uuid4())
        with self.assertRaises(HTTPException) as erro:
            self._confirmar(ticket=de_outra)

        self.assertEqual(erro.exception.status_code, 409)
        self.assertEqual(self.social.created, [])

    def test_sub_ja_ligado_a_outra_conta_e_409(self) -> None:
        alheia = fabricas.identidade_social(
            customer_id=uuid.uuid4(), provider_user_id=SUB
        )
        self.social.identity = alheia

        with self.assertRaises(HTTPException) as erro:
            self._confirmar()
        self.assertEqual(erro.exception.status_code, 409)

    def test_religar_o_mesmo_sub_e_idempotente(self) -> None:
        """O mesmo codigo conferido duas vezes, ou a ligacao feita noutra aba."""
        ja_ligada = fabricas.identidade_social(
            customer_id=self.cliente.id, provider_user_id=SUB
        )
        self.social.identity = ja_ligada

        resposta = self._confirmar()

        self.assertEqual(self.social.created, [])
        self.assertIsNotNone(resposta.access_token)

    def test_ticket_de_cadastro_nao_serve_aqui(self) -> None:
        """`purpose` errado: usar o ticket do caso (c) aqui seria o caminho de
        cliente novo entrando pelo de cliente existente."""
        de_cadastro = tickets.create_signup_ticket(identidade_do_google())
        with self.assertRaises(HTTPException) as erro:
            self._confirmar(ticket=de_cadastro)
        self.assertEqual(erro.exception.status_code, 400)

    def test_conta_inativa_e_403(self) -> None:
        self.cliente.is_active = False
        with self.assertRaises(HTTPException) as erro:
            self._confirmar()
        self.assertEqual(erro.exception.status_code, 403)


class TestOFluxoDeEmailNaoMudou(unittest.TestCase):
    """Sem ticket, `verify_email_code` faz o que sempre fez.

    E a metade que o enunciado manda vigiar: o caso (b) nao pode mexer no
    fluxo de e-mail existente.
    """

    def setUp(self) -> None:
        self.cliente = make_customer(email=EMAIL, email_verified_at=None)
        self.social = FakeSocialIdentityRepository()
        self.auth = montar_auth(
            customer=self.cliente, email_code=make_code_row(), social=self.social
        )

    def test_nao_devolve_token(self) -> None:
        resposta = self.auth.verify_email_code(
            VerifyEmailCodeRequest(email=EMAIL, code=CODIGO)
        )
        self.assertTrue(resposta.verified)
        self.assertIsNone(resposta.access_token)
        self.assertIsNone(resposta.customer)
        self.assertIsNone(resposta.linked_provider)

    def test_nao_liga_identidade_nenhuma(self) -> None:
        self.auth.verify_email_code(VerifyEmailCodeRequest(email=EMAIL, code=CODIGO))
        self.assertEqual(self.social.created, [])

    def test_a_mensagem_continua_a_de_sempre(self) -> None:
        resposta = self.auth.verify_email_code(
            VerifyEmailCodeRequest(email=EMAIL, code=CODIGO)
        )
        self.assertEqual(resposta.message, "E-mail verificado com sucesso.")

    def test_codigo_errado_continua_400(self) -> None:
        with self.assertRaises(HTTPException) as erro:
            self.auth.verify_email_code(
                VerifyEmailCodeRequest(email=EMAIL, code="000000")
            )
        self.assertEqual(erro.exception.status_code, 400)


# ============================================================== caso (c)


class TestOCasoC(unittest.TestCase):
    """`sub` novo e e-mail sem conta: cadastro."""

    def test_pede_o_perfil_e_nao_loga(self) -> None:
        resposta = montar_servico().sign_in(GoogleSignInRequest(id_token="qualquer"))

        self.assertEqual(resposta.status, GOOGLE_PROFILE_REQUIRED)
        self.assertIsNone(resposta.access_token)
        self.assertEqual(resposta.email, EMAIL)
        self.assertEqual(resposta.name, "Pessoa de Teste")

    def test_nao_cria_cliente_nem_identidade(self) -> None:
        servico = montar_servico()
        servico.sign_in(GoogleSignInRequest(id_token="qualquer"))

        self.assertIsNone(servico.customer_repository.created)
        self.assertEqual(servico.social_identity_repository.created, [])

    def test_o_ticket_de_cadastro_carrega_o_que_o_google_deu(self) -> None:
        resposta = montar_servico().sign_in(GoogleSignInRequest(id_token="qualquer"))

        ticket = tickets.read_signup_ticket(resposta.signup_ticket)
        self.assertEqual(ticket.subject, SUB)
        self.assertEqual(ticket.email, EMAIL)
        self.assertEqual(ticket.name, "Pessoa de Teste")


class TestOCadastroConcluido(unittest.TestCase):
    def setUp(self) -> None:
        self.servico = montar_servico()
        self.ticket = tickets.create_signup_ticket(identidade_do_google())

    def _concluir(self, **sobrescritas):
        campos = {
            "signup_ticket": self.ticket,
            "phone": "85999998888",
            "birth_date": date(1990, 5, 20),
            "privacy_accepted": True,
        }
        campos.update(sobrescritas)
        return self.servico.complete_signup(GoogleCompleteSignupRequest(**campos))

    def test_cria_o_cliente_com_o_que_o_google_deu_mais_o_que_faltava(self) -> None:
        self._concluir()

        criado = self.servico.customer_repository.created
        self.assertEqual(criado.email, EMAIL)
        self.assertEqual(criado.name, "Pessoa de Teste")
        self.assertEqual(criado.phone, "85999998888")
        self.assertEqual(criado.birth_date, date(1990, 5, 20))

    def test_o_email_ja_nasce_verificado(self) -> None:
        """O Google provou. Pedir codigo aqui seria pedir de novo o que acabou
        de ser provado — e `verify_id_token` ja recusa `email_verified` falso."""
        self._concluir()
        self.assertIsNotNone(self.servico.customer_repository.created.email_verified_at)

    def test_a_senha_nasce_inutilizavel(self) -> None:
        """Ninguem conhece este valor, nem nos. `password_hash` e NOT NULL e o
        Google nao manda senha; quem quiser uma vai por forgot-password."""
        from src.utils.security import verify_password

        self._concluir()
        criado = self.servico.customer_repository.created
        self.assertTrue(criado.password_hash.startswith("$2"))
        self.assertFalse(verify_password("", criado.password_hash))

    def test_liga_a_identidade_pelo_sub(self) -> None:
        self._concluir()

        criada = self.servico.social_identity_repository.created
        self.assertEqual(len(criada), 1)
        self.assertEqual(criada[0]["provider_user_id"], SUB)
        self.assertEqual(criada[0]["provider"], SOCIAL_PROVIDER_GOOGLE)

    def test_devolve_a_sessao(self) -> None:
        resposta = self._concluir()

        criado = self.servico.customer_repository.created
        self.assertEqual(cliente_do_token(resposta.access_token), str(criado.id))
        self.assertEqual(resposta.token_type, "bearer")

    def test_o_nome_do_corpo_ganha_do_nome_do_google(self) -> None:
        self._concluir(name="Maria da Silva")
        self.assertEqual(self.servico.customer_repository.created.name, "Maria da Silva")

    def test_sem_aceite_de_privacidade_e_400(self) -> None:
        with self.assertRaises(HTTPException) as erro:
            self._concluir(privacy_accepted=False)

        self.assertEqual(erro.exception.status_code, 400)
        self.assertIsNone(self.servico.customer_repository.created)

    def test_ticket_de_ligacao_nao_serve_aqui(self) -> None:
        """`purpose` errado: seria o caso (b) sendo resolvido como o (c) —
        conta nova para quem ja tem uma."""
        de_ligacao = tickets.create_link_ticket(identidade_do_google(), uuid.uuid4())
        with self.assertRaises(HTTPException) as erro:
            self._concluir(signup_ticket=de_ligacao)

        self.assertEqual(erro.exception.status_code, 400)
        self.assertIsNone(self.servico.customer_repository.created)

    def test_ticket_forjado_e_400(self) -> None:
        with self.assertRaises(HTTPException) as erro:
            self._concluir(signup_ticket="nao.e.um.jwt")
        self.assertEqual(erro.exception.status_code, 400)


class TestAsCorridasEntreAsDuasTelas(unittest.TestCase):
    """Entre `POST /auth/google` e a conclusao passam minutos."""

    def setUp(self) -> None:
        self.ticket = tickets.create_signup_ticket(identidade_do_google())

    def _concluir(self, servico, **sobrescritas):
        campos = {
            "signup_ticket": self.ticket,
            "phone": "85999998888",
            "birth_date": date(1990, 5, 20),
            "privacy_accepted": True,
        }
        campos.update(sobrescritas)
        return servico.complete_signup(GoogleCompleteSignupRequest(**campos))

    def test_sub_ligado_noutra_aba_e_409(self) -> None:
        ja_ligada = fabricas.identidade_social(provider_user_id=SUB)
        servico = montar_servico(social=FakeSocialIdentityRepository(ja_ligada))

        with self.assertRaises(HTTPException) as erro:
            self._concluir(servico)

        self.assertEqual(erro.exception.status_code, 409)
        self.assertIsNone(servico.customer_repository.created)

    def test_email_que_ganhou_conta_e_409(self) -> None:
        """Ligar seria juntar duas contas sem prova de caixa de entrada, e
        criar seria impossivel (UNIQUE). A saida e recomecar."""
        servico = montar_servico(customer=make_customer(email=EMAIL))

        with self.assertRaises(HTTPException) as erro:
            self._concluir(servico)

        self.assertEqual(erro.exception.status_code, 409)
        self.assertIsNone(servico.customer_repository.created)

    def test_telefone_ja_cadastrado_e_409(self) -> None:
        servico = montar_servico()
        servico.customer_repository.conflict_on = {"phone"}
        servico.customer_repository.customer = make_customer(email="outro@exemplo.com")

        with self.assertRaises(HTTPException) as erro:
            self._concluir(servico)
        self.assertEqual(erro.exception.status_code, 409)


# ============================================================== o Google


class TestAsFalhasDoGoogle(unittest.TestCase):
    """Cada uma diz outra coisa, e o app faz outra coisa com cada uma."""

    def _entrar(self, erro):
        servico = montar_servico(identity_client=FakeIdentityClient(erro=erro))
        with self.assertRaises(HTTPException) as capturado:
            servico.sign_in(GoogleSignInRequest(id_token="qualquer"))
        return capturado.exception

    def test_token_invalido_e_401(self) -> None:
        erro = self._entrar(GoogleIdentityInvalidTokenError("x"))
        self.assertEqual(erro.status_code, 401)

    def test_email_nao_verificado_e_401_com_frase_propria(self) -> None:
        """Recusa e ponto: nao ha "confirme por codigo" aqui. E-mail nao
        verificado no Google e um campo que o dono da conta digitou."""
        erro = self._entrar(GoogleIdentityUnverifiedEmailError("x"))
        self.assertEqual(erro.status_code, 401)
        self.assertIn("Google não confirmou", erro.detail)

    def test_google_fora_do_ar_e_502(self) -> None:
        erro = self._entrar(GoogleIdentityUnavailableError("x"))
        self.assertEqual(erro.status_code, 502)

    def test_servidor_sem_client_id_e_503(self) -> None:
        """Falha DESTE servidor. Um 401 mandaria o app conferir uma
        credencial que esta certa."""
        erro = self._entrar(GoogleIdentityNotConfiguredError("x"))
        self.assertEqual(erro.status_code, 503)


class TestOTicket(unittest.TestCase):
    def test_o_ticket_de_cadastro_nao_vale_como_sessao(self) -> None:
        ticket = tickets.create_signup_ticket(identidade_do_google())
        with self.assertRaises(Exception):
            decode_signed_token(ticket, "customer_access")

    def test_ticket_vencido_e_400_e_nao_500(self) -> None:
        """O caso mais comum de todos: a pessoa deixou a tela aberta. Sem a
        traducao, `TokenExpiredError` sobe ate o FastAPI e vira 500."""
        from datetime import timedelta

        from src.utils.security import create_signed_token

        vencido = create_signed_token(
            subject=SUB,
            purpose=tickets.PURPOSE_SIGNUP,
            expires_delta=timedelta(minutes=-1),
            extra={"email": EMAIL, "name": "Pessoa"},
        )
        with self.assertRaises(HTTPException) as erro:
            tickets.read_signup_ticket(vencido)
        self.assertEqual(erro.exception.status_code, 400)


class TestOsDadosQueOTesteUsa(unittest.TestCase):
    """O par do `pytest.raises`: os dublês descrevem objetos que existem."""

    def test_a_identidade_e_o_dataclass_de_verdade(self) -> None:
        self.assertIsInstance(identidade_do_google(), GoogleIdentity)

    def test_o_codigo_da_fabrica_confere_de_verdade(self) -> None:
        linha = make_code_row()
        self.assertEqual(linha.code_hash, hash_verification_code(CODIGO))
        self.assertGreater(linha.expires_at, utcnow())

    def test_o_cliente_da_fabrica_e_o_model_de_verdade(self) -> None:
        cliente = make_customer()
        self.assertIsInstance(cliente.email_verified_at, datetime)
        self.assertEqual(cliente.email_verified_at.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
