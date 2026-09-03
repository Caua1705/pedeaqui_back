import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta
from time import perf_counter

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.customer_model import Customer, PasswordResetCode
from src.repositories.customer_repository import CustomerRepository
from src.repositories.customer_social_identity_repository import (
    CustomerSocialIdentityRepository,
)
from src.schemas.auth_schema import (
    ForgotPasswordRequest,
    LoginRequest,
    LoginCustomerResponse,
    LoginResponse,
    MessageResponse,
    RegisterCustomerRequest,
    RegisterCustomerResponse,
    ResetPasswordRequest,
    ResendEmailCodeRequest,
    VerifyEmailCodeRequest,
    VerifyEmailCodeResponse,
    VerifyResetCodeRequest,
    VerifyResetCodeResponse,
)
from src.services.email_service import EmailService
from src.services.google_signin_tickets import read_link_ticket
from src.utils.normalization import is_valid_email, normalize_digits, normalize_email
from src.utils.security import (
    PasswordTooLongError,
    TokenExpiredError,
    TokenInvalidError,
    create_signed_token,
    decode_signed_token,
    generate_6_digit_code,
    generate_reset_token,
    hash_reset_token,
    hash_verification_code,
    hash_password,
    token_was_issued_before_password_change,
    utcnow,
    verify_password,
    verify_reset_token,
    verify_verification_code,
)


logger = logging.getLogger("uvicorn.error")

MAX_CODE_ATTEMPTS = 5
# Folga sobre a maior janela que ainda depende da linha do codigo. Existe
# para o relogio do cron nao precisar ser exato, e nao por causa de nenhuma
# regra: 10 minutos e barato e tira a discussao.
CODES_RETENTION_MARGIN_MINUTES = 10

MAX_RESENDS = 3
RESEND_COOLDOWN_SECONDS = 60
CODE_TTL_MINUTES = 10
RESEND_WINDOW_MINUTES = 15

RESEND_EMAIL_CODE_MESSAGE = "Se este e-mail estiver cadastrado, enviaremos um código de verificação."
FORGOT_PASSWORD_MESSAGE = "Enviamos um código de recuperação para o seu e-mail."
# Piso de latencia para que o tempo de resposta nao denuncie se o e-mail existe.
FORGOT_PASSWORD_MIN_SECONDS = 0.6


def _conflict_message(conflicts: list[str]) -> str:
    """"Email ja cadastrado" ou "Email e Telefone ja cadastrados".

    A concordancia existe porque esta mensagem vai para a tela do cliente. Um
    "ja cadastrado(s)" resolveria o plural e entregaria a costura junto.
    """
    if len(conflicts) == 1:
        return f"{conflicts[0]} já cadastrado"
    return f"{' e '.join(conflicts)} já cadastrados"


def codes_retention_cutoff(now: datetime) -> datetime:
    """Antes deste instante, a linha do codigo nao serve mais para nada.

    NAO e o vencimento do codigo, e a diferenca nao e teorica: DUAS coisas
    continuam dependendo da linha depois de o codigo vencer.

    1. **O teto de reenvios olha 15 minutos para tras, e o codigo vence em
       10.** Apagar no vencimento apagaria a prova do terceiro reenvio na
       faixa de 10 a 15 minutos, e quem tivesse batido no teto pediria de
       novo — o controle de abuso viraria enfeite.
    2. **O token de reset nasce quando o codigo e CONFERIDO**, e vale
       `PASSWORD_RESET_TOKEN_MINUTES` a partir dali. Um codigo conferido no
       minuto 9 gera um token valido ate o minuto 24, com o `expires_at` da
       linha ja no passado. Apagar pelo `expires_at` derrubaria uma troca de
       senha em andamento — e o cliente veria "link invalido" sem ter feito
       nada errado.

    Por isso o corte sai da MAIOR das duas janelas, e nao do TTL. Se alguem
    aumentar `RESEND_WINDOW_MINUTES` ou o TTL do token, a retencao acompanha
    sozinha: e a razao de isto ser conta e nao constante.
    """
    janelas = (
        RESEND_WINDOW_MINUTES,
        CODE_TTL_MINUTES + settings.PASSWORD_RESET_TOKEN_MINUTES,
    )
    return now - timedelta(minutes=max(janelas) + CODES_RETENTION_MARGIN_MINUTES)


