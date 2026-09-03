"""Entrar com Google no app do cliente. Os tres casos, e o do meio e o que importa.

O app faz o login com o Google no aparelho e chega aqui com um `id_token`.
Depois de `GoogleIdentityClient.verify_id_token` — assinatura, `aud`, `iss` e
`email_verified`, todos conferidos la — sobram tres situacoes:

| | `sub` | e-mail | o que acontece |
|---|---|---|---|
| a | conhecido | — | **sessao**, direto. Nada muda |
| b | novo | JA TEM conta | codigo por e-mail; so liga quando ele voltar |
| c | novo | sem conta | cadastro, com os campos que o Google nao da |

## O CASO (b) NAO PODE LOGAR NEM JUNTAR SOZINHO

E a falha conhecida deste fluxo, e ela nao parece uma falha: um `sub` novo
cujo e-mail bate com uma conta existente parece "a mesma pessoa voltando".

O ataque e o contrario. Alguem se cadastra com o e-mail da VITIMA e nunca o
verifica — o cadastro por e-mail nao exige verificacao para existir, so para
logar. Meses depois a vitima entra com o Google. Com ligacao automatica por
e-mail, ela e jogada dentro da conta do atacante: os enderecos dela passam a
ser digitados ali, os pedidos dela aparecem ali, e o atacante — que SABE a
senha daquela conta — le tudo.

O que fecha isso e uma prova a mais, e ela nao pode vir do Google: tem que vir
da CAIXA DE ENTRADA. O codigo de seis digitos que o cadastro por e-mail ja
usa e exatamente essa prova. Por isso o caso (b) devolve
`link_confirmation_required` e um ticket, e quem LIGA e
`AuthService.verify_email_code` quando o codigo volta certo.

## O caso (c) nao devolve sessao na primeira ida, e o motivo e o telefone

`customers.phone` e `birth_date` sao `NOT NULL` e o Google nao manda nenhum
dos dois. Sentinela em `phone` nao e uma coluna com valor feio: para cliente
logado, `OrderService` grava `customer_phone_snapshot` a partir de
`customers.phone` — o valor falso vira **o numero que o entregador liga**. Por
isso o cliente so nasce quando os dois campos chegam, em
`complete_signup`.

`password_hash` e `NOT NULL` tambem, e esse resolve sozinho: hash de um
segredo sorteado e jogado fora, como `_anonymize_customer` ja faz. A conta
nasce sem senha utilizavel, e quem quiser uma passa por
`/auth/forgot-password` — que manda codigo para o e-mail que o Google
verificou.
"""

import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.constants import SOCIAL_PROVIDER_GOOGLE
from src.integrations.google_identity_client import (
    GoogleIdentity,
    GoogleIdentityClient,
    GoogleIdentityInvalidTokenError,
    GoogleIdentityNotConfiguredError,
    GoogleIdentityUnavailableError,
    GoogleIdentityUnverifiedEmailError,
)
from src.models.customer_model import Customer
from src.models.customer_social_identity_model import CustomerSocialIdentity
from src.repositories.customer_repository import CustomerRepository
from src.repositories.customer_social_identity_repository import (
    CustomerSocialIdentityRepository,
)
from src.schemas.auth_schema import (
    GOOGLE_AUTHENTICATED,
    GOOGLE_LINK_CONFIRMATION_REQUIRED,
    GOOGLE_PROFILE_REQUIRED,
    GoogleCompleteSignupRequest,
    GoogleSignInRequest,
    GoogleSignInResponse,
    LoginResponse,
)
from src.services import google_signin_tickets as tickets
from src.services.auth_service import (
    AuthService,
    customer_response,
    issue_customer_token,
    unusable_password_hash,
)
from src.utils.normalization import is_valid_email, normalize_email
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")


LINK_CONFIRMATION_MESSAGE = (
    "Este e-mail já tem uma conta. Enviamos um código para ele: confirme o "
    "código para ligar sua conta do Google."
)
PROFILE_REQUIRED_MESSAGE = (
    "Falta pouco: informe telefone e data de nascimento para concluir o cadastro."
)


