"""Origens aceitas pelo CORS.

O padrao dos previews da Vercel e a unica origem que nao e uma string fixa,
e por isso e a unica que da para escrever errado sem ninguem perceber: um
padrao frouxo demais entrega a API a qualquer app hospedado na Vercel, e um
apertado demais quebra o preview do painel sem erro visivel no backend — o
navegador e que recusa, do lado de la.

A conferencia usa o proprio `CORSMiddleware` montado com os argumentos que
`main.py` passa, e nao um `re.fullmatch` reescrito aqui: assim o teste
tambem prova que o padrao esta LIGADO na aplicacao, nao so que ele existe.
"""

import unittest

from starlette.middleware.cors import CORSMiddleware

from main import app


def build_cors_middleware() -> CORSMiddleware:
    """CORS montado com o que a aplicacao realmente configurou.

    Se alguem tirar o `allow_origin_regex` do main.py, os testes de preview
    falham — que e o ponto.
    """
    for middleware in app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return CORSMiddleware(app=None, **middleware.kwargs)
    raise AssertionError("CORSMiddleware nao esta montado na aplicacao")


class CorsOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cors = build_cors_middleware()

    def test_admin_panel_domain_is_allowed(self):
        self.assertTrue(self.cors.is_allowed_origin("https://admin.pederapidex.com"))

    def test_commit_preview_is_allowed(self):
        # URL que a Vercel gera para cada deploy.
        self.assertTrue(
            self.cors.is_allowed_origin(
                "https://rapidex-admin-k2p9x7q1-cauas-projects-3c9f6aea.vercel.app"
            )
        )

    def test_branch_preview_is_allowed(self):
        # Alias por branch, com o "git-" no meio.
        self.assertTrue(
            self.cors.is_allowed_origin(
                "https://rapidex-admin-git-feat-fase-3-cauas-projects-3c9f6aea.vercel.app"
            )
        )

    def test_preview_from_another_vercel_scope_is_rejected(self):
        # O nome do projeto bate, o escopo nao. Sem o escopo no padrao,
        # qualquer pessoa criaria um projeto "rapidex-admin-*" na Vercel e
        # falaria com a API.
        self.assertFalse(
            self.cors.is_allowed_origin(
                "https://rapidex-admin-qualquercoisa-outro-scope.vercel.app"
            )
        )

    def test_domain_that_only_starts_with_the_pattern_is_rejected(self):
        # Sem a ancora no fim, este passaria: o dominio de verdade e
        # `atacante.com`.
        self.assertFalse(
            self.cors.is_allowed_origin(
                "https://rapidex-admin-k2p9x7q1-cauas-projects-3c9f6aea.vercel.app.atacante.com"
            )
        )

    def test_plain_http_preview_is_rejected(self):
        # O padrao exige https: o token do lojista viaja nessa conexao.
        self.assertFalse(
            self.cors.is_allowed_origin(
                "http://rapidex-admin-k2p9x7q1-cauas-projects-3c9f6aea.vercel.app"
            )
        )


if __name__ == "__main__":
    unittest.main()
