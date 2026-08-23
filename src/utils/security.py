import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from passlib.context import CryptContext

from src.core.config import settings


_PASSWORD_ITERATIONS = 390_000
_BCRYPT_MAX_PASSWORD_BYTES = 72
_PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")


class TokenExpiredError(Exception):
    pass


class TokenInvalidError(Exception):
    pass


class AuthSecretMissingError(RuntimeError):
    """O segredo de assinatura nao esta configurado neste servidor.

    NAO e subclasse de `TokenInvalidError`, e essa e a correcao inteira.

    Antes as duas coisas eram o mesmo erro: segredo ausente na configuracao e
    token falsificado por um atacante levantavam `TokenInvalidError`. Como
    todo mundo que pega essa excecao responde 401, um servidor que subisse
    com `CUSTOMER_AUTH_SECRET` vazia recusava TODO cliente com "token
    invalido" — e a mensagem apontava para o cliente, que nao tinha nada de
    errado.

    O sintoma e indistinguivel de um ataque, entao ninguem vai olhar a
    configuracao: e o defeito mais caro de diagnosticar de toda esta lista.
    Separada, ela sobe como 500, que e o que ela e — falha do servidor, nao
    do requisitante.
    """


class PasswordTooLongError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > _BCRYPT_MAX_PASSWORD_BYTES:
        raise PasswordTooLongError("Password is too long")
    return _PASSWORD_CONTEXT.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Confere a senha contra o hash gravado, no formato em que ele estiver.

    Sao DOIS formatos convivendo, e o antigo nao pode parar de funcionar:
    senha gravada por versao anterior da API e login de lojista, e quebrar
    aqui tranca gente fora da propria loja.

    O prefixo `$2` e o do bcrypt (`$2a$`, `$2b$`, `$2y$`); o que nao comeca
    assim e tratado como o formato antigo.
    """
    if not password_hash:
        return False
    if password_hash.startswith("$2"):
        return _verify_bcrypt(password, password_hash)
    return _verify_legacy_pbkdf2(password, password_hash)


def _verify_bcrypt(password: str, password_hash: str) -> bool:
    try:
        return _PASSWORD_CONTEXT.verify(password, password_hash)
    except (ValueError, TypeError):
        # Hash corrompido no banco vira "senha errada" na tela de login, e nao
        # um 500 que derruba a rota de login inteira.
        return False


def _verify_legacy_pbkdf2(password: str, password_hash: str) -> bool:
    """O formato que a versao anterior da API gravava.

    `pbkdf2_sha256$<iteracoes>$<salt>$<digest>`, com salt e digest em base64
    url-safe SEM padding.

    O numero de iteracoes sai do PROPRIO hash, e nao da constante do modulo:
    e o que permite subir `_PASSWORD_ITERATIONS` sem invalidar o que ja esta
    gravado.
    """
    try:
        algorithm, iterations, salt, expected = password_hash.split("$", 3)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False

    return hmac.compare_digest(_b64encode(digest), expected)


def generate_6_digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_verification_code(code: str) -> str:
    return _hmac_hex(code, settings.EMAIL_CODE_SECRET)


def verify_verification_code(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_verification_code(code), code_hash)


def generate_reset_token() -> str:
    return secrets.token_urlsafe(32)


# Alfabeto da senha temporaria de lojista. Sem `O`/`0`, `I`/`l`/`1` e sem
# minuscula que se confunda com maiuscula na fonte do WhatsApp: esta senha e
# DITADA por telefone e copiada a mao no balcao, e um caractere ambiguo vira
# "nao consigo entrar" que ninguem consegue depurar a distancia.
_TEMPORARY_PASSWORD_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 20 caracteres sobre 31 simbolos: ~99 bits. O minimo do lojista e 12
# (`MIN_ADMIN_PASSWORD_LENGTH`), e o excedente e de graca — ninguem memoriza
# esta senha, ela e usada uma vez e trocada.
_TEMPORARY_PASSWORD_LENGTH = 20


def generate_temporary_password() -> str:
    """Senha de primeiro acesso, gerada pelo servidor.

    Gerada aqui e nao escolhida por quem cadastra, e a diferenca e concreta: o
    dono digitando a senha da equipe produz `mesa123`, e passa a CONHECER a
    senha de outra pessoa — com isso o `admin:{email}` do historico de pedidos
    deixa de identificar quem de fato agiu.

    Ela existe em claro uma vez so, na resposta do POST. Nao ha rota que a
    devolva de novo.
    """
    return "".join(
        secrets.choice(_TEMPORARY_PASSWORD_ALPHABET)
        for _ in range(_TEMPORARY_PASSWORD_LENGTH)
    )


def generate_tracking_token() -> str:
    """Segredo de acompanhamento do pedido.

    `secrets` e nao `random`: e um valor que autoriza leitura de dados
    pessoais, entao precisa ser imprevisivel de verdade. 32 bytes viram 43
    caracteres URL-safe — cabe em link de WhatsApp e nao da para adivinhar.
    """
    return secrets.token_urlsafe(32)


def hash_tracking_token(token: str) -> str:
    """Forma em que o token de acompanhamento e GRAVADO.

    sha-256 sem chave, ao contrario do codigo de verificacao e do token de
    reset, que sao HMAC. A diferenca nao e descuido: chave compra
    resistencia a forca bruta sobre a ENTRADA, e a entrada aqui e
    `secrets.token_urlsafe(32)` — 256 bits, sem dicionario e sem rainbow
    table possiveis. O codigo de verificacao tem seis digitos, um milhao de
    possibilidades, e por isso precisa da chave.

    O que se ganha em troca: nao existe uma variavel de ambiente cuja perda
    apague, de uma vez, o acesso de todo cliente ao proprio pedido — e o
    backfill da revisao 0016 nao precisou de segredo dentro da migracao.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_tracking_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_tracking_token(token), token_hash)


