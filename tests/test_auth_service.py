"""Caracterizacao de `services/auth_service.py`.

TESTE DE CARACTERIZACAO: descreve o que o codigo faz HOJE. Comportamento
esquisito fica registrado e verde, com comentario apontando o problema.

Era o modulo com menos rede do repositorio (26%) e o de consequencia mais
direta: e o cadastro, o login e o "esqueci minha senha" do cliente. Duas
propriedades daqui sao de SEGURANCA e nao se veem lendo o codigo depressa:

- **`forgot_password` responde igual para e-mail cadastrado e nao cadastrado**,
  com o mesmo perfil de consultas ao banco e um piso de latencia. Qualquer uma
  das tres quebrada permite enumerar a base de clientes (armadilha 18).
- **`reset_password` derruba as sessoes antigas** gravando
  `password_changed_at`. A troca costuma ser reacao a invasao; sem essa linha
  o invasor continua dentro.

A revogacao de token em si (`_token_was_issued_before_password_change`) tem
arquivo proprio, `test_token_revocation.py`, e nao e repetida aqui.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.schemas.auth_schema import (
    ForgotPasswordRequest,
    LoginRequest,
    RegisterCustomerRequest,
    ResendEmailCodeRequest,
    ResetPasswordRequest,
    VerifyEmailCodeRequest,
    VerifyResetCodeRequest,
)
from src.services.auth_service import (
    CODE_TTL_MINUTES,
    FORGOT_PASSWORD_MESSAGE,
    MAX_CODE_ATTEMPTS,
    MAX_RESENDS,
    RESEND_COOLDOWN_SECONDS,
    RESEND_WINDOW_MINUTES,
    AuthService,
    codes_retention_cutoff,
)
from src.utils.security import (
    generate_reset_token,
    hash_password,
    hash_reset_token,
    hash_verification_code,
    utcnow,
)


SENHA = "senha-forte-123"
CODIGO = "123456"

# O bcrypt e LENTO DE PROPOSITO (~0,3s por hash) — e o que o torna caro de
# atacar. Hasheado uma vez por modulo em vez de uma vez por `make_customer`,
# senao este arquivo sozinho triplica o tempo da suite. O valor nao precisa
# variar entre testes: quem verifica a senha e o `verify_password` real.
SENHA_HASH = hash_password(SENHA)


class FakeDb:
    def __init__(self, falha=None):
        self.events = []
        self.falha = falha

    def add(self, value):
        self.events.append("add")

    def commit(self):
        if self.falha is not None:
            raise self.falha
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeEmailService:
    def __init__(self):
        self.verification_codes = []
        self.reset_codes = []

    def send_email_verification_code(self, email, code):
        self.verification_codes.append((email, code))

    def send_password_reset_code(self, email, code):
        self.reset_codes.append((email, code))


class FakeCustomerRepository:
    """Fake do repositorio de cliente, so com o que o AuthService chama."""

    def __init__(self, customer=None, email_code=None, reset_code=None, recent_codes=0):
        self.customer = customer
        self.email_code = email_code
        self.reset_code = reset_code
        self.recent_codes = recent_codes
        self.created = None
        self.email_codes_created = []
        self.reset_codes_created = []
        self.invalidated_for = []
        # Conflito de cadastro: qual campo responde "ja existe".
        self.conflict_on = set()

    # --- leitura -----------------------------------------------------------
    def get_by_email(self, email):
        return self.customer if "email" in self.conflict_on or self.customer else None

    def get_by_phone(self, phone):
        return self.customer if "phone" in self.conflict_on else None

    def get_by_id(self, customer_id):
        return self.customer

    def get_by_email_or_phone(self, email=None, phone=None):
        return self.customer

    def latest_unused_email_code(self, email):
        return self.email_code

    def latest_unused_password_reset_code(self, email):
        return self.reset_code

    def get_password_reset_by_token_hash(self, token_hash):
        return self.reset_code

    def count_email_codes_since(self, email, since):
        return self.recent_codes

    def count_password_reset_codes_since(self, email, since):
        return self.recent_codes

    # --- escrita -----------------------------------------------------------
    def create(self, **values):
        self.created = SimpleNamespace(id=uuid.uuid4(), **values)
        return self.created

    def create_email_code(self, **values):
        self.email_codes_created.append(values)

    def create_password_reset_code(self, **values):
        self.reset_codes_created.append(values)

    def invalidate_unused_password_reset_codes(self, customer_id):
        self.invalidated_for.append(customer_id)


def make_customer(**overrides):
    valores = {
        "id": uuid.uuid4(),
        "name": "Joana Souza",
        "email": "joana@exemplo.com",
        "phone": "85999998888",
        "birth_date": date(1990, 5, 20),
        "email_verified_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "password_hash": SENHA_HASH,
        "password_changed_at": None,
        "is_active": True,
    }
    valores.update(overrides)
    return SimpleNamespace(**valores)


def make_code_row(code=CODIGO, attempts=0, expires_in_minutes=10, used_at=None, created_at=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        code_hash=hash_verification_code(code),
        attempts_count=attempts,
        expires_at=utcnow() + timedelta(minutes=expires_in_minutes),
        used_at=used_at,
        created_at=created_at,
        resend_count=0,
        reset_token_hash=None,
        reset_token_expires_at=None,
    )


def make_service(db=None, repository=None):
    service = AuthService.__new__(AuthService)
    service.db = db or FakeDb()
    service.customer_repository = repository or FakeCustomerRepository()
    service.email_service = FakeEmailService()
    return service


def make_register_payload(**overrides):
    valores = {
        "name": "Joana Souza",
        "email": "joana@exemplo.com",
        "phone": "(85) 99999-8888",
        "birth_date": date(1990, 5, 20),
        "password": SENHA,
        "privacy_accepted": True,
    }
    valores.update(overrides)
    return RegisterCustomerRequest(**valores)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_it_normalizes_before_saving(self):
        """E-mail e telefone sao normalizados ANTES de gravar. Sem isso a
        busca por telefone nao acha o cliente que digitou com parenteses
        (armadilha 27)."""
        repository = FakeCustomerRepository()
        service = make_service(repository=repository)

        service.register(make_register_payload(email="  JOANA@Exemplo.COM "))

        assert repository.created.email == "joana@exemplo.com"
        assert repository.created.phone == "85999998888"

    def test_it_sends_a_verification_code_and_leaves_the_email_unverified(self):
        repository = FakeCustomerRepository()
        service = make_service(repository=repository)

        response = service.register(make_register_payload())

        assert response.requires_email_verification is True
        assert repository.created.email_verified_at is None
        assert len(service.email_service.verification_codes) == 1

    @pytest.mark.parametrize(
        ("overrides", "detail"),
        [
            ({"email": "nao-e-email"}, "Email invalido"),
            ({"password": "curta12"}, "Senha fraca"),
            ({"password": "x" * 73}, "Senha muito longa"),
            ({"privacy_accepted": False}, "Aceite de privacidade obrigatorio"),
        ],
    )
    def test_the_refusals(self, overrides, detail):
        with pytest.raises(HTTPException) as exc:
            make_service().register(make_register_payload(**overrides))

        assert exc.value.status_code == 400
        assert exc.value.detail == detail

    @pytest.mark.parametrize(
        ("campo", "detail"),
        [("email", "Email ja cadastrado"), ("phone", "Telefone ja cadastrado")],
    )
    def test_a_duplicate_is_409_naming_the_field(self, campo, detail):
        repository = FakeCustomerRepository(customer=make_customer())
        repository.conflict_on = {campo}
        if campo != "email":
            repository.get_by_email = lambda email: None

        with pytest.raises(HTTPException) as exc:
            make_service(repository=repository).register(make_register_payload())

        assert exc.value.status_code == 409
        assert exc.value.detail == detail

    def test_colliding_on_both_names_both(self):
        """Antes so o PRIMEIRO conflito era devolvido.

        Quem colide no e-mail E no telefone e o caso mais comum de todos —
        e a pessoa que ja tem conta e esqueceu. Ela corrigia o e-mail, tentava
        de novo, e so entao descobria o telefone: duas viagens para descobrir
        dois problemas que o servidor ja sabia na primeira.
        """
        repository = FakeCustomerRepository(customer=make_customer())
        repository.conflict_on = {"email", "phone"}

        with pytest.raises(HTTPException) as exc:
            make_service(repository=repository).register(make_register_payload())

        assert exc.value.detail == "Email e Telefone ja cadastrados"

    def test_a_single_conflict_keeps_the_singular(self):
        """A concordancia importa porque a mensagem vai para a tela: um
        "ja cadastrado(s)" resolveria o plural e entregaria a costura."""
        repository = FakeCustomerRepository(customer=make_customer())
        repository.conflict_on = {"email"}

        with pytest.raises(HTTPException) as exc:
            make_service(repository=repository).register(make_register_payload())

        assert exc.value.detail == "Email ja cadastrado"

    def test_a_failure_while_saving_rolls_back(self):
        db = FakeDb(falha=RuntimeError("banco caiu"))

        with pytest.raises(RuntimeError):
            make_service(db=db).register(make_register_payload())

        assert "rollback" in db.events


# ---------------------------------------------------------------------------
# verify_email_code
# ---------------------------------------------------------------------------


class TestVerifyEmailCode:
    def test_the_right_code_verifies_and_burns_the_row(self):
        customer = make_customer(email_verified_at=None)
        code_row = make_code_row()
        service = make_service(repository=FakeCustomerRepository(customer=customer, email_code=code_row))

        response = service.verify_email_code(VerifyEmailCodeRequest(email=customer.email, code=CODIGO))

        assert response.verified is True
        assert customer.email_verified_at is not None
        assert code_row.used_at is not None

    def test_an_unknown_customer_is_404(self):
        service = make_service(repository=FakeCustomerRepository(customer=None))

        with pytest.raises(HTTPException) as exc:
            service.verify_email_code(VerifyEmailCodeRequest(email="ninguem@exemplo.com", code=CODIGO))

        assert exc.value.status_code == 404

    def test_no_code_row_is_treated_as_expired(self):
        service = make_service(repository=FakeCustomerRepository(customer=make_customer(), email_code=None))

        with pytest.raises(HTTPException) as exc:
            service.verify_email_code(VerifyEmailCodeRequest(email="joana@exemplo.com", code=CODIGO))

        assert exc.value.detail == "Codigo expirado"

    def test_too_many_attempts_is_429(self):
        """O teto de tentativas e o que impede varrer os 10^6 codigos de 6
        digitos por forca bruta."""
        code_row = make_code_row(attempts=MAX_CODE_ATTEMPTS)
        service = make_service(repository=FakeCustomerRepository(customer=make_customer(), email_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.verify_email_code(VerifyEmailCodeRequest(email="joana@exemplo.com", code=CODIGO))

        assert exc.value.status_code == 429

    def test_an_expired_code_is_400(self):
        code_row = make_code_row(expires_in_minutes=-1)
        service = make_service(repository=FakeCustomerRepository(customer=make_customer(), email_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.verify_email_code(VerifyEmailCodeRequest(email="joana@exemplo.com", code=CODIGO))

        assert exc.value.detail == "Codigo expirado"

    def test_a_wrong_code_counts_the_attempt_and_commits_it(self):
        """A tentativa errada e GRAVADA antes de responder. Se o incremento
        ficasse so em memoria, o teto de 5 nunca seria atingido."""
        code_row = make_code_row(attempts=2)
        db = FakeDb()
        service = make_service(db=db, repository=FakeCustomerRepository(customer=make_customer(), email_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.verify_email_code(VerifyEmailCodeRequest(email="joana@exemplo.com", code="000000"))

        assert exc.value.detail == "Codigo invalido"
        assert code_row.attempts_count == 3
        assert "commit" in db.events


# ---------------------------------------------------------------------------
# resend_email_code
# ---------------------------------------------------------------------------


class TestResendEmailCode:
    def test_it_sends_a_new_code_for_an_unverified_customer(self):
        customer = make_customer(email_verified_at=None)
        service = make_service(repository=FakeCustomerRepository(customer=customer))

        service.resend_email_code(ResendEmailCodeRequest(email=customer.email))

        assert len(service.email_service.verification_codes) == 1

    @pytest.mark.parametrize(
        "repository",
        [
            FakeCustomerRepository(customer=None),  # e-mail nao cadastrado
            FakeCustomerRepository(customer=make_customer()),  # ja verificado
        ],
    )
    def test_nothing_is_sent_when_there_is_nothing_to_verify(self, repository):
        service = make_service(repository=repository)

        service.resend_email_code(ResendEmailCodeRequest(email="joana@exemplo.com"))

        assert service.email_service.verification_codes == []

    def test_the_answer_is_the_same_whether_the_email_exists_or_not(self):
        """Mesma frase nos dois casos — senao o reenvio vira sonda de
        cadastro."""
        existente = make_service(repository=FakeCustomerRepository(customer=make_customer(email_verified_at=None)))
        inexistente = make_service(repository=FakeCustomerRepository(customer=None))

        a = existente.resend_email_code(ResendEmailCodeRequest(email="joana@exemplo.com"))
        b = inexistente.resend_email_code(ResendEmailCodeRequest(email="ninguem@exemplo.com"))

        assert a.message == b.message

    def test_a_code_sent_seconds_ago_blocks_a_new_one(self):
        recente = make_code_row(created_at=utcnow() - timedelta(seconds=RESEND_COOLDOWN_SECONDS - 5))
        service = make_service(
            repository=FakeCustomerRepository(customer=make_customer(email_verified_at=None), email_code=recente)
        )

        service.resend_email_code(ResendEmailCodeRequest(email="joana@exemplo.com"))

        assert service.email_service.verification_codes == []

    def test_past_the_cooldown_a_new_code_goes_out(self):
        antigo = make_code_row(created_at=utcnow() - timedelta(seconds=RESEND_COOLDOWN_SECONDS + 5))
        service = make_service(
            repository=FakeCustomerRepository(customer=make_customer(email_verified_at=None), email_code=antigo)
        )

        service.resend_email_code(ResendEmailCodeRequest(email="joana@exemplo.com"))

        assert len(service.email_service.verification_codes) == 1

    def test_the_window_cap_blocks_the_fourth_send(self):
        service = make_service(
            repository=FakeCustomerRepository(customer=make_customer(email_verified_at=None), recent_codes=MAX_RESENDS)
        )

        service.resend_email_code(ResendEmailCodeRequest(email="joana@exemplo.com"))

        assert service.email_service.verification_codes == []


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_a_verified_active_customer_gets_a_token(self):
        service = make_service(repository=FakeCustomerRepository(customer=make_customer()))

        response = service.login(LoginRequest(login="joana@exemplo.com", password=SENHA))

        assert response.access_token

    def test_an_unknown_customer_is_401(self):
        service = make_service(repository=FakeCustomerRepository(customer=None))

        with pytest.raises(HTTPException) as exc:
            service.login(LoginRequest(login="ninguem@exemplo.com", password=SENHA))

        assert exc.value.status_code == 401

    def test_the_wrong_password_is_401_with_the_same_message(self):
        """A mesma frase de "cliente nao existe": distinguir as duas diria
        quais e-mails estao cadastrados."""
        service = make_service(repository=FakeCustomerRepository(customer=make_customer()))

        with pytest.raises(HTTPException) as exc:
            service.login(LoginRequest(login="joana@exemplo.com", password="errada"))

        assert exc.value.status_code == 401
        assert exc.value.detail == "Credenciais invalidas"

    def test_an_inactive_account_is_403(self):
        service = make_service(repository=FakeCustomerRepository(customer=make_customer(is_active=False)))

        with pytest.raises(HTTPException) as exc:
            service.login(LoginRequest(login="joana@exemplo.com", password=SENHA))

        assert exc.value.status_code == 403

    def test_an_unverified_email_is_not_an_error_it_is_an_answer(self):
        """Nao levanta: devolve `requires_email_verification=True` e o e-mail,
        para a tela saber para onde mandar o cliente."""
        service = make_service(repository=FakeCustomerRepository(customer=make_customer(email_verified_at=None)))

        response = service.login(LoginRequest(login="joana@exemplo.com", password=SENHA))

        assert response.requires_email_verification is True
        assert response.email == "joana@exemplo.com"

    def test_a_login_with_a_phone_is_accepted(self):
        """O campo e `login`, nao `email`: com `@` vira e-mail, sem `@` vira
        telefone normalizado."""
        service = make_service(repository=FakeCustomerRepository(customer=make_customer()))

        assert service.login(LoginRequest(login="(85) 99999-8888", password=SENHA)).access_token


# ---------------------------------------------------------------------------
# forgot_password — as tres propriedades de seguranca
# ---------------------------------------------------------------------------


class TestForgotPassword:
    def test_the_message_is_identical_for_a_known_and_an_unknown_email(self):
        conhecido = make_service(repository=FakeCustomerRepository(customer=make_customer()))
        desconhecido = make_service(repository=FakeCustomerRepository(customer=None))

        a = conhecido.forgot_password(ForgotPasswordRequest(email="joana@exemplo.com"))
        b = desconhecido.forgot_password(ForgotPasswordRequest(email="ninguem@exemplo.com"))

        assert a.message == b.message == FORGOT_PASSWORD_MESSAGE

    def test_a_known_email_receives_the_code(self):
        service = make_service(repository=FakeCustomerRepository(customer=make_customer()))

        service.forgot_password(ForgotPasswordRequest(email="joana@exemplo.com"))

        assert len(service.email_service.reset_codes) == 1

    def test_an_unknown_email_receives_nothing(self):
        service = make_service(repository=FakeCustomerRepository(customer=None))

        service.forgot_password(ForgotPasswordRequest(email="ninguem@exemplo.com"))

        assert service.email_service.reset_codes == []

    def test_an_internal_failure_is_swallowed_and_still_answers_the_same(self):
        """A falha e registrada no log e NAO propagada: um 500 so para e-mail
        existente seria a mesma sonda que a mensagem diferente."""
        service = make_service(
            db=FakeDb(falha=RuntimeError("banco caiu")),
            repository=FakeCustomerRepository(customer=make_customer()),
        )

        response = service.forgot_password(ForgotPasswordRequest(email="joana@exemplo.com"))

        assert response.message == FORGOT_PASSWORD_MESSAGE

    def test_the_cooldown_applies_here_too(self):
        recente = make_code_row(created_at=utcnow() - timedelta(seconds=RESEND_COOLDOWN_SECONDS - 5))
        service = make_service(
            repository=FakeCustomerRepository(customer=make_customer(), reset_code=recente)
        )

        service.forgot_password(ForgotPasswordRequest(email="joana@exemplo.com"))

        assert service.email_service.reset_codes == []


# ---------------------------------------------------------------------------
# verify_reset_code e reset_password
# ---------------------------------------------------------------------------


class TestVerifyResetCode:
    def test_the_right_code_issues_a_reset_token(self):
        code_row = make_code_row()
        service = make_service(repository=FakeCustomerRepository(reset_code=code_row))

        response = service.verify_reset_code(VerifyResetCodeRequest(email="joana@exemplo.com", code=CODIGO))

        assert response.reset_token
        # O token sai em claro para o cliente e HASHEADO para o banco.
        assert code_row.reset_token_hash == hash_reset_token(response.reset_token)
        assert code_row.reset_token_hash != response.reset_token

    def test_no_code_row_is_400(self):
        service = make_service(repository=FakeCustomerRepository(reset_code=None))

        with pytest.raises(HTTPException) as exc:
            service.verify_reset_code(VerifyResetCodeRequest(email="joana@exemplo.com", code=CODIGO))

        assert exc.value.status_code == 400

    def test_too_many_attempts_is_429(self):
        code_row = make_code_row(attempts=MAX_CODE_ATTEMPTS)
        service = make_service(repository=FakeCustomerRepository(reset_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.verify_reset_code(VerifyResetCodeRequest(email="joana@exemplo.com", code=CODIGO))

        assert exc.value.status_code == 429

    def test_a_wrong_code_counts_the_attempt(self):
        code_row = make_code_row(attempts=1)
        service = make_service(repository=FakeCustomerRepository(reset_code=code_row))

        with pytest.raises(HTTPException):
            service.verify_reset_code(VerifyResetCodeRequest(email="joana@exemplo.com", code="000000"))

        assert code_row.attempts_count == 2


class TestResetPassword:
    def make_valid_token_row(self, customer_id):
        token = generate_reset_token()
        code_row = make_code_row()
        code_row.customer_id = customer_id
        code_row.reset_token_hash = hash_reset_token(token)
        code_row.reset_token_expires_at = utcnow() + timedelta(minutes=15)
        return token, code_row

    def test_it_changes_the_password_and_drops_the_old_sessions(self):
        """A linha que importa e `password_changed_at`: a troca por "esqueci
        minha senha" costuma ser reacao a invasao, e e ela que expulsa quem
        estava dentro."""
        customer = make_customer()
        token, code_row = self.make_valid_token_row(customer.id)
        repository = FakeCustomerRepository(customer=customer, reset_code=code_row)
        service = make_service(repository=repository)

        service.reset_password(
            ResetPasswordRequest(reset_token=token, new_password="nova-senha-123", confirm_password="nova-senha-123")
        )

        assert customer.password_changed_at is not None
        assert code_row.used_at is not None
        assert repository.invalidated_for == [customer.id]

    @pytest.mark.parametrize(
        ("new", "confirm", "detail"),
        [
            ("nova-senha-123", "outra-coisa", "Confirmacao de senha nao confere"),
            ("curta12", "curta12", "Senha fraca"),
            ("x" * 73, "x" * 73, "Senha muito longa"),
        ],
    )
    def test_the_password_is_checked_before_the_token(self, new, confirm, detail):
        """ESQUISITO, e registrado como esta.

        As tres validacoes de senha rodam ANTES de conferir o reset_token.
        Quem manda um token invalido com senhas que nao conferem ouve
        "confirmacao nao confere" — uma dica sobre o formulario para quem nem
        tem token valido.

        Nao vaza cadastro e nao permite trocar senha nenhuma, mas e a ordem
        contraria a de todo o resto do modulo, onde a credencial e conferida
        primeiro. Nao e corrigido aqui.
        """
        service = make_service(repository=FakeCustomerRepository(reset_code=None))

        with pytest.raises(HTTPException) as exc:
            service.reset_password(
                ResetPasswordRequest(reset_token="token-que-nao-existe", new_password=new, confirm_password=confirm)
            )

        assert exc.value.detail == detail

    def test_an_unknown_token_is_400(self):
        service = make_service(repository=FakeCustomerRepository(reset_code=None))

        with pytest.raises(HTTPException) as exc:
            service.reset_password(
                ResetPasswordRequest(
                    reset_token="token-que-nao-existe",
                    new_password="nova-senha-123",
                    confirm_password="nova-senha-123",
                )
            )

        assert exc.value.detail == "Token invalido ou expirado"

    def test_an_already_used_token_is_refused(self):
        customer = make_customer()
        token, code_row = self.make_valid_token_row(customer.id)
        code_row.used_at = utcnow()
        service = make_service(repository=FakeCustomerRepository(customer=customer, reset_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.reset_password(
                ResetPasswordRequest(
                    reset_token=token, new_password="nova-senha-123", confirm_password="nova-senha-123"
                )
            )

        assert exc.value.detail == "Token invalido ou expirado"

    def test_an_expired_token_is_refused(self):
        customer = make_customer()
        token, code_row = self.make_valid_token_row(customer.id)
        code_row.reset_token_expires_at = utcnow() - timedelta(seconds=1)
        service = make_service(repository=FakeCustomerRepository(customer=customer, reset_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.reset_password(
                ResetPasswordRequest(
                    reset_token=token, new_password="nova-senha-123", confirm_password="nova-senha-123"
                )
            )

        assert exc.value.detail == "Token invalido ou expirado"

    def test_a_token_that_does_not_match_the_stored_hash_is_refused(self):
        customer = make_customer()
        _, code_row = self.make_valid_token_row(customer.id)
        service = make_service(repository=FakeCustomerRepository(customer=customer, reset_code=code_row))

        with pytest.raises(HTTPException) as exc:
            service.reset_password(
                ResetPasswordRequest(
                    reset_token=generate_reset_token(),
                    new_password="nova-senha-123",
                    confirm_password="nova-senha-123",
                )
            )

        assert exc.value.detail == "Token invalido ou expirado"


# ---------------------------------------------------------------------------
# get_customer_from_token_or_error
# ---------------------------------------------------------------------------


class TestGetCustomerFromTokenOrError:
    def test_garbage_is_401(self):
        with pytest.raises(HTTPException) as exc:
            make_service().get_customer_from_token_or_error("nao.e.jwt")

        assert exc.value.status_code == 401
        assert exc.value.detail == "Token invalido"

    def test_an_expired_token_says_so(self):
        """"Expirado" e "invalido" respondem os dois 401, com mensagens
        diferentes: a tela reage diferente a cada um."""
        from src.utils.security import create_signed_token

        token = create_signed_token("c", "customer_access", timedelta(seconds=-10), extra={"type": "customer"})

        with pytest.raises(HTTPException) as exc:
            make_service().get_customer_from_token_or_error(token)

        assert exc.value.detail == "Token expirado"

    def test_an_inactive_account_is_403_not_401(self):
        from src.utils.security import create_signed_token

        customer = make_customer(is_active=False)
        token = create_signed_token(
            str(customer.id), "customer_access", timedelta(minutes=5), extra={"type": "customer"}
        )
        service = make_service(repository=FakeCustomerRepository(customer=customer))

        with pytest.raises(HTTPException) as exc:
            service.get_customer_from_token_or_error(token)

        assert exc.value.status_code == 403

    def test_a_token_without_the_customer_type_is_refused(self):
        """O `purpose` ja separa admin de cliente; este `type` e a segunda
        tranca. Um token de outro uso nao vira sessao de cliente."""
        from src.utils.security import create_signed_token

        customer = make_customer()
        token = create_signed_token(str(customer.id), "customer_access", timedelta(minutes=5))
        service = make_service(repository=FakeCustomerRepository(customer=customer))

        with pytest.raises(HTTPException) as exc:
            service.get_customer_from_token_or_error(token)

        assert exc.value.status_code == 401

    def test_a_valid_token_returns_the_customer(self):
        from src.utils.security import create_signed_token

        customer = make_customer()
        token = create_signed_token(
            str(customer.id), "customer_access", timedelta(minutes=5), extra={"type": "customer"}
        )
        service = make_service(repository=FakeCustomerRepository(customer=customer))

        assert service.get_customer_from_token_or_error(token) is customer


class TestCodesRetentionCutoff:
    """Ate quando a linha de um codigo de verificacao ainda importa.

    As duas tabelas guardavam o e-mail de todo mundo que ja pediu um codigo,
    para sempre — dado pessoal sem prazo, que foi o que a frente 5 apontou.
    Mas apagar no VENCIMENTO do codigo quebraria duas coisas, e e isso que
    estes testes seguram.
    """

    def test_it_keeps_the_row_the_resend_cap_still_needs(self):
        """O teto de reenvios olha 15 minutos para tras e o codigo vence em
        10. Apagar no vencimento apagaria a prova do terceiro reenvio, e quem
        tivesse batido no teto pediria de novo — o controle viraria enfeite."""
        agora = datetime.now(timezone.utc)
        criado_ha_12_min = agora - timedelta(minutes=12)

        assert CODE_TTL_MINUTES < 12 < RESEND_WINDOW_MINUTES
        assert criado_ha_12_min > codes_retention_cutoff(agora)

    def test_it_keeps_the_row_an_in_flight_reset_still_needs(self):
        """O token de reset nasce quando o codigo e CONFERIDO e vale mais 15
        minutos. Um codigo conferido no minuto 9 gera token valido ate o 24,
        com o expires_at da linha ja no passado. Apagar por expires_at
        derrubaria uma troca de senha em andamento."""
        agora = datetime.now(timezone.utc)
        criado_ha_24_min = agora - timedelta(minutes=24)

        assert criado_ha_24_min > codes_retention_cutoff(agora)

    def test_it_does_delete_what_nothing_needs_anymore(self):
        agora = datetime.now(timezone.utc)
        criado_ha_2_horas = agora - timedelta(hours=2)

        assert criado_ha_2_horas < codes_retention_cutoff(agora)

    def test_the_cutoff_follows_the_widest_window(self):
        """E conta, e nao constante, de proposito: quem aumentar a janela de
        reenvio ou o TTL do token de reset nao precisa lembrar de vir aqui."""
        agora = datetime.now(timezone.utc)
        corte = codes_retention_cutoff(agora)

        assert (agora - corte).total_seconds() / 60 > RESEND_WINDOW_MINUTES
        assert (agora - corte).total_seconds() / 60 > CODE_TTL_MINUTES
