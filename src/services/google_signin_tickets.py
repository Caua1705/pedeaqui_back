"""Os dois tickets que atravessam as telas do "entrar com Google".

`POST /auth/google` tem tres desfechos, e so um deles e um JWT de sessao. Os
outros dois pedem uma segunda tela — completar o cadastro, ou confirmar a
ligacao por codigo — e precisam levar adiante o que o Google acabou de provar:
QUAL conta do Google e, e para qual e-mail.

## Por que um ticket assinado, e nao uma linha no banco

A alternativa obvia era uma tabela de "ligacoes pendentes". Ela custaria uma
migracao, um prazo de retencao (armadilha 38: o `sub` e o e-mail sao rastro de
pessoa) e um passo novo na exclusao de conta — tudo para guardar um estado que
vive quinze minutos e pertence a UMA pessoa, que esta com a tela aberta.

O ticket e o mesmo estado, assinado por nos e guardado por ela. Ele nao
autoriza nada sozinho: no cadastro, quem manda os campos obrigatorios e ela;
na ligacao, o que autoriza e o CODIGO que chegou na caixa de entrada. O ticket
so diz de que conta do Google se esta falando.

## `purpose` proprio para cada um, e o motivo e a armadilha 32

Os dois sao assinados com `CUSTOMER_AUTH_SECRET`, a mesma chave do token de
sessao do cliente. E exatamente o caso em que `purpose` serve: separar usos
que COMPARTILHAM a chave. Sem ele, um ticket de cadastro valeria como token de
acesso — `decode_signed_token` o recusa porque o `purpose` nao bate.

E os dois nao valem um pelo outro: um ticket de LIGACAO apresentado na rota de
cadastro criaria conta nova para quem ja tem uma, que e precisamente o caso
(b) sendo resolvido como o (c).
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException, status

from src.core.config import settings
from src.integrations.google_identity_client import GoogleIdentity
from src.utils.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_signed_token,
    decode_signed_token,
)


#: Cadastro novo: o Google provou quem e, faltam telefone e nascimento.
PURPOSE_SIGNUP = "google_signup"

#: Ligacao a uma conta que ja existe, esperando o codigo do e-mail.
PURPOSE_LINK = "google_link"


@dataclass(frozen=True)
class SignupTicket:
    subject: str
    email: str
    name: str


@dataclass(frozen=True)
class LinkTicket:
    subject: str
    email: str
    customer_id: UUID


def _expires_delta() -> timedelta:
    return timedelta(minutes=settings.GOOGLE_OAUTH_TICKET_MINUTES)


def create_signup_ticket(identity: GoogleIdentity) -> str:
    return create_signed_token(
        subject=identity.subject,
        purpose=PURPOSE_SIGNUP,
        expires_delta=_expires_delta(),
        extra={"email": identity.email, "name": identity.name},
    )


def create_link_ticket(identity: GoogleIdentity, customer_id: UUID) -> str:
    """O ticket carrega o CLIENTE, e nao so o e-mail.

    Ligar por "o dono do e-mail agora" abriria uma janela: entre a emissao e a
    volta do codigo, o e-mail pode ter mudado de dono (a conta anterior foi
    anonimizada e liberou o endereco, e outra pessoa se cadastrou com ele). O
    id fixa de quem era a conta quando o codigo saiu.
    """
    return create_signed_token(
        subject=identity.subject,
        purpose=PURPOSE_LINK,
        expires_delta=_expires_delta(),
        extra={"email": identity.email, "customer_id": str(customer_id)},
    )


#: A frase e a mesma para vencido e para invalido, e a UNICA acao possivel
#: esta dentro dela: recomecar. Distinguir os dois casos na tela nao mudaria
#: nada do que a pessoa faz, e diria a quem esta tentando forjar um ticket se
#: ele chegou a ser aceito.
TICKET_INVALIDO = "Esta confirmação não vale mais. Entre com Google novamente."


def read_signup_ticket(ticket: str) -> SignupTicket:
    payload = _ler(ticket, PURPOSE_SIGNUP)
    return SignupTicket(
        subject=_texto(payload, "sub"),
        email=_texto(payload, "email"),
        name=_texto(payload, "name"),
    )


def read_link_ticket(ticket: str) -> LinkTicket:
    payload = _ler(ticket, PURPOSE_LINK)
    return LinkTicket(
        subject=_texto(payload, "sub"),
        email=_texto(payload, "email"),
        customer_id=_uuid(payload, "customer_id"),
    )


def _ler(ticket: str, purpose: str) -> dict:
    """Traduz a falha do token para HTTP aqui, e nao em cada chamador.

    Sem isto, `TokenExpiredError` sobe ate o FastAPI e vira **500** — um
    ticket vencido, que e o caso mais comum de todos (a pessoa deixou a tela
    aberta), sairia como erro do servidor.
    """
    try:
        return decode_signed_token(ticket, purpose)
    except (TokenExpiredError, TokenInvalidError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=TICKET_INVALIDO
        ) from exc


def _texto(payload: dict, campo: str) -> str:
    """Campo obrigatorio do ticket. Ausente e ticket invalido, nao string vazia.

    Um ticket assinado por nos sempre tem os tres — mas quem le um JWT trata
    o conteudo como entrada, e cair com `TokenInvalidError` aqui e melhor que
    criar um cliente com nome vazio numa coluna NOT NULL.
    """
    valor = payload.get(campo)
    if not isinstance(valor, str) or not valor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=TICKET_INVALIDO
        )
    return valor


def _uuid(payload: dict, campo: str) -> UUID:
    try:
        return UUID(_texto(payload, campo))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=TICKET_INVALIDO
        ) from exc
