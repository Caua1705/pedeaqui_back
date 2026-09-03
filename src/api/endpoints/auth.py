from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.api.rate_limit import (
    FORGOT_PASSWORD_RATE_LIMIT,
    GOOGLE_COMPLETE_SIGNUP_RATE_LIMIT,
    GOOGLE_SIGN_IN_RATE_LIMIT,
    LOGIN_RATE_LIMIT,
    REGISTER_RATE_LIMIT,
    RESEND_EMAIL_CODE_RATE_LIMIT,
    RESET_PASSWORD_RATE_LIMIT,
    VERIFY_EMAIL_CODE_RATE_LIMIT,
    VERIFY_RESET_CODE_RATE_LIMIT,
    limiter,
)
from src.schemas.auth_schema import (
    ForgotPasswordRequest,
    GoogleCompleteSignupRequest,
    GoogleSignInRequest,
    GoogleSignInResponse,
    LoginRequest,
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
from src.services.auth_service import AuthService
from src.services.google_auth_service import GoogleAuthService


router = APIRouter(prefix="/auth", tags=["customer auth"])


@router.post("/register", response_model=RegisterCustomerResponse)
@limiter.limit(REGISTER_RATE_LIMIT)
def register_customer(
    request: Request,
    payload: RegisterCustomerRequest,
    db: Session = Depends(get_db),
) -> RegisterCustomerResponse:
    return AuthService(db).register(payload)


@router.post(
    "/verify-email-code",
    response_model=VerifyEmailCodeResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Codigo invalido ou expirado, ou `google_link_ticket` que nao vale mais"
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Conta inativa (so no caminho com `google_link_ticket`)"
        },
        status.HTTP_404_NOT_FOUND: {"description": "Cliente nao encontrado"},
        status.HTTP_409_CONFLICT: {
            "description": "A conta do Google do ticket ja esta ligada a outra conta"
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Muitas tentativas neste codigo"},
    },
)
@limiter.limit(VERIFY_EMAIL_CODE_RATE_LIMIT)
def verify_email_code(
    request: Request,
    payload: VerifyEmailCodeRequest,
    db: Session = Depends(get_db),
) -> VerifyEmailCodeResponse:
    """Confere o codigo de seis digitos. E a MESMA rota para dois usos.

    **Sem `google_link_ticket`** — o cadastro por e-mail, sem nenhuma mudanca:
    marca `email_verified_at`, responde `{verified, message}` e nao devolve
    token. O login continua sendo o passo seguinte.

    **Com `google_link_ticket`** — o caso (b) do "entrar com Google":
    `POST /auth/google` encontrou um `sub` novo cujo e-mail ja tem conta,
    mandou este codigo e devolveu o ticket. O codigo certo **liga a identidade
    ao cliente que ja existe** — nunca cria outro — e a resposta traz
    `access_token`, `token_type`, `customer` e `linked_provider`.

    O ticket sozinho nao liga nada: quem autoriza e o codigo, que so chega na
    caixa de entrada. E essa prova a mais que fecha o furo de juntar contas por
    e-mail — sem ela, quem tivesse se cadastrado antes com o endereco de outra
    pessoa receberia a conta dela pronta quando ela entrasse com o Google.

    **Nao ha rota de reenvio para este codigo.** `POST /auth/resend-email-code`
    nao serve: ele desiste em silencio quando o e-mail ja esta verificado, que
    e o caso da maioria das contas existentes. Para outro codigo, chame
    `POST /auth/google` de novo — o cooldown de 60 s e o teto de 3 na janela de
    15 min continuam valendo, e um ticket novo vem junto.
    """
    return AuthService(db).verify_email_code(payload)


