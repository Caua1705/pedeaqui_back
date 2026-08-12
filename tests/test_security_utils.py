"""Caracterizacao de `utils/security.py`.

TESTE DE CARACTERIZACAO: descreve o que o codigo faz HOJE, nao o que deveria
fazer. Comportamento esquisito fica registrado e verde, com comentario
apontando o problema.

O que traz este arquivo para o topo da fila: **senha gravada por versao antiga
da API precisa continuar validando.** `verify_password` tem um caminho
`pbkdf2_sha256` que nenhum teste exercitava — e ele e o login do lojista. Uma
refatoracao que o quebrasse tranca gente fora da propria loja, e o sintoma
("minha senha parou de funcionar") chega por telefone, nao por alerta.
"""

import base64
import hashlib
from datetime import timedelta

import pytest

from src.core.config import settings
from src.utils.security import (
    PasswordTooLongError,
    TokenExpiredError,
    TokenInvalidError,
    _b64decode,
    _b64encode,
    _customer_auth_secret,
    admin_auth_secret,
    create_signed_token,
    decode_signed_token,
    generate_6_digit_code,
    generate_numeric_code,
    generate_reset_token,
    generate_tracking_token,
    hash_code,
    hash_password,
    hash_reset_token,
    hash_verification_code,
    verify_code,
    verify_password,
    verify_reset_token,
    verify_verification_code,
)


SENHA = "senha-do-lojista"


def legacy_pbkdf2_hash(password: str, iterations: int = 390_000, salt: bytes = b"salt-de-teste-16") -> str:
    """Uma senha no formato que a versao ANTIGA da API gravava.

    O b64 e montado aqui a mao, e nao com o `_b64encode` do modulo, de
    proposito: assim o teste prende o FORMATO gravado no banco, e nao a
    implementacao atual concordando consigo mesma.
    """
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)

    def b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


# ---------------------------------------------------------------------------
# verify_password — o fallback que nao pode quebrar
# ---------------------------------------------------------------------------


class TestVerifyPasswordLegacyPbkdf2:
    def test_a_password_stored_by_the_old_api_still_validates(self):
        assert verify_password(SENHA, legacy_pbkdf2_hash(SENHA)) is True

    def test_the_wrong_password_against_a_legacy_hash_fails(self):
        assert verify_password("outra-senha", legacy_pbkdf2_hash(SENHA)) is False

    def test_the_iteration_count_comes_from_the_hash_not_from_the_constant(self):
        """O numero de iteracoes e lido do proprio hash gravado.

        E o que permite trocar `_PASSWORD_ITERATIONS` sem invalidar o que ja
        esta no banco: uma linha gravada com 100.000 continua conferindo com
        100.000, nao com o valor de hoje.
        """
        assert verify_password(SENHA, legacy_pbkdf2_hash(SENHA, iterations=100_000)) is True

    @pytest.mark.parametrize(
        "password_hash",
        [
            "sha1$390000$c2FsdA$abc",  # algoritmo que nao e pbkdf2_sha256
            "pbkdf2_sha256$nao-e-numero$c2FsdA$abc",
            "pbkdf2_sha256$390000",  # campos de menos
            "nao-e-hash-nenhum",
        ],
    )
    def test_a_malformed_legacy_hash_is_false_never_an_exception(self, password_hash):
        """O `except (ValueError, TypeError)` da funcao.

        Importa que seja `False` e nao excecao: um hash corrompido no banco
        vira "senha errada" na tela de login, e nao um 500 que derruba a rota
        de login inteira.
        """
        assert verify_password(SENHA, password_hash) is False


class TestVerifyPasswordBcrypt:
    def test_the_current_format_round_trips(self):
        password_hash = hash_password(SENHA)
        assert password_hash.startswith("$2")
        assert verify_password(SENHA, password_hash) is True
        assert verify_password("outra-senha", password_hash) is False

    def test_a_corrupted_bcrypt_hash_is_false_never_an_exception(self):
        assert verify_password(SENHA, "$2b$12$curto-demais") is False

    @pytest.mark.parametrize("password_hash", [None, ""])
    def test_no_hash_at_all_is_false(self, password_hash):
        """Conta sem senha gravada (cliente criado por login social, lojista
        ainda sem senha definida). Nao autentica, e nao estoura."""
        assert verify_password(SENHA, password_hash) is False