class GoogleAuthService:
    def __init__(self, db: Session, identity_client: GoogleIdentityClient | None = None):
        self.db = db
        self.customer_repository = CustomerRepository(db)
        self.social_identity_repository = CustomerSocialIdentityRepository(db)
        # `AuthService` entra para o envio de codigo do caso (b) — cooldown,
        # teto por e-mail, HMAC e TTL ja moram la, e uma segunda copia disso
        # divergiria da primeira no dia em que o teto mudasse.
        self.auth_service = AuthService(db)
        self.identity_client = identity_client or GoogleIdentityClient(
            client_ids=settings.google_oauth_client_ids,
            jwks_url=settings.GOOGLE_OAUTH_JWKS_URL,
            timeout_seconds=settings.GOOGLE_OAUTH_TIMEOUT_SECONDS,
        )

    def sign_in(self, payload: GoogleSignInRequest) -> GoogleSignInResponse:
        """Os tres casos, na ordem em que se decidem. Cada um sai por um return."""
        identity = self._verified_identity(payload.id_token)

        vinculo = self.social_identity_repository.get_by_provider_user(
            SOCIAL_PROVIDER_GOOGLE, identity.subject
        )
        if vinculo is not None:
            return self._sign_in_known_subject(vinculo)

        email = normalize_email(identity.email)
        existente = self.customer_repository.get_by_email(email)
        if existente is not None:
            return self._ask_for_link_confirmation(existente, identity)

        return self._ask_for_profile(identity)

    # ---------------------------------------------------------------- (a)

    def _sign_in_known_subject(
        self, vinculo: CustomerSocialIdentity
    ) -> GoogleSignInResponse:
        customer = self.customer_repository.get_by_id(vinculo.customer_id)
        if customer is None:
            # A FK e `ON DELETE CASCADE`, entao isto nao acontece por caminho
            # nenhum do codigo. Se acontecer, e linha escrita por fora — e a
            # resposta certa e recusar, nao seguir sem cliente.
            logger.error(
                "[Auth Google] identidade orfa social_identity_id=%s", vinculo.id
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas"
            )
        self._ensure_active(customer)

        self.social_identity_repository.mark_login(vinculo, utcnow())
        self.db.commit()
        return self._authenticated(customer)

    # ---------------------------------------------------------------- (b)

    def _ask_for_link_confirmation(
        self, customer: Customer, identity: GoogleIdentity
    ) -> GoogleSignInResponse:
        """Pede o codigo do e-mail. NAO liga nada, e nao devolve sessao.

        A conta inativa recebe a mesma resposta das outras, e nao um 403: o
        que ela esta pedindo aqui e um codigo, e negar de forma distinguivel
        nesta rota diria a quem tem um Google com aquele e-mail qual e o
        estado da conta. Quem barra a conta inativa e o login, depois.
        """
        self.auth_service.send_link_confirmation_code(customer)
        return GoogleSignInResponse(
            status=GOOGLE_LINK_CONFIRMATION_REQUIRED,
            message=LINK_CONFIRMATION_MESSAGE,
            email=customer.email,
            link_ticket=tickets.create_link_ticket(identity, customer.id),
        )

    # ---------------------------------------------------------------- (c)

    @staticmethod
    def _ask_for_profile(identity: GoogleIdentity) -> GoogleSignInResponse:
        return GoogleSignInResponse(
            status=GOOGLE_PROFILE_REQUIRED,
            message=PROFILE_REQUIRED_MESSAGE,
            email=identity.email,
            name=identity.name,
            signup_ticket=tickets.create_signup_ticket(identity),
        )

    def complete_signup(self, payload: GoogleCompleteSignupRequest) -> LoginResponse:
        """Cria o cliente do caso (c) e devolve a sessao.

        O ticket diz de qual conta do Google se trata; os campos obrigatorios
        vem do corpo. As duas reconferencias do meio nao sao cerimonia: entre
        `POST /auth/google` e esta chamada passam minutos, e nesse intervalo o
        `sub` pode ter sido ligado noutra aba e o e-mail pode ter ganhado
        conta pelo cadastro comum.
        """
        ticket = tickets.read_signup_ticket(payload.signup_ticket)
        if not payload.privacy_accepted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Aceite de privacidade obrigatório",
            )

        email = normalize_email(ticket.email)
        if not is_valid_email(email):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Email inválido"
            )

        self._ensure_subject_is_free(ticket.subject)
        self._ensure_registration_is_possible(email=email, phone=payload.phone)

        customer = self._create_customer(ticket, payload, email)
        return LoginResponse(
            access_token=issue_customer_token(customer),
            token_type="bearer",
            customer=customer_response(customer),
        )

    def _create_customer(
        self,
        ticket: tickets.SignupTicket,
        payload: GoogleCompleteSignupRequest,
        email: str,
    ) -> Customer:
        agora = utcnow()
        try:
            customer = self.customer_repository.create(
                name=(payload.name or ticket.name).strip(),
                email=email,
                phone=payload.phone,
                # Sem senha utilizavel: ninguem conhece este valor, nem nos.
                # Quem quiser senha vai por `/auth/forgot-password`.
                password_hash=unusable_password_hash(),
                birth_date=payload.birth_date,
                # O Google ja provou o e-mail — `verify_id_token` recusa
                # `email_verified` falso. Pedir um codigo aqui seria pedir
                # de novo o que acabou de ser provado.
                email_verified_at=agora,
                phone_verified_at=None,
                marketing_opt_in=payload.marketing_opt_in,
                privacy_accepted_at=agora,
                is_active=True,
            )
            self.social_identity_repository.create(
                customer_id=customer.id,
                provider=SOCIAL_PROVIDER_GOOGLE,
                provider_user_id=ticket.subject,
                last_login_at=agora,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return customer

    # ------------------------------------------------------------ comuns

    def _verified_identity(self, id_token: str) -> GoogleIdentity:
        """Traduz as falhas do Google para HTTP. Cada uma diz outra coisa."""
        try:
            return self.identity_client.verify_id_token(id_token)
        except GoogleIdentityNotConfiguredError as exc:
            # Falha DESTE servidor, e nao do app: 503, como o upload sem
            # chave do Supabase. Um 401 aqui mandaria o cliente conferir uma
            # credencial que esta certa.
            logger.error("[Auth Google] %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Entrar com Google não está disponível neste servidor",
            ) from exc
        except GoogleIdentityUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Não foi possível falar com o Google. Tente novamente.",
            ) from exc
        except GoogleIdentityUnverifiedEmailError as exc:
            # Recusa e ponto: nao ha caminho de "confirme por codigo" aqui. O
            # e-mail nao verificado no Google e um campo que o dono da conta
            # digitou, e ligar conta por ele e o furo que o caso (b) fecha.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="O Google não confirmou este e-mail. Verifique-o na sua conta Google e tente de novo.",
            ) from exc
        except GoogleIdentityInvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenciais inválidas",
            ) from exc

    @staticmethod
    def _ensure_active(customer: Customer) -> None:
        if customer.is_active:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Conta inativa"
        )

    def _ensure_subject_is_free(self, subject: str) -> None:
        """A corrida das duas abas, e a resposta que ela merece.

        O UNIQUE do banco recusaria de qualquer jeito — mas como
        `IntegrityError`, que vira 500. Aqui vira 409 com uma frase que diz o
        que fazer: entrar com Google de novo, que agora cai no caso (a).
        """
        ja_ligado = self.social_identity_repository.get_by_provider_user(
            SOCIAL_PROVIDER_GOOGLE, subject
        )
        if ja_ligado is None:
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta conta do Google já está ligada. Entre com Google novamente.",
        )

    def _ensure_registration_is_possible(self, email: str, phone: str) -> None:
        """Os mesmos conflitos do cadastro por e-mail, com uma diferenca.

        O e-mail colidindo AQUI nao e "e-mail já cadastrado": e o caso (b)
        acontecendo entre as duas telas — alguem criou a conta com este
        endereco enquanto o ticket estava aberto. Ligar seria juntar duas
        contas sem prova de caixa de entrada, e criar seria impossivel
        (UNIQUE). A resposta e mandar recomecar, e o `POST /auth/google`
        seguinte cai no caso (b) sozinho.
        """
        if self.customer_repository.get_by_email(email) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Este e-mail passou a ter uma conta. "
                    "Entre com Google novamente para confirmar por código."
                ),
            )
        if self.customer_repository.get_by_phone(phone) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Telefone já cadastrado"
            )

    @staticmethod
    def _authenticated(customer: Customer) -> GoogleSignInResponse:
        return GoogleSignInResponse(
            status=GOOGLE_AUTHENTICATED,
            message="Login realizado com sucesso.",
            email=customer.email,
            access_token=issue_customer_token(customer),
            token_type="bearer",
            customer=customer_response(customer),
        )