#: Marca de "esta conta nao tem senha". A convencao e a do Django, e o
#: caractere e escolhido por nao aparecer em hash nenhum: bcrypt comeca com
#: `$2`, e o formato antigo com `pbkdf2_sha256$`.
UNUSABLE_PASSWORD_PREFIX = "!"


def unusable_password_hash() -> str:
    """Uma senha que nao existe, e que se RECONHECE como inexistente.

    Duas coisas dentro de um valor so, e as duas sao necessarias:

    1. **hash de um segredo sorteado e jogado fora** — o que `_anonymize_customer`
       ja faz. Ninguem conhece essa senha, nem nos, e ela passa pelo bcrypt de
       verdade: a recusa nao depende de como `verify_password` trata lixo;
    2. **o prefixo `!`**, que e o que permite PERGUNTAR se ha senha. Sem ele,
       um bcrypt aleatorio e indistinguivel de uma senha real, e a tela do app
       so descobriria que nao ha senha oferecendo "alterar senha" e recebendo
       "senha atual incorreta" — sem nada que a pessoa possa fazer a respeito.

    O prefixo tambem fecha por conta propria: `!$2b$...` nao comeca com `$2`,
    entao `verify_password` cai no formato antigo, o `split` devolve `!` como
    algoritmo e a resposta e `False`. Sao duas recusas independentes.

    Quem nasce assim e a conta criada pelo "entrar com Google": `password_hash`
    e NOT NULL e o Google nao manda senha. Quem quiser uma senha passa por
    `/auth/forgot-password`, que manda codigo para o e-mail que o Google ja
    verificou.
    """
    return UNUSABLE_PASSWORD_PREFIX + hash_password(secrets.token_urlsafe(32))


def password_is_set(customer: Customer) -> bool:
    """Se esta conta tem uma senha que pode ser usada.

    Falso so para quem entrou pelo Google e nunca definiu uma. E o que o app
    precisa saber para oferecer "definir senha" em vez de "alterar senha" — e
    para explicar por que a exclusao de conta, que exige a senha atual, ainda
    nao esta disponivel para aquela conta.
    """
    if not customer.password_hash:
        return False
    return not customer.password_hash.startswith(UNUSABLE_PASSWORD_PREFIX)


def issue_customer_token(customer: Customer) -> str:
    """O token de sessao do cliente. UMA construcao, para toda porta de login.

    Duas copias divergiriam no dia em que uma delas ganhasse um campo, e a que
    ficasse para tras produziria sessao que o resto do sistema le pela metade.
    """
    return create_signed_token(
        subject=str(customer.id),
        purpose="customer_access",
        expires_delta=timedelta(minutes=settings.CUSTOMER_ACCESS_TOKEN_MINUTES),
        extra={"type": "customer"},
    )


def customer_response(customer: Customer) -> LoginCustomerResponse:
    return LoginCustomerResponse(
        id=customer.id,
        name=customer.name,
        email=customer.email,
        phone=customer.phone,
        email_verified=customer.email_verified_at is not None,
    )


