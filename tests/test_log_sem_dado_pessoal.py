"""Nenhum log pode levar dado pessoal do cliente.

Guarda de regressao para o achado A11 da auditoria de 12/08/2026: o log de
estimativa de entrega gravava `latitude` e `longitude` do endereco do cliente
em `logger.info`, sempre ligado. Coordenada e mais precisa que rua e numero —
quem tivesse o log do container reconstruia a casa de quem pediu entrega.

O teste le a arvore sintatica de `src/` e reprova qualquer argumento de
`logger.*` que saia de um objeto de cliente ou endereco. E deliberadamente
estreito: proibe pela ORIGEM do dado, nao pela palavra. `branch.latitude` e a
coordenada da LOJA, que ja esta publica na vitrine, e continua permitida —
proibir por nome faria o teste reprovar o log util junto com o perigoso.
"""

import ast
import pathlib
import unittest


RAIZ = pathlib.Path(__file__).resolve().parent.parent / "src"

# Objetos que carregam dado do cliente. Ler qualquer atributo deles dentro de
# uma chamada de log e o que o teste reprova.
ORIGENS_PESSOAIS = {
    "address",
    "customer",
    "current_customer",
    "payer",
    "cliente",
}

# Atributos aceitos mesmo vindo de origem pessoal: descrevem a AREA, que e o
# recorte de que a regra de entrega precisa, e nao identificam a pessoa.
ATRIBUTOS_LIBERADOS = {"neighborhood", "city", "state", "id"}

METODOS_DE_LOG = {"info", "warning", "error", "exception", "debug"}


def _chamadas_de_log(arvore: ast.AST):
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call):
            continue
        funcao = no.func
        if not isinstance(funcao, ast.Attribute) or funcao.attr not in METODOS_DE_LOG:
            continue
        if isinstance(funcao.value, ast.Name) and funcao.value.id == "logger":
            yield no


def _origem_e_atributo(no: ast.AST) -> tuple[str, str] | None:
    """`address.latitude` -> ("address", "latitude").

    Cobre tambem `getattr(address, "latitude", None)`, que e a forma usada no
    servico de entrega.
    """
    if isinstance(no, ast.Attribute) and isinstance(no.value, ast.Name):
        return no.value.id, no.attr
    if (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Name)
        and no.func.id == "getattr"
        and len(no.args) >= 2
        and isinstance(no.args[0], ast.Name)
        and isinstance(no.args[1], ast.Constant)
        and isinstance(no.args[1].value, str)
    ):
        return no.args[0].id, no.args[1].value
    return None


class LogSemDadoPessoalTests(unittest.TestCase):
    def test_nenhum_log_le_atributo_de_cliente_ou_endereco(self):
        infracoes = []

        for caminho in sorted(RAIZ.rglob("*.py")):
            arvore = ast.parse(caminho.read_text(encoding="utf-8"))
            for chamada in _chamadas_de_log(arvore):
                # args[0] e a mensagem com os %s; o dado vem dos seguintes.
                for argumento in chamada.args[1:]:
                    for no in ast.walk(argumento):
                        par = _origem_e_atributo(no)
                        if par is None:
                            continue
                        origem, atributo = par
                        if origem not in ORIGENS_PESSOAIS:
                            continue
                        if atributo in ATRIBUTOS_LIBERADOS:
                            continue
                        infracoes.append(
                            f"{caminho.relative_to(RAIZ.parent)}:{chamada.lineno} "
                            f"-> {origem}.{atributo}"
                        )

        self.assertEqual(
            infracoes,
            [],
            "Log com dado pessoal do cliente:\n  " + "\n  ".join(infracoes),
        )

    def test_o_teste_pegaria_o_defeito_original(self):
        """Sem isto, o teste acima passa por nao encontrar nada e ninguem nota."""
        codigo = (
            "logger.info('addr lat=%s', getattr(address, 'latitude', None))\n"
        )
        arvore = ast.parse(codigo)
        encontrados = [
            _origem_e_atributo(no)
            for chamada in _chamadas_de_log(arvore)
            for argumento in chamada.args[1:]
            for no in ast.walk(argumento)
            if _origem_e_atributo(no) is not None
        ]
        self.assertIn(("address", "latitude"), encontrados)


if __name__ == "__main__":
    unittest.main()
