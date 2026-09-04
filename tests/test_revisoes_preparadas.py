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
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from scripts.divergencias_orm_schema import comparar


RAIZ = Path(__file__).resolve().parent.parent
PREPARADAS = RAIZ / "alembic" / "preparadas"

# A ETAPA 1 SAIU DE `preparadas/` EM 05/09/2026 e esta na cadeia como
# `20260905_0055`. A etapa 2 continua preparada.
#
# Os caminhos ficam em constantes porque as duas etapas passaram a morar em
# diretorios diferentes, e e exatamente essa a situacao que este arquivo
# precisa cobrir: uma aplicada, a outra esperando — com as duas ainda obrigadas
# a descrever as MESMAS colunas.
ETAPA_1 = RAIZ / "alembic" / "versions" / "20260905_0055_alinhamento_orm_schema_etapa_1.py"
ETAPA_2 = PREPARADAS / "alinhamento_orm_schema_etapa_2.py"


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
    etapa_1 = _valor_do_modulo(ETAPA_1, "COLUNAS")
    etapa_2 = _valor_do_modulo(ETAPA_2, "COLUNAS")

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
        for coluna in _valor_do_modulo(ETAPA_1, "COLUNAS")
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

    - a etapa 1 aceita as 15 `CHECK ... NOT VALID`;
    - a etapa 2 valida, aplica `SET NOT NULL` e derruba as restricoes — e
      **as 15 colunas ficam NOT NULL de verdade**, lidas do `inspect()` e nao
      do que a revisao acha que fez;
    - os dois `downgrade` devolvem as 15 a `nullable`, sem sobra.

    O que ele NAO prova esta logo abaixo dele, e e o motivo do desenho em duas
    etapas: aqui a tabela esta VAZIA, e `VALIDATE` em tabela vazia passa sempre.

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

    etapa_1 = _carregar_revisao(ETAPA_1)
    etapa_2 = _carregar_revisao(ETAPA_2)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        # A etapa 1 NAO e aplicada aqui: ela esta na cadeia desde 05/09/2026, e
        # o schema de sessao ja veio com as 15 restricoes do `upgrade head`.
        # Conferir isso e metade do teste — se elas nao estivessem, a etapa 2
        # falharia com "constraint does not exist" e o motivo nao seria obvio.
        faltando = [
            f"{tabela}.{coluna}"
            for tabela, coluna in etapa_1.COLUNAS
            if not _restricao_existe(conexao, etapa_1.nome_da_restricao(tabela, coluna))
        ]
        assert not faltando, (
            "a etapa 1 esta na cadeia, mas estas restricoes nao existem no "
            f"schema: {faltando}"
        )

        with Operations.context(MigrationContext.configure(conexao)):
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

            # So a etapa 2 desfaz: o downgrade dela recria as `NOT VALID`, que
            # e exatamente o estado em que a etapa 1 deixa o banco.
            etapa_2.downgrade()

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


# ---------------------------------------------------------------------------
# O que o teste acima NAO prova, e e o motivo inteiro do desenho em duas etapas
# ---------------------------------------------------------------------------
#
# Ele roda contra o schema de sessao, que esta VAZIO. E `VALIDATE CONSTRAINT`
# em tabela vazia passa sempre — nao ha linha para contradizer a regra.
#
# Ou seja: ate aqui, a revisao estava provada apenas no caminho feliz, e o
# caminho feliz e o unico que nao precisa de duas etapas. Os tres testes abaixo
# cobram o resto:
#
#   1. com linha DE VERDADE, as duas etapas passam e a coluna vira NOT NULL;
#   2. depois da etapa 1, nulo NOVO ja e recusado — e o "o buraco para de
#      crescer" da rodada 2 deixa de ser afirmacao e vira comportamento;
#   3. com nulo ANTIGO, a etapa 2 falha NO VALIDATE, que e o lugar certo de
#      falhar: antes de qualquer coluna ser alterada.
#
# A tabela escolhida e `ai_feedback`, por tres motivos: ela tem quatro das 15
# colunas da revisao, precisa so de um restaurante para existir, e e a unica
# cujo nulo tem consequencia conhecida em producao (a retencao da LGPD que
# nunca alcancava a linha — armadilha 55).