class TestHashPasswordLength:
    def test_seventy_two_bytes_is_accepted(self):
        assert hash_password("x" * 72)

    def test_seventy_three_bytes_is_refused(self):
        """O bcrypt TRUNCA silenciosamente em 72 bytes.

        Sem esta guarda, duas senhas diferentes que compartilhassem os
        primeiros 72 bytes autenticariam uma a outra — e ninguem veria erro.
        """
        with pytest.raises(PasswordTooLongError):
            hash_password("x" * 73)

    def test_the_limit_is_in_bytes_not_characters(self):
        """`é` ocupa 2 bytes em UTF-8, entao 36 acentos cabem e 37 nao.

        Uma senha de 37 caracteres parece curta para quem digita; e o byte que
        manda.
        """
        assert hash_password("é" * 36)
        with pytest.raises(PasswordTooLongError):
            hash_password("é" * 37)


# ---------------------------------------------------------------------------
# Os wrappers: dois nomes publicos para a mesma funcao
# ---------------------------------------------------------------------------


class TestCodeWrappers:
    def test_hash_code_is_hash_verification_code(self):
        assert hash_code("123456") == hash_verification_code("123456")

    def test_verify_code_is_verify_verification_code(self):
        code_hash = hash_verification_code("123456")
        assert verify_code("123456", code_hash) == verify_verification_code("123456", code_hash)

    def test_the_round_trip_works_through_either_name(self):
        assert verify_code("123456", hash_code("123456")) is True
        assert verify_code("654321", hash_code("123456")) is False

    def test_generate_numeric_code_is_generate_6_digit_code(self):
        """ESQUISITO, e registrado como esta.

        Sao TRES pares de nomes publicos para a mesma coisa neste modulo:
        `hash_code`/`hash_verification_code`, `verify_code`/
        `verify_verification_code` e `generate_numeric_code`/
        `generate_6_digit_code`. O segundo de cada par nao acrescenta nada — e
        uma chamada direta ao primeiro.

        Quem le uma chamada a `hash_code` nao tem como saber que ela e a mesma
        do `hash_verification_code` sem abrir o arquivo, e o autocomplete
        oferece as duas. Fica registrado sem correcao: apagar nome publico e
        decisao separada, e precisa varrer quem chama.
        """
        code = generate_numeric_code()
        assert len(code) == 6
        assert code.isdigit()

    def test_a_six_digit_code_keeps_the_leading_zeros(self):
        """`f"{n:06d}"`. Sem o zero a esquerda, o codigo 000042 chegaria ao
        cliente como "42" e nao casaria com o hash gravado."""
        codes = {generate_6_digit_code() for _ in range(200)}
        assert all(len(code) == 6 and code.isdigit() for code in codes)


# ---------------------------------------------------------------------------
# Tokens de reset e de acompanhamento
# ---------------------------------------------------------------------------


class TestResetToken:
    def test_the_round_trip_works(self):
        token = generate_reset_token()
        assert verify_reset_token(token, hash_reset_token(token)) is True

    def test_another_token_does_not_validate(self):
        assert verify_reset_token(generate_reset_token(), hash_reset_token(generate_reset_token())) is False

    def test_the_hash_is_stable_for_the_same_token(self):
        """HMAC, nao bcrypt: o mesmo token da sempre o mesmo hash, que e o que
        permite procurar a linha no banco pelo hash."""
        token = generate_reset_token()
        assert hash_reset_token(token) == hash_reset_token(token)


class TestTrackingToken:
    def test_it_is_url_safe_and_long(self):
        token = generate_tracking_token()
        assert len(token) == 43
        assert "/" not in token and "+" not in token and "=" not in token

    def test_two_tokens_are_never_the_same(self):
        assert len({generate_tracking_token() for _ in range(200)}) == 200


# ---------------------------------------------------------------------------
# JWT assinado
# ---------------------------------------------------------------------------


