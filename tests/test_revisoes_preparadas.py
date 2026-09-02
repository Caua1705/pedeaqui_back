"""As revisoes de `alembic/preparadas/` estao escritas e NAO estao na cadeia.

Migracao escrita e nao aplicada e uma coisa util e perigosa. Util porque tira
a decisao do meio da madrugada: o roteiro fica revisado, e o dono so escolhe o
dia. Perigosa por dois motivos independentes, e este arquivo cobra os dois.

**Perigo 1: ela aplicar sozinha.** Basta um arquivo cair em
`alembic/versions/` — um `git mv` distraido, um merge — e o proximo
`alembic upgrade head` do container a executa em producao, sem ninguem ter
decidido nada. Os testes daqui provam que o Alembic nao conhece nenhuma das
duas.

**Perigo 2: ela envelhecer calada.** A lista de colunas foi escrita contra o
schema de hoje. Uma revisao futura que alinhe uma dessas colunas — ou que crie
uma divergencia nova — deixaria a preparada descrevendo um banco que nao existe
mais, e o defeito so apareceria na noite da aplicacao. O teste `db` compara a
lista com a divergencia REAL do schema montado e falha antes.
"""

import ast
import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from scripts.divergencias_orm_schema import comparar


RAIZ = Path(__file__).resolve().parent.parent
PREPARADAS = RAIZ / "alembic" / "preparadas"


def _valor_do_modulo(caminho: Path, nome: str):
    """Le uma constante do arquivo sem importa-lo.

    Importar traria `from alembic import op` junto, e o `op` de uma revisao so
    existe dentro de um contexto de migracao — importar uma revisao fora dele e
    justamente o tipo de coisa que este arquivo existe para nao fazer.
    `ast.literal_eval` le o valor escrito, que e o que interessa.
    """
    arvore = ast.parse(caminho.read_text(encoding="utf-8"), filename=str(caminho))
    for no in arvore.body:
        if isinstance(no, ast.Assign) and any(
            isinstance(alvo, ast.Name) and alvo.id == nome for alvo in no.targets
        ):
            return ast.literal_eval(no.value)
    raise AssertionError(f"{caminho.name} nao define {nome}")


def arquivos_preparados() -> list[Path]:
    return sorted(PREPARADAS.glob("*.py"))


def test_ha_pelo_menos_uma_revisao_preparada():
    """Se este teste falhar, os outros deste arquivo passariam por vacuidade.

    Uma suite verde que nao testa nada e pior que uma vermelha. Se as
    preparadas foram aplicadas e o diretorio esvaziou, apague este arquivo no
    mesmo commit em vez de deixa-lo verde sem assunto.
    """
    assert arquivos_preparados(), (
        f"{PREPARADAS} nao tem revisao nenhuma. Se as preparadas foram para "
        "`versions/`, apague tests/test_revisoes_preparadas.py junto."
    )


@pytest.mark.parametrize("arquivo", arquivos_preparados(), ids=lambda p: p.name)
def test_a_revisao_preparada_nao_esta_na_cadeia_do_alembic(arquivo):
    """O Alembic tem que NAO conhecer o `revision` declarado no arquivo.

    A pergunta e feita ao proprio `ScriptDirectory`, e nao ao caminho do
    arquivo: e ele que o `alembic upgrade` consulta, entao e a resposta dele
    que decide se a revisao roda.
    """
    identificador = _valor_do_modulo(arquivo, "revision")
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))

    conhecidas = {revisao.revision for revisao in script.walk_revisions()}

    assert identificador not in conhecidas, (
        f"{arquivo.name} declara revision={identificador!r} e o Alembic a "
        "conhece. Isso significa que ela ESTA na cadeia e o proximo "
        "`alembic upgrade head` a aplica — inclusive no container de producao, "
        "pelo entrypoint.\n\n"
        "Se a aplicacao foi decidida, o arquivo pertence a `alembic/versions/` "
        "e este teste sai junto. Se nao foi, tire-o de la."
    )


def test_as_duas_etapas_descrevem_as_mesmas_colunas():
    """A etapa 2 valida o que a etapa 1 criou. Listas diferentes = revisao quebrada.

    A duplicacao da lista e deliberada (o motivo esta no comentario da etapa 2:
    revisao e modulo carregado por caminho, e o import quebraria na hora do
    `git mv`). O preco da duplicacao e este teste.
    """
    etapa_1 = _valor_do_modulo(PREPARADAS / "alinhamento_orm_schema_etapa_1.py", "COLUNAS")
    etapa_2 = _valor_do_modulo(PREPARADAS / "alinhamento_orm_schema_etapa_2.py", "COLUNAS")

    assert etapa_1 == etapa_2, (
        "As duas etapas do alinhamento listam colunas diferentes.\n"
        "A etapa 2 roda `VALIDATE CONSTRAINT` sobre restricoes que a etapa 1 "
        "criou: coluna so na etapa 2 falha com 'constraint does not exist', e "
        "coluna so na etapa 1 fica com uma CHECK NOT VALID orfa para sempre."
    )