def _um_restaurante(conexao) -> uuid.UUID:
    """Um restaurante cru, por SQL, sem passar pelo ORM.

    A fabrica de `fabricas_db` precisa de uma `Session`, e estes testes
    trabalham na CONEXAO — e nela que o DDL da migracao roda, e misturar as
    duas faria a transacao que volta deixar de ser uma so.
    """
    identificador = uuid.uuid4()
    conexao.execute(
        text(
            "INSERT INTO restaurants (id, name, slug, is_active) "
            "VALUES (:id, 'Teste do alinhamento', :slug, true)"
        ),
        {"id": identificador, "slug": f"alinhamento-{identificador.hex[:12]}"},
    )
    return identificador


def _feedback(conexao, restaurant_id: uuid.UUID, *, user_message: str | None) -> None:
    """Uma linha de `ai_feedback`, por SQL CRU — e o `None` chega como NULL.

    Pelo ORM seria impossivel escrever o nulo em `created_at` (armadilha 55:
    com `server_default`, o SQLAlchemy trata o `None` explicito como "deixe o
    banco preencher" e OMITE a coluna). Aqui a coluna e outra, mas a razao de
    usar SQL cru e a mesma que vale para a origem real destes nulos: eles nao
    vem do ORM, vem de escrita feita por fora (armadilha 33).
    """
    conexao.execute(
        text(
            "INSERT INTO ai_feedback ("
            "  restaurant_id, session_id, user_message, assistant_message,"
            "  response_type, selected_product_ids, feedback"
            ") VALUES ("
            "  :restaurant_id, :session_id, :user_message, 'Resposta',"
            "  'text', '{}'::uuid[], 'like'"
            ")"
        ),
        {
            "restaurant_id": restaurant_id,
            "session_id": f"sessao-{uuid.uuid4().hex[:8]}",
            "user_message": user_message,
        },
    )