class TestSignedToken:
    def test_the_round_trip_carries_subject_and_purpose(self):
        token = create_signed_token("cliente-1", "password_reset", timedelta(minutes=5))
        payload = decode_signed_token(token, "password_reset")
        assert payload["sub"] == "cliente-1"
        assert payload["purpose"] == "password_reset"

    def test_extra_claims_go_into_the_payload(self):
        token = create_signed_token("c", "p", timedelta(minutes=5), extra={"role": "owner"})
        assert decode_signed_token(token, "p")["role"] == "owner"

    def test_the_wrong_purpose_is_invalid_not_expired(self):
        """A conferencia de `purpose` e o que impede um token de cliente de
        virar token de admin quando os dois segredos coincidem (ver
        `admin_auth_secret`)."""
        token = create_signed_token("c", "password_reset", timedelta(minutes=5))
        with pytest.raises(TokenInvalidError):
            decode_signed_token(token, "email_verification")

    def test_an_expired_token_raises_expired_not_invalid(self):
        """Os dois erros sao separados porque a tela responde diferente:
        expirado pede um link novo, invalido nao."""
        token = create_signed_token("c", "p", timedelta(seconds=-10))
        with pytest.raises(TokenExpiredError):
            decode_signed_token(token, "p")

    @pytest.mark.parametrize("token", ["", "nao.e.jwt", "a.b.c"])
    def test_garbage_is_invalid(self, token):
        with pytest.raises(TokenInvalidError):
            decode_signed_token(token, "p")

    def test_a_token_signed_with_another_secret_is_invalid(self):
        # 32+ bytes nos dois: abaixo disso o PyJWT emite InsecureKeyLengthWarning
        # e o teste sujaria a saida da suite com um aviso que nao e sobre ele.
        segredo_a = "segredo-a-com-trinta-e-dois-bytes-ou-mais"
        segredo_b = "segredo-b-com-trinta-e-dois-bytes-ou-mais"

        token = create_signed_token("c", "p", timedelta(minutes=5), secret=segredo_a)
        assert decode_signed_token(token, "p", secret=segredo_a)["sub"] == "c"
        with pytest.raises(TokenInvalidError):
            decode_signed_token(token, "p", secret=segredo_b)


class TestAuthSecrets:
    def test_admin_never_falls_back_to_the_customer_secret(self, monkeypatch):
        """O fallback existia e foi removido: dois publicos com a mesma chave
        de assinatura fazem um token forjado de um lado valer do outro.
        ADMIN_AUTH_SECRET e obrigatoria na configuracao desde entao."""
        monkeypatch.setattr(settings, "ADMIN_AUTH_SECRET", "segredo-de-admin")
        monkeypatch.setattr(settings, "CUSTOMER_AUTH_SECRET", "segredo-de-cliente")
        assert admin_auth_secret() != "segredo-de-cliente"

    def test_admin_prefers_its_own_secret_when_set(self, monkeypatch):
        monkeypatch.setattr(settings, "ADMIN_AUTH_SECRET", "segredo-de-admin")
        monkeypatch.setattr(settings, "CUSTOMER_AUTH_SECRET", "segredo-de-cliente")
        assert admin_auth_secret() == "segredo-de-admin"

    def test_no_secret_configured_raises_token_invalid(self, monkeypatch):
        """ESQUISITO, e registrado como esta.

        Segredo AUSENTE na configuracao do servidor levanta
        `TokenInvalidError` — o mesmo erro de um token falsificado. Sao coisas
        de naturezas opostas: uma e o cliente mandando lixo (401 correto), a
        outra e a API subindo mal configurada (que deveria gritar no boot).

        Com os dois no mesmo erro, uma variavel de ambiente esquecida no deploy
        aparece como "todo mundo com token invalido" em vez de como falha de
        configuracao.
        """
        monkeypatch.setattr(settings, "ADMIN_AUTH_SECRET", None)
        monkeypatch.setattr(settings, "CUSTOMER_AUTH_SECRET", None)
        monkeypatch.setattr(settings, "CUSTOMER_JWT_SECRET", None)
        with pytest.raises(TokenInvalidError):
            _customer_auth_secret()


# ---------------------------------------------------------------------------
# base64 sem padding — o formato do hash legado depende disto
# ---------------------------------------------------------------------------


class TestBase64Helpers:
    @pytest.mark.parametrize("raw", [b"", b"a", b"ab", b"abc", b"salt-de-teste-16", bytes(range(256))])
    def test_the_round_trip_survives_the_missing_padding(self, raw):
        """`_b64encode` corta o `=` e `_b64decode` o repoe pela conta
        `-len(value) % 4`. Se essa conta mudar, toda senha pbkdf2 gravada no
        banco para de conferir de uma vez."""
        assert _b64decode(_b64encode(raw)) == raw

    def test_the_encoded_form_has_no_padding_and_is_url_safe(self):
        assert _b64encode(b"ab") == "YWI"
        assert "=" not in _b64encode(bytes(range(256)))