def link_identity_after_code(
    repository: CustomerSocialIdentityRepository,
    customer: Customer,
    ticket: tickets.LinkTicket,
) -> CustomerSocialIdentity:
    """Liga a identidade ao cliente que JA EXISTE. Nunca cria cliente.

    Chamada por `AuthService.verify_email_code` quando o codigo volta certo e
    veio um `google_link_ticket`. Mora aqui, e nao la, porque e regra do
    "entrar com Google" — e mora numa funcao, e nao num metodo de
    `GoogleAuthService`, para o `AuthService` nao precisar importar o service
    inteiro (que importa o `AuthService` de volta).

    Recebe o REPOSITORIO e nao a sessao, e a diferenca so aparece no teste: um
    `CustomerSocialIdentityRepository(db)` construido aqui dentro seria o
    repositorio de VERDADE rodando contra o `FakeDb` da suite rapida — e o
    dublê ligado no `AuthService` ficaria sem efeito, o que e a armadilha 25.1
    (ligar o dublê numa camada so).

    **O que esta funcao NAO faz e o ponto dela: criar cliente.** O caso (b) e
    exatamente aquele em que a conta ja existe, e um `create` aqui produziria
    a segunda conta com o mesmo e-mail — impossivel pelo UNIQUE, entao 500 —
    ou, pior, num mundo sem UNIQUE, a conta duplicada que divide o historico
    da pessoa em duas.
    """
    if customer.id != ticket.customer_id:
        # O ticket foi emitido para OUTRA conta. Acontece quando o e-mail
        # mudou de dono entre a emissao e a volta do codigo (a conta anterior
        # foi anonimizada e liberou o endereco).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Esta confirmação não vale mais. Entre com Google novamente.",
        )

    existente = repository.get_by_provider_user(
        SOCIAL_PROVIDER_GOOGLE, ticket.subject
    )
    if existente is not None:
        return _existing_link(existente, customer)

    return repository.create(
        customer_id=customer.id,
        provider=SOCIAL_PROVIDER_GOOGLE,
        provider_user_id=ticket.subject,
        last_login_at=utcnow(),
    )


def _existing_link(
    existente: CustomerSocialIdentity, customer: Customer
) -> CustomerSocialIdentity:
    """O mesmo codigo conferido duas vezes, ou a ligacao feita noutra aba.

    Ligar de novo e a mesma coisa que ligar uma vez, entao o caminho e
    idempotente — mas SO para o mesmo cliente. Apontando para outro, e o
    `sub` de alguem tentando ser ligado a uma segunda conta, e ai recusar e a
    unica resposta certa.
    """
    if existente.customer_id == customer.id:
        return existente
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Esta conta do Google já está ligada a outra conta.",
    )