@pytest.mark.db
def test_as_duas_etapas_passam_com_a_tabela_CHEIA(engine_de_teste):
    """`VALIDATE` em tabela vazia passa sempre. Aqui ele tem o que varrer.

    Sem este teste, a unica prova que a revisao tinha era contra zero linhas —
    e o `VALIDATE CONSTRAINT`, que e a operacao inteira da etapa 2, nao teria
    sido exercitado uma vez.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    etapa_2 = _carregar_revisao(ETAPA_2)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        restaurante = _um_restaurante(conexao)
        for _ in range(5):
            _feedback(conexao, restaurante, user_message="quanto custa a picanha?")

        # Sem `etapa_1.upgrade()`: ela ja esta na cadeia e no schema.
        with Operations.context(MigrationContext.configure(conexao)):
            etapa_2.upgrade()

        assert not _aceita_nulo(conexao, "ai_feedback", "user_message")
        # As linhas continuam la: o alinhamento nao apaga nada.
        assert conexao.execute(text("SELECT count(*) FROM ai_feedback")).scalar_one() == 5
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_depois_da_etapa_1_o_nulo_NOVO_ja_e_recusado(engine_de_teste):
    """"O buraco para de crescer" deixa de ser afirmacao e vira comportamento.

    E a propriedade que justifica aplicar a etapa 1 sozinha e deixar assar:
    ela nao repara nada, mas a partir do commit dela nenhuma linha nula NOVA
    entra. Sem isso, esperar entre as duas etapas seria so esperar.

    A recusa vem do `CHECK ... NOT VALID`, que ja cobra as linhas novas mesmo
    sem ter validado as antigas — e essa e a metade menos obvia do que
    `NOT VALID` significa.
    """
    from sqlalchemy.exc import IntegrityError

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        restaurante = _um_restaurante(conexao)

        # Nada e aplicado aqui. Desde 05/09/2026 a etapa 1 esta na cadeia, e
        # esta e a propriedade do schema DE PRODUCAO — nao mais de uma revisao
        # rodada dentro do teste. O teste ficou menor porque a garantia subiu
        # de lugar.
        #
        # SAVEPOINT, e o `rollback()` explicito depois. O `with begin_nested()`
        # nao serve aqui: o `pytest.raises` engole a excecao dentro dele, o
        # SQLAlchemy conclui que deu tudo certo e tenta `RELEASE SAVEPOINT`
        # numa transacao ja abortada — o erro que aparece entao e o do RELEASE,
        # e nao o da CHECK que o teste veio medir.
        ponto = conexao.begin_nested()
        with pytest.raises(IntegrityError) as erro:
            _feedback(conexao, restaurante, user_message=None)
        ponto.rollback()

        assert "ck_ai_feedback_user_message_nao_nula" in str(erro.value)
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_a_etapa_2_falha_no_VALIDATE_quando_sobra_nulo_antigo(engine_de_teste):
    """O desenho inteiro existe para falhar AQUI, e nunca depois.

    A etapa 2 e `VALIDATE` -> `SET NOT NULL` -> `DROP CONSTRAINT`, nessa ordem.
    Com uma linha nula antiga, ela tem que morrer no PRIMEIRO comando: nada
    alterado, transacao inteira de volta, e o erro nomeando a restricao — de
    onde sai a coluna, porque o nome e `ck_<tabela>_<coluna>_nao_nula`.

    Se falhasse depois do `SET NOT NULL` de outras colunas, o banco ficaria com
    metade do alinhamento aplicado. Nao fica: `alembic/env.py` roda o upgrade
    inteiro numa transacao so, e este teste e a demonstracao disso valendo.

    **E a etapa 0 (`scripts/nulos_nas_colunas_em_desacordo.py`) existe para
    esta falha nunca acontecer em producao** — ela conta os nulos ANTES, com a
    API no ar e sem lock nenhum. Este teste mostra o que ela evita.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import IntegrityError

    etapa_1 = _carregar_revisao(ETAPA_1)
    etapa_2 = _carregar_revisao(ETAPA_2)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        restaurante = _um_restaurante(conexao)

        # ENCENAR O NULO ANTIGO, agora que a etapa 1 esta aplicada.
        #
        # Nao da mais para inserir o nulo e so depois criar a restricao: ela ja
        # existe e recusa o INSERT (e o teste acima prova isso). O caminho e
        # derrubar, inserir e recriar `NOT VALID` — e recriar FUNCIONA sobre a
        # linha nula, que e a metade menos obvia do que `NOT VALID` significa e
        # e exatamente o estado que producao teria se sobrasse um nulo.
        restricao = etapa_1.nome_da_restricao("ai_feedback", "user_message")
        conexao.execute(text(f'ALTER TABLE ai_feedback DROP CONSTRAINT "{restricao}"'))
        _feedback(conexao, restaurante, user_message=None)
        conexao.execute(
            text(
                f'ALTER TABLE ai_feedback ADD CONSTRAINT "{restricao}" '
                'CHECK ("user_message" IS NOT NULL) NOT VALID'
            )
        )

        with Operations.context(MigrationContext.configure(conexao)):
            with pytest.raises(IntegrityError) as erro:
                etapa_2.upgrade()

        texto = str(erro.value)
        assert "ck_ai_feedback_user_message_nao_nula" in texto, texto
        # O erro do Postgres para VALIDATE nao satisfeito. E a mensagem que o
        # log do container vai carregar, e quem a ler precisa reconhecer.
        assert "violated by some row" in texto or "violada" in texto, texto
    finally:
        transacao.rollback()
        conexao.close()

    # Depois do rollback, numa conexao NOVA: o schema nao mudou.
    #
    # A afirmacao vale mais do que parece. A transacao abortada e a garantia,
    # mas quem a le no codigo esta lendo o `finally` — e o `finally` e
    # exatamente o que uma refatoracao distraida remove.
    with engine_de_teste.connect() as limpa:
        assert _aceita_nulo(limpa, "ai_feedback", "user_message")
        # A restricao da etapa 1 continua la, intacta: o DROP e o ADD do
        # encenamento voltaram junto com o resto.
        assert _restricao_existe(limpa, "ck_ai_feedback_user_message_nao_nula")


def _restricao_existe(conexao, nome: str) -> bool:
    return bool(
        conexao.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :nome"), {"nome": nome}
        ).first()
    )
