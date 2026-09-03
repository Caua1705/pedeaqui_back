"""Confere o `id_token` que o app do cliente traz do Google.

O app faz o login com o Google no aparelho e chega aqui com um `id_token` —
um JWT assinado pelo Google, em RS256, com as chaves publicas deles. Este
modulo faz UMA coisa: dizer se aquele token e autentico e de quem ele e.

## As tres conferencias, e nenhuma delas e opcional

1. **assinatura**, contra a chave publica do Google cujo `kid` esta no
   cabecalho do proprio token;
2. **`aud`**, que tem que ser um dos NOSSOS client ids. Sem isso, um
   `id_token` legitimo emitido para **outro aplicativo qualquer** — e o
   Google emite milhoes deles — entraria aqui: quem operasse esse outro
   aplicativo passaria a poder logar como qualquer cliente dele no nosso app.
   E o furo classico desse fluxo, e ele nao aparece em teste nenhum que use
   token nosso;
3. **`iss`**, `accounts.google.com` ou `https://accounts.google.com` (o
   Google emite as duas formas, e as duas sao validas).

`exp` e `iat` sao conferidos pelo PyJWT.

## `email_verified` falso e recusado AQUI, e nao no service

A regra e "se `email_verified` vier false, RECUSA — nao tenta ligar nada", e
ela mora no ponto mais baixo de proposito. Deixada no service, ela seria um
`if` que a proxima rota a usar este cliente pode esquecer — e a armadilha 46 e
sobre exatamente isso: regra que depende de alguem lembrar de aplica-la e uma
recomendacao. Aqui nao ha caminho que devolva uma identidade nao verificada.

O que `email_verified` significa: o Google diz que aquela conta controla
aquele endereco. Sem ele, o e-mail e um campo de texto que o dono da conta
escolheu — e ligar conta por um campo desses e entregar a conta da vitima a
quem digitou o e-mail dela.

## O cache das chaves

`_ChavesDoGoogle` guarda o JWKS por `JWKS_TTL_SECONDS`. E estado de PROCESSO,
declarado em `scripts/estado_entre_workers.py`: com N workers sao N copias das
mesmas chaves PUBLICAS, o que e correto e nao tem custo — nao ha nada a
compartilhar, e cada copia se renova sozinha.

Quando o `kid` do token nao esta no cache, ele e **descartado e o JWKS e
buscado de novo**, uma vez. E o que faz a rotacao de chave do Google (que
acontece sozinha, sem aviso) nao virar uma janela em que todo login falha ate
o TTL vencer.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKSet


logger = logging.getLogger("uvicorn.error")


#: Onde o Google publica as chaves publicas dos `id_token`.
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

#: As duas formas que o Google emite. As duas sao validas, e recusar a
#: primeira derrubaria uma fatia dos tokens sem nenhum ganho.
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

#: Por quanto tempo o JWKS e reaproveitado. O Google publica `Cache-Control`
#: de horas; uma hora e conservador e nao e o que protege da rotacao — quem
#: protege e a rebusca por `kid` desconhecido, logo abaixo.
JWKS_TTL_SECONDS = 3600


class GoogleIdentityError(Exception):
    pass


class GoogleIdentityNotConfiguredError(GoogleIdentityError):
    """Nao ha client id neste servidor. E falha de CONFIGURACAO, nao do cliente.

    Separada pelo motivo do `AuthSecretMissingError`: sem a separacao, um
    servidor sem `GOOGLE_OAUTH_CLIENT_IDS` recusaria todo mundo com "token
    invalido" — mensagem que aponta para o app, que nao tem nada de errado, e
    que e indistinguivel de um ataque.
    """


class GoogleIdentityUnavailableError(GoogleIdentityError):
    """Nao deu para falar com o Google. Tentar de novo pode funcionar."""


class GoogleIdentityInvalidTokenError(GoogleIdentityError):
    """O token nao e autentico, venceu, ou nao foi emitido para nos."""


class GoogleIdentityUnverifiedEmailError(GoogleIdentityError):
    """O Google nao confirma que esta conta controla este e-mail."""


@dataclass(frozen=True)
class GoogleIdentity:
    """Quem o Google diz que e. `subject` e o que liga — nunca o `email`."""

    subject: str
    email: str
    name: str


class _ChavesDoGoogle:
    """O JWKS, guardado por um tempo. Estado de processo, e declarado."""

    def __init__(self) -> None:
        self._jwks: PyJWKSet | None = None
        self._expira_em: float = 0.0

    def chave(self, kid: str, url: str, timeout_seconds: float):
        """A chave publica daquele `kid`, buscando de novo se ela faltar."""
        jwks = self._jwks_em_cache(url, timeout_seconds)
        try:
            return jwks[kid]
        except KeyError:
            pass

        # `kid` desconhecido e o sintoma da rotacao de chave do Google, que
        # acontece sozinha e sem aviso. Sem esta segunda busca, todo login
        # falharia ate o TTL vencer.
        jwks = self._buscar(url, timeout_seconds)
        try:
            return jwks[kid]
        except KeyError as exc:
            raise GoogleIdentityInvalidTokenError(
                "Token assinado por uma chave que o Google nao publica"
            ) from exc

    def _jwks_em_cache(self, url: str, timeout_seconds: float) -> PyJWKSet:
        if self._jwks is not None and time.monotonic() < self._expira_em:
            return self._jwks
        return self._buscar(url, timeout_seconds)

    def _buscar(self, url: str, timeout_seconds: float) -> PyJWKSet:
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                resposta = client.get(url)
                resposta.raise_for_status()
                jwks = PyJWKSet.from_dict(resposta.json())
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
            raise GoogleIdentityUnavailableError(
                "Nao foi possivel obter as chaves publicas do Google"
            ) from exc

        self._jwks = jwks
        self._expira_em = time.monotonic() + JWKS_TTL_SECONDS
        return jwks

    def esquecer(self) -> None:
        """Descarta o cache. Existe para o teste nao herdar chave do vizinho."""
        self._jwks = None
        self._expira_em = 0.0


#: Uma copia por processo. Ver o cabecalho e `scripts/estado_entre_workers.py`.
CHAVES_DO_GOOGLE = _ChavesDoGoogle()


class GoogleIdentityClient:
    def __init__(
        self,
        client_ids: tuple[str, ...],
        jwks_url: str = GOOGLE_JWKS_URL,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.client_ids = client_ids
        self.jwks_url = jwks_url
        self.timeout_seconds = timeout_seconds

    def verify_id_token(self, id_token: str) -> GoogleIdentity:
        """A identidade do dono do token, ou uma excecao. Nunca meio termo."""
        self._ensure_configured()
        claims = self._decode(id_token)
        return self._identity_from(claims)

    def _ensure_configured(self) -> None:
        if self.client_ids:
            return
        raise GoogleIdentityNotConfiguredError(
            "GOOGLE_OAUTH_CLIENT_IDS esta vazia. Entrar com Google nao "
            "funciona neste servidor ate isso ser corrigido."
        )

    def _decode(self, id_token: str) -> dict[str, Any]:
        chave = self._signing_key(id_token)
        try:
            return jwt.decode(
                id_token,
                chave,
                algorithms=["RS256"],
                # `audience` e `issuer` conferidos pelo PyJWT, e nao por um
                # `if` depois do decode: esquecer o `if` e silencioso, e
                # esquecer o argumento tambem — mas o argumento fica ao lado
                # da chave, que e onde quem le a chamada olha.
                audience=list(self.client_ids),
                issuer=list(GOOGLE_ISSUERS),
            )
        except jwt.PyJWTError as exc:
            # A mensagem do PyJWT ("Audience doesn't match", "Signature has
            # expired") NAO vai para a resposta: ela e util no log e, na tela,
            # so ensinaria a quem esta tentando o que ajustar.
            logger.info("[Auth Google] id_token recusado: %s", exc)
            raise GoogleIdentityInvalidTokenError("id_token invalido") from exc

    def _signing_key(self, id_token: str):
        try:
            cabecalho = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise GoogleIdentityInvalidTokenError("id_token malformado") from exc

        kid = cabecalho.get("kid")
        if not kid:
            raise GoogleIdentityInvalidTokenError("id_token sem `kid`")
        return CHAVES_DO_GOOGLE.chave(kid, self.jwks_url, self.timeout_seconds)

    @staticmethod
    def _identity_from(claims: dict[str, Any]) -> GoogleIdentity:
        subject = claims.get("sub")
        email = claims.get("email")
        if not subject or not email:
            raise GoogleIdentityInvalidTokenError("id_token sem `sub` ou `email`")

        # `email_verified` chega como booleano, e versoes antigas do Google
        # mandavam a string "true". Comparar contra os dois evita recusar
        # token legitimo; qualquer outra coisa (inclusive ausente) e falso.
        if claims.get("email_verified") not in (True, "true"):
            raise GoogleIdentityUnverifiedEmailError(
                "O Google nao confirma este e-mail"
            )

        # `name` pode faltar quando a pessoa nao preencheu o perfil. O e-mail
        # e um nome ruim e e melhor que vazio — a coluna e NOT NULL, e a tela
        # de completar cadastro deixa trocar.
        return GoogleIdentity(
            subject=str(subject),
            email=str(email),
            name=str(claims.get("name") or email),
        )