def hash_reset_token(token: str) -> str:
    return _hmac_hex(token, settings.PASSWORD_RESET_SECRET)


def verify_reset_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_reset_token(token), token_hash)


def create_signed_token(
    subject: str,
    purpose: str,
    expires_delta: timedelta,
    extra: dict[str, Any] | None = None,
    secret: str | None = None,
) -> str:
    now = utcnow()
    payload: dict[str, Any] = {
        "sub": subject,
        "purpose": purpose,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra:
        payload.update(extra)

    return jwt.encode(payload, secret or _customer_auth_secret(), algorithm="HS256")


def decode_signed_token(token: str, purpose: str, secret: str | None = None) -> dict[str, Any]:
    # O `try` cobre so a decodificacao. Envolvendo tambem a conferencia de
    # `purpose`, qualquer ValueError/TypeError vindo dela seria convertido em
    # "token invalido" — e um erro nosso sairia como culpa do cliente.
    try:
        payload = jwt.decode(token, secret or _customer_auth_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError from exc
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise TokenInvalidError from exc

    # `purpose` separa usos que compartilham o MESMO segredo — o ticket de
    # stream de 30s e o token de lojista de 12h sao os dois assinados com
    # ADMIN_AUTH_SECRET, e um nao pode valer pelo outro. A separacao entre
    # cliente e lojista quem faz e o segredo (ver `admin_auth_secret`).
    if payload.get("purpose") != purpose:
        raise TokenInvalidError
    return payload


def _customer_auth_secret() -> str:
    secret = settings.CUSTOMER_AUTH_SECRET or settings.CUSTOMER_JWT_SECRET
    if not secret:
        raise AuthSecretMissingError(
            "CUSTOMER_AUTH_SECRET nao esta configurada. Nenhum cliente "
            "consegue autenticar ate isso ser corrigido no servidor."
        )
    return secret


def admin_auth_secret() -> str:
    """Segredo dos tokens de lojista.

    Sem fallback para o segredo de cliente, de proposito: sao dois publicos
    diferentes e uma chave compartilhada faz o comprometimento de um alcancar
    o outro. `ADMIN_AUTH_SECRET` e obrigatoria na configuracao, entao aqui
    nao ha caminho de ausencia a tratar.
    """
    return settings.ADMIN_AUTH_SECRET


def token_was_issued_before_password_change(
    payload: dict,
    password_changed_at: datetime | None,
) -> bool:
    """Revogacao de JWT na troca de senha, para cliente e para lojista.

    Nao ha lista de tokens revogados nem refresh token: comparamos o `iat` do
    token com o instante da ultima troca de senha. Trocou a senha, todo token
    emitido antes daquele momento morre — inclusive o do ladrao, que era o
    ponto.

    Mora aqui, e nao em cada service, porque a regra e a mesma nos dois
    publicos e duas copias seriam duas chances de divergir. O que muda entre
    eles e so de qual coluna sai o `password_changed_at`.

    Sem `password_changed_at` nada e revogado — e o comportamento de quem
    nunca trocou a senha depois de a coluna existir.

    O `iat` do JWT tem resolucao de segundos. Um token emitido no MESMO
    segundo da troca e tratado como anterior e cai: errar para o lado de
    derrubar uma sessao legitima custa um login novo; o outro lado deixa a
    conta invadida aberta.
    """
    if password_changed_at is None:
        return False

    issued_at_epoch = payload.get("iat")
    if issued_at_epoch is None:
        # Token sem `iat` nao da para datar. Tratamos como antigo: quem emite
        # hoje sempre inclui o campo (create_signed_token, logo acima).
        return True

    issued_at = datetime.fromtimestamp(issued_at_epoch, timezone.utc)
    if password_changed_at.tzinfo is None:
        password_changed_at = password_changed_at.replace(tzinfo=timezone.utc)
    return issued_at < password_changed_at


def _hmac_hex(value: str, secret: str | None) -> str:
    key = (secret or settings.CUSTOMER_AUTH_SECRET).encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
