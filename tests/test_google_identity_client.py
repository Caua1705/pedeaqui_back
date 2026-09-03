"""O `id_token` do Google conferido de verdade — chave real, PyJWT real.

O que e dublado aqui e o **transporte** (o `httpx.Client` que busca o JWKS), e
so ele. A assinatura RSA e feita com uma chave gerada no teste, o JWKS e o
formato de verdade, e quem confere e o PyJWT do lock. E a armadilha 42: dublar
mais alto que o transporte testaria o dublê.

## O teste que importa e o do `aud`

`test_token_de_outro_aplicativo_e_recusado` e o unico deles que cobre uma
falha silenciosa e real: um `id_token` **legitimo**, assinado pelo Google de
verdade, com `email_verified` verdadeiro — e emitido para OUTRO aplicativo. O
Google emite milhoes deles por dia. Sem a conferencia de `aud`, quem operasse
qualquer um desses aplicativos logaria como qualquer cliente dele aqui dentro,
e nada na resposta pareceria errado.

Os outros dublês do arquivo (`iss`, chave errada, vencido) sao o PyJWT fazendo
o trabalho dele; este e o argumento que **nos** temos que passar.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from src.integrations.google_identity_client import (
    CHAVES_DO_GOOGLE,
    GoogleIdentityClient,
    GoogleIdentityInvalidTokenError,
    GoogleIdentityNotConfiguredError,
    GoogleIdentityUnavailableError,
    GoogleIdentityUnverifiedEmailError,
)


HTTPX_CLIENT_PATH = "src.integrations.google_identity_client.httpx.Client"

NOSSO_CLIENT_ID = "123456789-android.apps.googleusercontent.com"
JWKS_URL = "https://exemplo.invalid/certs"

_CHAVE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_CHAVE_ALHEIA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_KID = "chave-de-teste-1"


def _jwk(chave_privada, kid: str) -> dict:
    jwk = RSAAlgorithm.to_jwk(chave_privada.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


def _token(
    chave=_CHAVE,
    kid: str = _KID,
    audience: str = NOSSO_CLIENT_ID,
    issuer: str = "https://accounts.google.com",
    email_verified=True,
    expira_em_minutos: int = 30,
    **sobrescritas,
) -> str:
    agora = datetime.now(timezone.utc)
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": "104829173829173829173",
        "email": "pessoa@gmail.com",
        "email_verified": email_verified,
        "name": "Pessoa de Teste",
        "iat": agora,
        "exp": agora + timedelta(minutes=expira_em_minutos),
    }
    claims.update(sobrescritas)
    return jwt.encode(claims, chave, algorithm="RS256", headers={"kid": kid})


class _RespostaFalsa:
    def __init__(self, corpo: dict) -> None:
        self._corpo = corpo

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._corpo


class _ClienteFalso:
    """Substitui `httpx.Client` dentro do modulo. Conta as buscas.

    A contagem nao e enfeite: e ela que separa "o cache funcionou" de "o
    cliente foi buscar de novo", e e o unico jeito de provar a rebusca por
    `kid` desconhecido (a rotacao de chave do Google).
    """

    respostas: list[dict] = []
    buscas: int = 0
    erro: Exception | None = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_ClienteFalso":
        return self

    def __exit__(self, *args) -> None:
        return None

    def get(self, url: str) -> _RespostaFalsa:
        type(self).buscas += 1
        if type(self).erro is not None:
            raise type(self).erro
        indice = min(type(self).buscas - 1, len(type(self).respostas) - 1)
        return _RespostaFalsa(type(self).respostas[indice])


def _jwks(*jwks_individuais: dict) -> dict:
    return {"keys": list(jwks_individuais)}


class _ComJwksFalso(unittest.TestCase):
    """Base: cada teste comeca com o cache vazio e a contagem zerada.

    Sem o `esquecer()`, o cache de PROCESSO faria um teste herdar a chave que
    o vizinho baixou — e o de rotacao ficaria verde sem nunca ter buscado.
    """

    respostas: list[dict] = []

    def setUp(self) -> None:
        CHAVES_DO_GOOGLE.esquecer()
        _ClienteFalso.respostas = self.respostas or [_jwks(_jwk(_CHAVE, _KID))]
        _ClienteFalso.buscas = 0
        _ClienteFalso.erro = None
        self.patch = patch(HTTPX_CLIENT_PATH, _ClienteFalso)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(CHAVES_DO_GOOGLE.esquecer)

    def cliente(self, client_ids=(NOSSO_CLIENT_ID,)) -> GoogleIdentityClient:
        return GoogleIdentityClient(client_ids=client_ids, jwks_url=JWKS_URL)


class TestOTokenValido(_ComJwksFalso):
    def test_devolve_a_identidade(self) -> None:
        identidade = self.cliente().verify_id_token(_token())
        self.assertEqual(identidade.subject, "104829173829173829173")
        self.assertEqual(identidade.email, "pessoa@gmail.com")
        self.assertEqual(identidade.name, "Pessoa de Teste")

    def test_email_verified_como_a_string_true_tambem_vale(self) -> None:
        """O Google ja mandou `"true"` em vez de `true`. Recusar isso seria
        recusar token legitimo."""
        identidade = self.cliente().verify_id_token(_token(email_verified="true"))
        self.assertEqual(identidade.email, "pessoa@gmail.com")

    def test_sem_nome_o_email_serve_de_nome(self) -> None:
        """`customers.name` e NOT NULL, e quem nao preencheu o perfil do
        Google nao manda `name`. O e-mail e um nome ruim e melhor que vazio —
        e a tela de completar cadastro deixa trocar."""
        identidade = self.cliente().verify_id_token(_token(name=None))
        self.assertEqual(identidade.name, "pessoa@gmail.com")

    def test_o_issuer_sem_esquema_tambem_vale(self) -> None:
        identidade = self.cliente().verify_id_token(_token(issuer="accounts.google.com"))
        self.assertEqual(identidade.subject, "104829173829173829173")


class TestOTokenRecusado(_ComJwksFalso):
    def test_token_de_outro_aplicativo_e_recusado(self) -> None:
        """O furo classico deste fluxo, e o unico silencioso da lista.

        Token legitimo, assinado pelo Google, `email_verified` verdadeiro — e
        emitido para outro aplicativo. Sem a conferencia de `aud`, quem
        operasse esse aplicativo logaria como qualquer cliente dele aqui.
        """
        alheio = _token(audience="999-outro-app.apps.googleusercontent.com")
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(alheio)

    def test_o_mesmo_token_com_o_nosso_aud_passa(self) -> None:
        """O par do teste acima. Sem ele, `aud` sempre recusando ficaria
        verde e ninguem entraria com Google."""
        identidade = self.cliente().verify_id_token(_token())
        self.assertEqual(identidade.subject, "104829173829173829173")

    def test_issuer_estranho_e_recusado(self) -> None:
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(_token(issuer="https://accounts.evil.invalid"))

    def test_assinatura_de_outra_chave_e_recusada(self) -> None:
        """Mesmo `kid`, chave errada: e o token forjado por quem copiou o
        cabecalho de um legitimo."""
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(_token(chave=_CHAVE_ALHEIA))

    def test_token_vencido_e_recusado(self) -> None:
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(_token(expira_em_minutos=-1))

    def test_token_sem_kid_e_recusado(self) -> None:
        sem_kid = jwt.encode({"sub": "x"}, _CHAVE, algorithm="RS256")
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(sem_kid)

    def test_token_sem_sub_e_recusado(self) -> None:
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(_token(sub=None))


class TestOEmailNaoVerificado(_ComJwksFalso):
    """A recusa mora no cliente, e nao no service. Armadilha 46: regra que
    depende de alguem lembrar de aplicar e uma recomendacao."""

    def test_email_verified_falso_e_recusado(self) -> None:
        with self.assertRaises(GoogleIdentityUnverifiedEmailError):
            self.cliente().verify_id_token(_token(email_verified=False))

    def test_email_verified_ausente_e_recusado(self) -> None:
        """Ausente e falso, e nao "provavelmente verdadeiro"."""
        with self.assertRaises(GoogleIdentityUnverifiedEmailError):
            self.cliente().verify_id_token(_token(email_verified=None))


class TestSemConfiguracao(_ComJwksFalso):
    def test_sem_client_id_a_falha_e_de_configuracao(self) -> None:
        """Erro proprio, e nao "token invalido".

        Mesmo motivo do `AuthSecretMissingError`: um servidor sem client id
        recusando todo mundo com "token invalido" aponta para o app, que nao
        tem nada de errado, e o sintoma fica indistinguivel de um ataque.
        """
        with self.assertRaises(GoogleIdentityNotConfiguredError):
            self.cliente(client_ids=()).verify_id_token(_token())

    def test_nao_chega_a_buscar_o_jwks(self) -> None:
        with self.assertRaises(GoogleIdentityNotConfiguredError):
            self.cliente(client_ids=()).verify_id_token(_token())
        self.assertEqual(_ClienteFalso.buscas, 0)


class TestOGoogleForaDoAr(_ComJwksFalso):
    def test_falha_de_rede_vira_erro_proprio(self) -> None:
        """Separado de "token invalido": um e "tente de novo", o outro e
        "este token nao serve", e o app faz coisas diferentes."""
        import httpx

        _ClienteFalso.erro = httpx.ConnectError("sem rede")
        with self.assertRaises(GoogleIdentityUnavailableError):
            self.cliente().verify_id_token(_token())

    def test_jwks_sem_chave_nenhuma_vira_erro_proprio(self) -> None:
        _ClienteFalso.respostas = [{"keys": []}]
        with self.assertRaises(GoogleIdentityUnavailableError):
            self.cliente().verify_id_token(_token())


class TestOCacheDasChaves(_ComJwksFalso):
    def test_a_segunda_conferencia_nao_busca_de_novo(self) -> None:
        cliente = self.cliente()
        cliente.verify_id_token(_token())
        cliente.verify_id_token(_token())
        self.assertEqual(_ClienteFalso.buscas, 1)

    def test_kid_desconhecido_faz_buscar_de_novo(self) -> None:
        """A ROTACAO DE CHAVE, que o Google faz sozinho e sem aviso.

        Primeira busca: o JWKS antigo, sem a chave nova. Segunda: com ela.
        Sem a rebusca, TODO login falharia ate o TTL de uma hora vencer.
        """
        novo_kid = f"chave-nova-{uuid.uuid4()}"
        _ClienteFalso.respostas = [
            _jwks(_jwk(_CHAVE_ALHEIA, "chave-antiga")),
            _jwks(_jwk(_CHAVE_ALHEIA, "chave-antiga"), _jwk(_CHAVE, novo_kid)),
        ]

        identidade = self.cliente().verify_id_token(_token(kid=novo_kid))

        self.assertEqual(identidade.subject, "104829173829173829173")
        self.assertEqual(_ClienteFalso.buscas, 2)

    def test_kid_que_nao_existe_nem_depois_da_rebusca_e_recusado(self) -> None:
        _ClienteFalso.respostas = [_jwks(_jwk(_CHAVE_ALHEIA, "chave-antiga"))]
        with self.assertRaises(GoogleIdentityInvalidTokenError):
            self.cliente().verify_id_token(_token(kid="kid-que-nao-existe"))
        self.assertEqual(_ClienteFalso.buscas, 2)


if __name__ == "__main__":
    unittest.main()