@pytest.mark.db
def test_a_lista_preparada_ainda_descreve_o_schema_de_hoje(engine_de_teste):
    """As 16 colunas do arquivo sao as 16 divergencias que o schema tem.

    Comparado contra o schema que o REPOSITORIO constroi (baseline + `upgrade
    head`), que e o de producao por construcao. Uma revisao nova que alinhe uma
    dessas colunas, ou que crie uma divergencia nova, derruba este teste — e o
    lugar de descobrir isso e aqui, nao na noite da aplicacao.
    """
    esperadas = [
        tuple(coluna)
        for coluna in _valor_do_modulo(
            PREPARADAS / "alinhamento_orm_schema_etapa_1.py", "COLUNAS"
        )
    ]

    reais = sorted(comparar(inspect(engine_de_teste)).orm_mais_estrito)

    assert sorted(esperadas) == reais, (
        "A revisao preparada nao descreve mais o schema.\n\n"
        f"  so na revisao: {sorted(set(esperadas) - set(reais))}\n"
        f"  so no schema : {sorted(set(reais) - set(esperadas))}\n\n"
        "Atualize `COLUNAS` nas DUAS etapas de `alembic/preparadas/`, ou "
        "apague as preparadas se o alinhamento deixou de fazer sentido. "
        "Ver docs/alinhamento-orm-schema.md."
    )


@pytest.mark.db
def test_as_duas_etapas_rodam_e_desfazem_no_postgres_de_verdade(engine_de_teste):
    """A prova de que "pronta" quer dizer pronta.

    Revisao escrita e nao aplicada costuma ser revisao NUNCA executada — e o
    primeiro lugar onde ela roda de verdade acaba sendo producao, de madrugada,
    com a API fora do ar. Aqui ela roda antes, contra o Postgres 17 do
    `docker-compose.test.yml`, no schema que o repositorio constroi.

    O que este teste cobra, em ordem:

    - a etapa 1 aceita as 16 `CHECK ... NOT VALID`;
    - a etapa 2 valida, aplica `SET NOT NULL` e derruba as restricoes — e
      **as 16 colunas ficam NOT NULL de verdade**, lidas do `inspect()` e nao
      do que a revisao acha que fez;
    - os dois `downgrade` devolvem as 16 a `nullable`, sem sobra.

    TUDO DENTRO DE UMA TRANSACAO QUE VOLTA. O Postgres tem DDL transacional, e
    e por isso que este teste pode existir sem estragar o schema de sessao que
    os outros 600 testes `db` compartilham. O `finally` nao e decoracao: sem
    ele, uma falha no meio deixaria o schema alterado e a suite inteira
    passaria a mentir a partir dali.

    `MigrationContext` + `Operations.context` e o que da um `op` de verdade ao
    modulo da revisao fora de um `alembic upgrade`. Sem isso, `op.execute` nao
    tem para onde mandar o DDL.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    etapa_1 = _carregar_revisao(PREPARADAS / "alinhamento_orm_schema_etapa_1.py")
    etapa_2 = _carregar_revisao(PREPARADAS / "alinhamento_orm_schema_etapa_2.py")

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            etapa_1.upgrade()
            etapa_2.upgrade()

            ainda_nulavel = [
                f"{tabela}.{coluna}"
                for tabela, coluna in etapa_1.COLUNAS
                if _aceita_nulo(conexao, tabela, coluna)
            ]
            assert not ainda_nulavel, (
                "Depois das duas etapas, estas colunas continuam aceitando "
                f"NULL: {ainda_nulavel}"
            )

            etapa_2.downgrade()
            etapa_1.downgrade()

            nao_voltou = [
                f"{tabela}.{coluna}"
                for tabela, coluna in etapa_1.COLUNAS
                if not _aceita_nulo(conexao, tabela, coluna)
            ]
            assert not nao_voltou, (
                f"O downgrade nao devolveu estas colunas a nullable: {nao_voltou}"
            )
    finally:
        transacao.rollback()
        conexao.close()


def _carregar_revisao(caminho: Path):
    """Importa o arquivo da revisao pelo caminho, sem ele ser modulo de pacote.

    E como o proprio Alembic carrega revisao, e e o que permite o arquivo viver
    em `alembic/preparadas/` sem `__init__.py` — e continuar carregando igual
    depois do `git mv` para `versions/`.
    """
    especificacao = importlib.util.spec_from_file_location(caminho.stem, caminho)
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


def _aceita_nulo(conexao, tabela: str, coluna: str) -> bool:
    return next(
        info["nullable"]
        for info in inspect(conexao).get_columns(tabela)
        if info["name"] == coluna
    )