def _hash_new_password(password: str) -> str:
    """A senha nova, conferida e hasheada.

    As duas recusas moram juntas porque respondem a mesma pergunta — "esta
    senha serve?" — e porque `register` e `reset_password` faziam a dupla
    inteira, cada um com sua copia. Duas copias de uma regra de senha e onde
    uma delas fica para tras no dia em que o piso mudar de 8 para 10.

    O teto de 72 BYTES vem do bcrypt, que trunca em silencio acima disso: sem
    virar 400 aqui, duas senhas diferentes com os mesmos 72 primeiros bytes
    autenticariam uma a outra.
    """
    if len(password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Senha fraca")
    try:
        return hash_password(password)
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Senha muito longa") from exc


def _is_within_resend_cooldown(code_row) -> bool:
    """Se o ultimo codigo saiu ha menos de `RESEND_COOLDOWN_SECONDS`.

    Sem `created_at` nao da para datar o codigo, e a ausencia e tratada como
    "fora do cooldown" — errar para o lado de reenviar e barato; errar para o
    outro deixaria o cliente sem conseguir um codigo novo.
    """
    if code_row is None or code_row.created_at is None:
        return False
    return (utcnow() - code_row.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS


def _pad_latency(started_at: float, minimum_seconds: float) -> None:
    remaining = minimum_seconds - (perf_counter() - started_at)
    if remaining > 0:
        time.sleep(remaining)


def _token_was_issued_before_password_change(payload: dict, customer: Customer) -> bool:
    """A regra vive em `utils/security`, compartilhada com o lojista.

    Antes o token roubado sobrevivia ate 7 dias a troca de senha da vitima
    (CUSTOMER_ACCESS_TOKEN_MINUTES).
    """
    return token_was_issued_before_password_change(
        payload, customer.password_changed_at
    )


class AuthService:
    def __init__(self, db: Session):
        self.db = db
        self.customer_repository = CustomerRepository(db)
        # Atributo, e nao construido na hora do uso: e o que permite ligar o
        # dublê nas DUAS camadas na suite rapida (armadilha 25.1).
        self.social_identity_repository = CustomerSocialIdentityRepository(db)
        self.email_service = EmailService()

    def register(self, payload: RegisterCustomerRequest) -> RegisterCustomerResponse:
        email = normalize_email(payload.email)
        phone = normalize_digits(payload.phone)

        if not is_valid_email(email):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email inválido")
        password_hash = _hash_new_password(payload.password)
        if not payload.privacy_accepted:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Aceite de privacidade obrigatório")

        conflicts = self._registration_conflicts(email=email, phone=phone)
        if conflicts:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_conflict_message(conflicts),
            )

        try:
            customer = self.customer_repository.create(
                name=payload.name.strip(),
                email=email,
                phone=phone,
                password_hash=password_hash,
                birth_date=payload.birth_date,
                email_verified_at=None,
                phone_verified_at=None,
                marketing_opt_in=payload.marketing_opt_in,
                privacy_accepted_at=utcnow(),
                is_active=True,
            )
            self._create_email_verification_code(customer)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return RegisterCustomerResponse(
            customer_id=customer.id,
            email=customer.email,
            requires_email_verification=True,
            message="Codigo de verificacao enviado para seu e-mail.",
        )

    def verify_email_code(self, payload: VerifyEmailCodeRequest) -> VerifyEmailCodeResponse:
        """Confere o codigo. Com `google_link_ticket`, LIGA a conta do Google.

        O caminho do cadastro por e-mail nao mudou uma linha: sem ticket, a
        rota faz o que sempre fez e devolve o que sempre devolveu (os campos
        novos da resposta tem default). O segundo comportamento e PEDIDO pelo
        corpo, e nao deduzido de estado escondido no servidor — e por isso o
        cadastro comum nao consegue cair nele por acidente.

        **E ligar NUNCA cria cliente.** O caso (b) e justamente aquele em que a
        conta ja existe: um `create` aqui seria a segunda conta com o mesmo
        e-mail. Quem liga e `link_identity_after_code`, que so recebe o
        `customer` que esta funcao ja achou.
        """
        email = normalize_email(payload.email)
        customer = self.customer_repository.get_by_email(email)
        if not customer:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cliente não encontrado")

        code_row = self.customer_repository.latest_unused_email_code(email)
        if not code_row:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código expirado")

        if code_row.attempts_count >= MAX_CODE_ATTEMPTS:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Muitas tentativas")
        if code_row.expires_at < utcnow():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código expirado")

        if not verify_verification_code(payload.code, code_row.code_hash):
            code_row.attempts_count += 1
            self.db.add(code_row)
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código inválido")

        customer.email_verified_at = utcnow()
        code_row.used_at = utcnow()
        self.db.add(customer)
        self.db.add(code_row)

        if payload.google_link_ticket:
            return self._verified_and_linked(customer, payload.google_link_ticket)

        self.db.commit()
        return VerifyEmailCodeResponse(verified=True, message="E-mail verificado com sucesso.")

    def _verified_and_linked(
        self, customer: Customer, google_link_ticket: str
    ) -> VerifyEmailCodeResponse:
        """O codigo voltou certo E veio ticket: liga, e devolve a sessao.

        O commit e UM so, com a marcacao do codigo e a identidade dentro dele:
        commitar o codigo antes da ligacao queimaria a prova de caixa de
        entrada num caminho que ainda pode recusar (409 de ticket velho), e a
        pessoa teria que pedir outro codigo sem entender por que.

        A conta inativa e barrada AQUI e nao no `POST /auth/google`: la, negar
        de forma distinguivel diria a quem tem um Google com aquele e-mail
        qual e o estado da conta.
        """
        # Import local, e nao no topo: `google_auth_service` importa este
        # modulo, e no topo os dois se importariam em circulo. A seta que
        # existe e google -> auth; esta chamada e a unica de volta.
        from src.services.google_auth_service import link_identity_after_code

        ticket = read_link_ticket(google_link_ticket)
        if not customer.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")

        identidade = link_identity_after_code(
            self.social_identity_repository, customer, ticket
        )
        self.db.commit()
        return VerifyEmailCodeResponse(
            verified=True,
            message="Conta do Google ligada com sucesso.",
            linked_provider=identidade.provider,
            access_token=issue_customer_token(customer),
            token_type="bearer",
            customer=customer_response(customer),
        )

    def send_link_confirmation_code(self, customer: Customer) -> None:
        """O codigo do caso (b) do "entrar com Google".

        MESMA maquinaria do cadastro por e-mail — mesma tabela, mesmo HMAC,
        mesmo TTL, mesmo cooldown e mesmo teto de reenvios por e-mail. Nao ha
        um segundo tipo de codigo, e nao deve haver: dois seriam duas janelas,
        dois tetos e duas retencoes para envelhecer separado.

        **O cooldown e o teto sao o que impede esta rota de virar uma metralhadora
        de e-mail.** Quem tem uma conta do Google com o e-mail de alguem pode
        chamar `POST /auth/google` em laco; sem estes dois, cada chamada seria
        uma mensagem na caixa de entrada da pessoa.

        Silenciosa quando o teto fecha, como `resend_email_code`: a resposta da
        rota nao muda, e nao ha o que o app faca de diferente.
        """
        latest = self.customer_repository.latest_unused_email_code(customer.email)
        if _is_within_resend_cooldown(latest):
            return
        if self._email_codes_in_window(customer.email) >= MAX_RESENDS:
            return

        resend_count = (latest.resend_count + 1) if latest else 0
        self._create_email_verification_code(customer, resend_count=resend_count)
        self.db.commit()

    def resend_email_code(self, payload: ResendEmailCodeRequest) -> MessageResponse:
        # A MESMA resposta em todo caminho, inclusive e-mail inexistente:
        # variar a mensagem transformaria o reenvio numa sonda de quais
        # e-mails estao cadastrados (armadilha 18).
        answer = MessageResponse(message=RESEND_EMAIL_CODE_MESSAGE)

        email = normalize_email(payload.email)
        customer = self.customer_repository.get_by_email(email)
        if not customer:
            return answer
        if customer.email_verified_at:
            return answer

        latest = self.customer_repository.latest_unused_email_code(email)
        if _is_within_resend_cooldown(latest):
            return answer
        if self._email_codes_in_window(email) >= MAX_RESENDS:
            return answer

        resend_count = (latest.resend_count + 1) if latest else 0
        self._create_email_verification_code(customer, resend_count=resend_count)
        self.db.commit()
        return answer

    def _email_codes_in_window(self, email: str) -> int:
        return self.customer_repository.count_email_codes_since(
            email=email,
            since=utcnow() - timedelta(minutes=RESEND_WINDOW_MINUTES),
        )

    def login(self, payload: LoginRequest) -> LoginResponse:
        identifier = payload.login.strip()
        email = normalize_email(identifier) if "@" in identifier else None
        phone = normalize_digits(identifier) if "@" not in identifier else None
        customer = self.customer_repository.get_by_email_or_phone(email=email, phone=phone)

        if not customer or not verify_password(payload.password, customer.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")
        if not customer.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")
        if not customer.email_verified_at:
            return LoginResponse(
                requires_email_verification=True,
                email=customer.email,
                message="Verifique seu e-mail para continuar.",
            )

        return LoginResponse(
            access_token=issue_customer_token(customer),
            token_type="bearer",
            customer=customer_response(customer),
        )

    def forgot_password(self, payload: ForgotPasswordRequest) -> MessageResponse:
        started_at = perf_counter()
        try:
            self._issue_password_reset_code(normalize_email(payload.email))
        except Exception:
            # A resposta precisa ser indistinguivel entre e-mail existente e
            # inexistente, entao a falha e apenas registrada e nao propagada.
            self.db.rollback()
            logger.exception("[Auth] forgot_password_failed")
        finally:
            _pad_latency(started_at, FORGOT_PASSWORD_MIN_SECONDS)
        return MessageResponse(message=FORGOT_PASSWORD_MESSAGE)

    def _issue_password_reset_code(self, email: str) -> None:
        # AS CONSULTAS RODAM MESMO SEM CLIENTE CADASTRADO, e por isso o
        # `customer` NAO e um retorno cedo aqui: o perfil de acesso ao banco
        # precisa ser o mesmo nos dois caminhos, senao o tempo de resposta
        # denuncia quais e-mails existem (armadilha 18).
        customer = self.customer_repository.get_by_email(email)
        latest = self.customer_repository.latest_unused_password_reset_code(email)
        if _is_within_resend_cooldown(latest):
            return

        if self._reset_codes_in_window(email) >= MAX_RESENDS:
            return
        if customer is None:
            return

        self._create_password_reset_code(customer)
        self.db.commit()

    def _reset_codes_in_window(self, email: str) -> int:
        return self.customer_repository.count_password_reset_codes_since(
            email=email,
            since=utcnow() - timedelta(minutes=RESEND_WINDOW_MINUTES),
        )

    def verify_reset_code(self, payload: VerifyResetCodeRequest) -> VerifyResetCodeResponse:
        code_row = self.customer_repository.latest_unused_password_reset_code(normalize_email(payload.email))
        if not code_row:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código inválido ou expirado")
        if code_row.attempts_count >= MAX_CODE_ATTEMPTS:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Muitas tentativas")
        if code_row.expires_at < utcnow():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código expirado")

        if not verify_verification_code(payload.code, code_row.code_hash):
            code_row.attempts_count += 1
            self.db.add(code_row)
            self.db.commit()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Código inválido")

        reset_token = generate_reset_token()
        code_row.reset_token_hash = hash_reset_token(reset_token)
        code_row.reset_token_expires_at = utcnow() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_MINUTES)
        self.db.add(code_row)
        self.db.commit()
        return VerifyResetCodeResponse(reset_token=reset_token)

    def reset_password(self, payload: ResetPasswordRequest) -> MessageResponse:
        # O TOKEN primeiro, e a senha depois. A ordem inversa fazia quem
        # chegava com um token vencido e uma confirmacao errada ouvir so
        # "confirmacao de senha nao confere": a pessoa arrumava a senha,
        # tentava de novo e SO ENTAO descobria que o link tinha expirado.
        #
        # De quebra, o bcrypt de `_hash_new_password` (~0,3s de proposito) so
        # roda depois de o token valer. Antes, qualquer requisicao com um
        # token inventado ja pagava esse custo no servidor.
        code_row = self.customer_repository.get_password_reset_by_token_hash(hash_reset_token(payload.reset_token))
        if not self._valid_reset_token(code_row, payload.reset_token):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token inválido ou expirado")

        customer = self.customer_repository.get_by_id(code_row.customer_id)
        if not customer:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token inválido ou expirado")

        if payload.new_password != payload.confirm_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Confirmação de senha não confere")
        password_hash = _hash_new_password(payload.new_password)

        customer.password_hash = password_hash
        # Derruba as sessoes antigas: quem esta com um token emitido antes
        # daqui perde o acesso. Em "esqueci minha senha" isso e o esperado —
        # a troca costuma ser justamente reacao a suspeita de invasao.
        customer.password_changed_at = utcnow()
        code_row.used_at = utcnow()
        self.customer_repository.invalidate_unused_password_reset_codes(customer.id)
        self.db.add(customer)
        self.db.add(code_row)
        self.db.commit()
        return MessageResponse(message="Senha alterada com sucesso.")

    def get_customer_from_token(self, token: str) -> Customer | None:
        payload = decode_signed_token(token, "customer_access")
        if payload.get("type") != "customer":
            return None
        try:
            customer = self.customer_repository.get_by_id(uuid.UUID(payload["sub"]))
        except (ValueError, KeyError):
            return None
        if customer is None:
            return None
        if _token_was_issued_before_password_change(payload, customer):
            return None
        return customer

    def get_customer_from_token_or_error(self, token: str) -> Customer:
        try:
            customer = self.get_customer_from_token(token)
        except TokenExpiredError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expirado") from exc
        except TokenInvalidError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido") from exc
        if not customer:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
        if not customer.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa")
        return customer

    def _registration_conflicts(self, email: str, phone: str) -> list[str]:
        """TODOS os campos ja cadastrados, e nao so o primeiro.

        Antes esta funcao devolvia o primeiro conflito e parava. Quem colidia
        no e-mail E no telefone — o caso mais comum de todos, porque e a
        pessoa que ja tem conta e esqueceu — corrigia o e-mail, tentava de
        novo, e so entao descobria o telefone. Duas viagens para descobrir
        dois problemas que o servidor ja sabia na primeira.

        A lista mantem a ordem dos campos, entao a mensagem sai estavel.
        """
        conflitos = []
        if self.customer_repository.get_by_email(email):
            conflitos.append("Email")
        if self.customer_repository.get_by_phone(phone):
            conflitos.append("Telefone")
        return conflitos

    def _create_email_verification_code(self, customer: Customer, resend_count: int = 0) -> None:
        code = generate_6_digit_code()
        self.customer_repository.create_email_code(
            customer_id=customer.id,
            email=customer.email,
            code_hash=hash_verification_code(code),
            expires_at=utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
            attempts_count=0,
            resend_count=resend_count,
        )
        self.email_service.send_email_verification_code(customer.email, code)

    def _create_password_reset_code(self, customer: Customer) -> None:
        code = generate_6_digit_code()
        self.customer_repository.create_password_reset_code(
            customer_id=customer.id,
            email=customer.email,
            code_hash=hash_verification_code(code),
            expires_at=utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
            attempts_count=0,
            resend_count=0,
        )
        self.email_service.send_password_reset_code(customer.email, code)

    @staticmethod
    def _valid_reset_token(code_row: PasswordResetCode | None, reset_token: str) -> bool:
        if not code_row or code_row.used_at or not code_row.reset_token_hash or not code_row.reset_token_expires_at:
            return False
        if code_row.reset_token_expires_at < utcnow():
            return False
        return verify_reset_token(reset_token, code_row.reset_token_hash)
