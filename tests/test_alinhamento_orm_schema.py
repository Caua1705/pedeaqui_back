"""As duas etapas do alinhamento ORM x schema, agora as DUAS na cadeia.

Este arquivo substitui `tests/test_revisoes_preparadas.py`, e a substituicao e
a mudanca de estado: ate 04/09/2026 a etapa 2 morava em `alembic/preparadas/`,
e o que havia a cobrar era que o Alembic NAO a conhecesse. Hoje ela e
`20260905_0056`, o dono decidiu aplicar as duas na mesma janela, e o que ha a
cobrar e outra coisa — que elas facam o que dizem.

**O schema de sessao ja vem com as duas.** `tests/conftest.py` monta o banco
com o baseline mais `alembic upgrade head`, entao as 15 colunas chegam aqui
`NOT NULL` e as `CHECK ... NOT VALID` da etapa 1 ja foram derrubadas pela
etapa 2. Os testes que precisam de um estado ANTERIOR o encenam com
`etapa_2.downgrade()`, dentro de uma transacao que volta — o que tem o efeito
util de exercitar o downgrade em todo teste que o usa, e nao so no que o mede.

O que continua valendo, e e o motivo de o arquivo nao ter encolhido para dois
`assert`: `VALIDATE CONSTRAINT` em tabela VAZIA passa sempre. O caminho feliz e
o unico que nao precisa de duas etapas, e provar so ele seria nao provar nada.
Os tres testes do fim usam `ai_feedback` com linha de verdade — ela tem quatro
das 15 colunas, precisa so de um restaurante para existir, e e a unica cujo
nulo tem consequencia conhecida em producao (a retencao da LGPD que nunca
alcancava a linha, armadilha 55).
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
VERSIONS = RAIZ / "alembic" / "versions"
PREPARADAS = RAIZ / "alembic" / "preparadas"

ETAPA_1 = VERSIONS / "20260905_0055_alinhamento_orm_schema_etapa_1.py"
ETAPA_2 = VERSIONS / "20260905_0056_alinhamento_orm_schema_etapa_2.py"


def _valor_do_modulo(caminho: Path, nome: str):
    """Le uma constante do arquivo sem importa-lo.

    Importar traria `from alembic import op` junto, e o `op` de uma revisao so
    existe dentro de um contexto de migracao. `ast.literal_eval` le o valor
    escrito, que e o que interessa.
    """
    arvore = ast.parse(caminho.read_text(encoding="utf-8"), filename=str(caminho))
    for no in arvore.body:
        if isinstance(no, ast.Assign) and any(
            isinstance(alvo, ast.Name) and alvo.id == nome for alvo in no.targets
        ):
            return ast.literal_eval(no.value)
    raise AssertionError(f"{caminho.name} nao define {nome}")


def _carregar_revisao(caminho: Path):
    """Importa o arquivo da revisao pelo caminho, como o proprio Alembic faz."""
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


def _restricao_existe(conexao, nome: str) -> bool:
    return bool(
        conexao.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :nome"), {"nome": nome}
        ).first()
    )


# ---------------------------------------------------------------------------
# As duas revisoes, lidas como arquivo
# ---------------------------------------------------------------------------


def test_as_duas_etapas_descrevem_as_mesmas_colunas():
    """A etapa 2 valida o que a etapa 1 criou. Listas diferentes = revisao quebrada.

    A duplicacao da lista e deliberada (o motivo esta no comentario da etapa 2:
    revisao e modulo carregado por caminho, e o import quebraria na hora do
    `git mv` — que ja aconteceu duas vezes agora, e nas duas nao quebrou nada).
    O preco da duplicacao e este teste.
    """
    etapa_1 = _valor_do_modulo(ETAPA_1, "COLUNAS")
    etapa_2 = _valor_do_modulo(ETAPA_2, "COLUNAS")

    assert etapa_1 == etapa_2, (
        "As duas etapas do alinhamento listam colunas diferentes.\n"
        "A etapa 2 roda `VALIDATE CONSTRAINT` sobre restricoes que a etapa 1 "
        "criou: coluna so na etapa 2 falha com 'constraint does not exist', e "
        "coluna so na etapa 1 fica com uma CHECK NOT VALID orfa para sempre."
    )


def test_a_etapa_2_vem_logo_depois_da_etapa_1_na_cadeia():
    """Nada pode entrar ENTRE as duas, e a ordem nao pode inverter.

    Nao e preciosismo de numeracao: a etapa 2 e `VALIDATE` sobre restricoes que
    a etapa 1 cria. Uma revisao encaixada no meio nao quebra isso sozinha, mas
    quebra o roteiro do deploy — que manda parar em `20260905_0055` com
    `ALEMBIC_TARGET` e seguir para head, e passaria a aplicar essa terceira
    junto com a etapa 2, sem ninguem ter decidido.
    """
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    etapa_2 = script.get_revision("20260905_0056")

    assert etapa_2.down_revision == "20260905_0055", (
        f"a etapa 2 aponta para {etapa_2.down_revision!r}, e nao para a etapa 1"
    )


def test_nada_em_preparadas_esta_na_cadeia():
    """Guarda do mecanismo, e hoje ele esta VAZIO.

    `alembic/preparadas/` nao tem revisao nenhuma desde 04/09/2026 — as duas
    que moravam la foram aplicadas. O diretorio e o LEIA-ME continuam porque o
    mecanismo continua (armadilha 53), e esta guarda continua porque o dia em
    que alguem escrever a proxima preparada e o dia em que ela pode cair em
    `versions/` por um `git mv` distraido.

    Com o diretorio vazio o teste passa sem afirmar nada, e isso esta dito aqui
    de proposito: quem for ler um verde deste arquivo tem que saber que a
    substancia esta nos outros testes, e nao neste.
    """
    script = ScriptDirectory.from_config(Config(str(RAIZ / "alembic.ini")))
    conhecidas = {revisao.revision for revisao in script.walk_revisions()}

    for arquivo in sorted(PREPARADAS.glob("*.py")):
        identificador = _valor_do_modulo(arquivo, "revision")
        assert identificador not in conhecidas, (
            f"{arquivo.name} declara revision={identificador!r} e o Alembic a "
            "conhece: ela ESTA na cadeia e o proximo `alembic upgrade head` a "
            "aplica, inclusive no container de producao."
        )


# ---------------------------------------------------------------------------
# O schema que o repositorio constroi — com as duas etapas ja aplicadas
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_a_primeira_classe_de_divergencia_foi_a_zero(engine_de_teste):
    """"ORM diz NOT NULL, banco aceita NULL" tem que dizer `Nenhuma.`.

    E a afirmacao que o alinhamento inteiro existe para poder fazer, e ela e
    feita contra o schema que o REPOSITORIO constroi (baseline + `upgrade
    head`) — o de producao por construcao, pela armadilha 33.

    Este teste tambem e o que segura a lista das duas revisoes no dia em que
    alguem criar uma divergencia nova: ela apareceria aqui, e nao na noite da
    aplicacao.
    """
    reais = sorted(comparar(inspect(engine_de_teste)).orm_mais_estrito)

    assert reais == [], (
        "Voltou a haver coluna em que o ORM diz NOT NULL e o banco aceita "
        f"NULL: {reais}\n\n"
        "Ou uma revisao nova relaxou uma das 15, ou um model novo declara "
        "`nullable=False` sobre coluna que o banco deixa nula. Ver "
        "docs/alinhamento-orm-schema.md."
    )


@pytest.mark.db
def test_as_15_colunas_estao_NOT_NULL_no_schema(engine_de_teste):
    """A mesma verdade pelo outro lado, lida coluna a coluna do `inspect()`.

    O teste acima pergunta ao comparador; este pergunta ao banco. Sao duas
    perguntas porque o comparador e codigo nosso, e um defeito nele deixaria o
    outro teste verde sobre um schema errado.
    """
    colunas = _valor_do_modulo(ETAPA_1, "COLUNAS")

    with engine_de_teste.connect() as conexao:
        nulaveis = [
            f"{tabela}.{coluna}"
            for tabela, coluna in colunas
            if _aceita_nulo(conexao, tabela, coluna)
        ]

    assert not nulaveis, f"depois do `upgrade head` estas ainda aceitam NULL: {nulaveis}"


@pytest.mark.db
def test_a_etapa_2_nao_deixou_CHECK_para_tras(engine_de_teste):
    """As `ck_..._nao_nula` sao andaime, e o terceiro comando as derruba.

    Manter uma delas seria pedir ao Postgres que conferisse duas vezes a mesma
    coisa em todo INSERT — e a duplicata de constraint e a armadilha 15 na
    forma que o `audit_indexes.py` nao ve.
    """
    etapa_1 = _carregar_revisao(ETAPA_1)

    with engine_de_teste.connect() as conexao:
        sobraram = [
            etapa_1.nome_da_restricao(tabela, coluna)
            for tabela, coluna in etapa_1.COLUNAS
            if _restricao_existe(conexao, etapa_1.nome_da_restricao(tabela, coluna))
        ]

    assert not sobraram, f"a etapa 2 deixou restricoes vivas: {sobraram}"


@pytest.mark.db
def test_o_downgrade_da_etapa_2_devolve_o_estado_do_fim_da_etapa_1(engine_de_teste):
    """Voltar tem que dar EXATAMENTE o fim da etapa 1: nulavel, com a CHECK.

    E o unico rollback que este deploy pode precisar no meio da noite, e ele
    nao pode ser "quase": voltar sem recriar as `NOT VALID` deixaria o buraco
    aberto de novo, e recriar sem soltar o `NOT NULL` nao voltaria nada.

    Tudo dentro de uma transacao que volta — o Postgres tem DDL transacional, e
    e por isso que este teste pode existir sem estragar o schema de sessao que
    os outros testes `db` compartilham. O `finally` nao e decoracao.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    etapa_1 = _carregar_revisao(ETAPA_1)
    etapa_2 = _carregar_revisao(ETAPA_2)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            etapa_2.downgrade()

        nao_voltou = [
            f"{tabela}.{coluna}"
            for tabela, coluna in etapa_1.COLUNAS
            if not _aceita_nulo(conexao, tabela, coluna)
        ]
        assert not nao_voltou, f"continuam NOT NULL depois do downgrade: {nao_voltou}"

        sem_check = [
            f"{tabela}.{coluna}"
            for tabela, coluna in etapa_1.COLUNAS
            if not _restricao_existe(conexao, etapa_1.nome_da_restricao(tabela, coluna))
        ]
        assert not sem_check, (
            "o downgrade soltou o NOT NULL e NAO recriou a CHECK destas — o "
            f"buraco fica aberto: {sem_check}"
        )
    finally:
        transacao.rollback()
        conexao.close()