@router.post(
    "/google",
    response_model=GoogleSignInResponse,
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "`id_token` invalido, ou e-mail nao verificado no Google"
        },
        status.HTTP_403_FORBIDDEN: {"description": "Conta inativa"},
        status.HTTP_502_BAD_GATEWAY: {"description": "Nao foi possivel falar com o Google"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "`GOOGLE_OAUTH_CLIENT_IDS` nao esta configurada neste servidor"
        },
    },
)
@limiter.limit(GOOGLE_SIGN_IN_RATE_LIMIT)
def sign_in_with_google(
    request: Request,
    payload: GoogleSignInRequest,
    db: Session = Depends(get_db),
) -> GoogleSignInResponse:
    """Entrar com Google. Mande o `id_token`; leia o `status` da resposta.

    O app faz o login com o Google no aparelho e manda aqui o `id_token` (o
    `idToken` do `GoogleSignIn`, nao o `accessToken`). O servidor confere a
    assinatura contra as chaves publicas do Google, o `aud` contra os nossos
    client ids e o `iss`; `email_verified` falso e recusado com 401.

    ## Os tres desfechos, pelo campo `status`

    **`authenticated`** — o `sub` ja e conhecido. Vem `access_token`,
    `token_type` e `customer`: e o MESMO token de `POST /auth/login`, e a
    sessao se usa igual.

    **`link_confirmation_required`** — o `sub` e novo e o `email` ja tem conta
    aqui. **Ninguem foi logado e nada foi ligado.** Um codigo de seis digitos
    saiu para o e-mail, e a resposta traz `link_ticket`: leve a pessoa para a
    tela de codigo e chame `POST /auth/verify-email-code` com
    `{email, code, google_link_ticket}`. E de la que sai a sessao.

    **`profile_required`** — o `sub` e novo e o e-mail nao tem conta. Vem
    `signup_ticket`, `email` e `name` (o nome do Google, que pode ser o proprio
    e-mail quando o perfil nao tem nome). Peca telefone e data de nascimento —
    os dois campos obrigatorios que o Google nao fornece — e chame
    `POST /auth/google/complete-signup`.

    ## O que esta rota NAO faz

    Nao junta contas por e-mail, em nenhuma hipotese. Nao devolve sessao para
    um e-mail que o Google nao confirmou. E nao guarda o `id_token`: ele e
    conferido e descartado.
    """
    return GoogleAuthService(db).sign_in(payload)


@router.post(
    "/google/complete-signup",
    response_model=LoginResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Ticket que nao vale mais, ou aceite de privacidade ausente"
        },
        status.HTTP_409_CONFLICT: {
            "description": "Telefone ja cadastrado, ou o e-mail/`sub` mudou de estado entre as duas telas"
        },
    },
)
@limiter.limit(GOOGLE_COMPLETE_SIGNUP_RATE_LIMIT)
def complete_google_signup(
    request: Request,
    payload: GoogleCompleteSignupRequest,
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Conclui o cadastro do caso (c) e devolve a sessao.

    Mande o `signup_ticket` que `POST /auth/google` deu, mais `phone`,
    `birth_date` e `privacy_accepted`. `marketing_opt_in` e `name` sao
    opcionais — sem `name`, fica o do Google.

    **Por que estes dois campos, e por que nao da para pular.** `phone` e
    `birth_date` sao `NOT NULL` em `customers` e o Google nao manda nenhum dos
    dois. O telefone em especial nao aceita valor de enfeite: para cliente
    logado, o pedido copia `customers.phone` no snapshot, e e esse numero que
    o entregador liga.

    A resposta e o mesmo `LoginResponse` do login por e-mail. A conta nasce com
    o e-mail ja verificado (o Google provou) e **sem senha utilizavel** — quem
    quiser uma senha usa `POST /auth/forgot-password`, que manda codigo para
    esse mesmo e-mail.

    **409 significa recomecar**, e nao "tente outros dados": entre as duas
    telas, ou o `sub` foi ligado em outra aba, ou alguem criou conta com esse
    e-mail. Chame `POST /auth/google` de novo — ele cai sozinho no caso certo.
    """
    return GoogleAuthService(db).complete_signup(payload)


@router.post("/resend-email-code", response_model=MessageResponse)
@limiter.limit(RESEND_EMAIL_CODE_RATE_LIMIT)
def resend_email_code(
    request: Request,
    payload: ResendEmailCodeRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    return AuthService(db).resend_email_code(payload)


@router.post("/login", response_model=LoginResponse)
@limiter.limit(LOGIN_RATE_LIMIT)
def login(
    request: Request,
    payload: LoginRequest,
    db: Session = Depends(get_db),
) -> LoginResponse:
    return AuthService(db).login(payload)


@router.post("/forgot-password", response_model=MessageResponse)
@limiter.limit(FORGOT_PASSWORD_RATE_LIMIT)
def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    return AuthService(db).forgot_password(payload)


@router.post("/verify-reset-code", response_model=VerifyResetCodeResponse)
@limiter.limit(VERIFY_RESET_CODE_RATE_LIMIT)
def verify_reset_code(
    request: Request,
    payload: VerifyResetCodeRequest,
    db: Session = Depends(get_db),
) -> VerifyResetCodeResponse:
    return AuthService(db).verify_reset_code(payload)


@router.post("/reset-password", response_model=MessageResponse)
@limiter.limit(RESET_PASSWORD_RATE_LIMIT)
def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    return AuthService(db).reset_password(payload)