# ---------------------------------------------------------------------------
# O que so aparece com linha de verdade
# ---------------------------------------------------------------------------
#
# `VALIDATE CONSTRAINT` em tabela vazia passa sempre — nao ha linha para
# contradizer a regra. Os tres testes abaixo poem linha na mesa:
#
#   1. com linha DE VERDADE, a etapa 2 passa e a coluna vira NOT NULL;
#   2. no estado do fim da etapa 1, nulo NOVO ja e recusado — e o "o buraco
#      para de crescer" deixa de ser afirmacao e vira comportamento;
#   3. com nulo ANTIGO, a etapa 2 falha NO VALIDATE, que e o lugar certo de
#      falhar: antes de qualquer coluna ser alterada.


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

    Pelo ORM seria impossivel escrever nulo em coluna com `server_default`
    (armadilha 55: o SQLAlchemy trata o `None` explicito como "deixe o banco
    preencher" e OMITE a coluna). Aqui a coluna e outra, mas a razao de usar SQL
    cru e a mesma que vale para a origem real destes nulos: eles nao vem do
    ORM, vem de escrita feita por fora (armadilha 33).
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
def test_a_etapa_2_passa_com_a_tabela_CHEIA(engine_de_teste):
    """`VALIDATE` em tabela vazia passa sempre. Aqui ele tem o que varrer.

    Sem este teste, a unica prova que a revisao teria era contra zero linhas —
    e o `VALIDATE CONSTRAINT`, que e a operacao inteira da etapa 2, nao teria
    sido exercitado uma vez.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    etapa_2 = _carregar_revisao(ETAPA_2)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            etapa_2.downgrade()

        restaurante = _um_restaurante(conexao)
        for _ in range(5):
            _feedback(conexao, restaurante, user_message="quanto custa a picanha?")

        with Operations.context(MigrationContext.configure(conexao)):
            etapa_2.upgrade()

        assert not _aceita_nulo(conexao, "ai_feedback", "user_message")
        # As linhas continuam la: o alinhamento nao apaga nada.
        assert conexao.execute(text("SELECT count(*) FROM ai_feedback")).scalar_one() == 5
    finally:
        transacao.rollback()
        conexao.close()


@pytest.mark.db
def test_no_estado_do_fim_da_etapa_1_o_nulo_NOVO_ja_e_recusado(engine_de_teste):
    """"O buraco para de crescer" deixa de ser afirmacao e vira comportamento.

    E a propriedade que torna seguro aplicar a etapa 1 e so depois a etapa 2,
    seja o intervalo de dez minutos ou de duas semanas: a etapa 1 nao repara
    nada, mas a partir dela nenhuma linha nula NOVA entra. Sem isso, o intervalo
    entre as duas seria uma janela de risco em vez de uma pausa.

    A recusa vem do `CHECK ... NOT VALID`, que ja cobra as linhas novas mesmo
    sem ter validado as antigas — e essa e a metade menos obvia do que
    `NOT VALID` significa.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import IntegrityError

    etapa_2 = _carregar_revisao(ETAPA_2)

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        # Volta ao fim da etapa 1: colunas nulaveis outra vez, com as 15 CHECK
        # `NOT VALID` no lugar. E o estado em que producao fica entre as duas
        # execucoes do deploy.
        with Operations.context(MigrationContext.configure(conexao)):
            etapa_2.downgrade()

        restaurante = _um_restaurante(conexao)

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
    inteiro numa transacao so, e este teste e a demonstracao disso valendo. E o
    que responde, com o Postgres e nao com o manual, a pergunta "se a segunda
    execucao falhar no meio, eu reverto ou tento de novo?" — nao ha meio.

    **E a etapa 0 (`scripts/nulos_nas_colunas_em_desacordo.py`) existe para
    esta falha nunca acontecer em producao** — ela conta os nulos ANTES, com a
    API no ar e sem lock nenhum. Este teste mostra o que ela evita.
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy.exc import IntegrityError

    etapa_1 = _carregar_revisao(ETAPA_1)
    etapa_2 = _carregar_revisao(ETAPA_2)
    restricao = etapa_1.nome_da_restricao("ai_feedback", "user_message")

    conexao = engine_de_teste.connect()
    transacao = conexao.begin()
    try:
        with Operations.context(MigrationContext.configure(conexao)):
            etapa_2.downgrade()

        restaurante = _um_restaurante(conexao)

        # ENCENAR O NULO ANTIGO. Nao da para inserir o nulo e so depois criar a
        # restricao: no fim da etapa 1 ela ja existe e recusa o INSERT (e o
        # teste acima prova isso). O caminho e derrubar, inserir e recriar
        # `NOT VALID` — e recriar FUNCIONA sobre a linha nula, que e a metade
        # menos obvia do que `NOT VALID` significa, e e exatamente o estado que
        # producao teria se sobrasse um nulo.
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
        assert restricao in texto, texto
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
        assert not _aceita_nulo(limpa, "ai_feedback", "user_message")
        assert not _restricao_existe(limpa, restricao)
