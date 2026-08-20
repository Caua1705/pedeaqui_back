"""A cópia de cardápio da revisão 0026, contra um Postgres de verdade.

**Por que ela merece teste próprio, como a 0025 mereceu.** O resto da revisão
é DDL: erra alto, e o `docker-entrypoint.sh` transforma o erro em loop de
restart antes de o Uvicorn subir (armadilha 5). A cópia não. Ela não levanta
exceção nenhuma quando está errada — ela deixa a segunda loja com **meio
cardápio**, ou com produto apontando para a categoria da loja errada, e o
sintoma aparece dias depois, na forma de um cliente que não acha a picanha.

Cinco propriedades, e cada uma tem um jeito silencioso de quebrar:

1. **A filial que adota fica com as linhas originais** — mesmos ids, mesmos
   slugs. É o que faz link já divulgado continuar funcionando.
2. **A cópia é COMPLETA**: categoria, produto, grupo de opção e opção. Sem os
   grupos, a cópia do produto entra no cardápio sem complemento e o pedido
   dela é recusado por "opção obrigatória não selecionada".
3. **Cada cópia aponta para a categoria da PRÓPRIA filial.** Apontar para a
   original é exatamente o que a FK composta passa a proibir — mas ela só
   proíbe depois; a cópia roda antes de a FK existir.
4. **O setor de impressão é remapeado pelo NOME**, e cai para nulo quando não
   há par. Copiar o id do setor de origem grava de saída o estado que a
   revisão inteira existe para tornar impossível.
5. **O embedding é copiado com o vetor da origem.** Sem isso o Rapi fica cego
   na loja nova até o worker rodar uma vez por produto — 161 chamadas pagas à
   OpenAI no caso do Júnior.

**O schema aqui já é o de depois da revisão** (a fixture `db` roda `alembic
upgrade head`). Cada teste monta um restaurante cujo cardápio inteiro está em
uma filial — que é o estado imediatamente anterior à cópia — e roda os mesmos
comandos que a revisão roda.
"""

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.printing_sector_model import PrintingSector
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

DIMENSOES_DO_VETOR = 1536


def _carregar_revisao():
    """O nome do arquivo começa com dígito, então `import` não serve."""
    caminho = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "20260820_0026_cardapio_por_filial.py"
    )
    spec = importlib.util.spec_from_file_location("revisao_0026", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


REVISAO = _carregar_revisao()


def _rodar_a_copia(db: Session) -> None:
    """Os mesmos comandos da revisão, na mesma ordem.

    Sem `op` no meio: `SQL_COPIA_O_CARDAPIO` existe justamente para que a
    parte perigosa da revisão possa ser executada fora do Alembic.
    """
    for comando in REVISAO.SQL_COPIA_O_CARDAPIO:
        db.execute(text(comando))


def _rede_com_cardapio_em_uma_filial(db: Session):
    """O estado de véspera: duas lojas, e o cardápio todo na primeira.

    É exatamente o que a revisão encontra depois de `_adotar_cardapio_existente`
    — a filial padrão adotou tudo, a outra está vazia.
    """
    restaurante = fab.criar_restaurante(db, "Junior da Picanha")
    centro = fab.criar_filial(db, restaurante, "Centro")
    centro.is_main = True
    aldeota = fab.criar_filial(db, restaurante, "Aldeota")
    db.flush()

    categoria = fab.criar_categoria(db, restaurante, nome="Carnes", filial=centro)
    produto = fab.criar_produto(
        db, restaurante, categoria, nome="Picanha", preco=Decimal("89.90")
    )
    produto.catalog_key = str(produto.id)
    db.flush()
    return restaurante, centro, aldeota, categoria, produto


def _linhas(db: Session, sql: str, **parametros) -> list[dict]:
    return [dict(linha) for linha in db.execute(text(sql), parametros).mappings().all()]


class TestOQueAFilialQueAdotaMantem:
    def test_as_linhas_originais_nao_se_movem(self, db):
        """O id e o slug do produto continuam onde estavam.

        É a propriedade que sustenta a promessa feita ao front: link já
        divulgado abre o mesmo produto que sempre abriu, porque a rota sem
        `branch_id` resolve na filial padrão — a mesma que adotou.
        """
        _, centro, _, categoria, produto = _rede_com_cardapio_em_uma_filial(db)
        id_original, slug_original = produto.id, produto.slug

        _rodar_a_copia(db)
        db.expire_all()

        original = _linhas(
            db,
            "SELECT branch_id, slug FROM products WHERE id = :id",
            id=id_original,
        )[0]
        assert original["branch_id"] == centro.id
        assert original["slug"] == slug_original
        assert categoria.branch_id == centro.id


class TestOQueAOutraFilialRecebe:
    def test_a_copia_e_completa_ate_a_opcao(self, db):
        """Categoria, produto, grupo e opção. Faltando um nível, a loja nova
        tem um cardápio que não fecha pedido."""
        _, _, aldeota, _, produto = _rede_com_cardapio_em_uma_filial(db)
        grupo = fab.criar_grupo_de_opcoes(db, produto, nome="Ponto da carne", is_required=True)
        fab.criar_opcao(db, grupo, nome="Ao ponto")
        db.flush()

        _rodar_a_copia(db)

        copia = _linhas(db, """
            SELECT p.id, p.name, p.slug, p.price
              FROM products p WHERE p.branch_id = :filial
        """, filial=aldeota.id)
        assert len(copia) == 1
        assert copia[0]["name"] == "Picanha"
        assert copia[0]["price"] == Decimal("89.90")
        # O slug se REPETE entre as lojas, e é assim que tem que ser: o
        # índice único passou a ser (branch_id, slug).
        assert copia[0]["slug"] == produto.slug

        grupos = _linhas(db, """
            SELECT g.id, g.name, g.is_required
              FROM product_option_groups g
              JOIN products p ON p.id = g.product_id
             WHERE p.branch_id = :filial
        """, filial=aldeota.id)
        assert [g["name"] for g in grupos] == ["Ponto da carne"]
        assert grupos[0]["is_required"] is True

        opcoes = _linhas(db, """
            SELECT o.name
              FROM product_options o
              JOIN product_option_groups g ON g.id = o.option_group_id
              JOIN products p ON p.id = g.product_id
             WHERE p.branch_id = :filial
        """, filial=aldeota.id)
        assert [o["name"] for o in opcoes] == ["Ao ponto"]

    def test_a_copia_aponta_para_a_categoria_da_propria_filial(self, db):
        """O erro mais fácil de cometer e o mais difícil de ver.

        Um produto copiado para a Aldeota apontando para a categoria do Centro
        não quebra INSERT nenhum enquanto a FK composta não existe — e a
        revisão cria a FK depois da cópia. Se isso acontecesse, o `upgrade`
        morreria lá na frente com uma violação de FK que não diz de onde veio.
        """
        _, centro, aldeota, categoria_do_centro, _ = _rede_com_cardapio_em_uma_filial(db)

        _rodar_a_copia(db)

        pares = _linhas(db, """
            SELECT p.branch_id AS filial_do_produto,
                   c.branch_id AS filial_da_categoria
              FROM products p
              JOIN categories c ON c.id = p.category_id
             WHERE p.restaurant_id = c.restaurant_id
        """)
        assert pares, "a cópia não gerou produto nenhum"
        for par in pares:
            assert par["filial_do_produto"] == par["filial_da_categoria"]

        da_aldeota = _linhas(db, """
            SELECT category_id FROM products WHERE branch_id = :filial
        """, filial=aldeota.id)
        assert da_aldeota[0]["category_id"] != categoria_do_centro.id
        assert centro.id != aldeota.id

    def test_a_chave_de_catalogo_e_a_mesma_nas_duas_lojas(self, db):
        """A pergunta inteira que `catalog_key` existe para responder.

        Se a cópia gerasse chave nova, "quanto vendi de picanha nas duas
        lojas" ficaria sem resposta no dia seguinte ao deploy — e ligar as
        duas linhas depois exigiria alguém dizer à mão quais são pares.
        """
        _, _, _, _, produto = _rede_com_cardapio_em_uma_filial(db)

        _rodar_a_copia(db)

        chaves = _linhas(db, """
            SELECT DISTINCT catalog_key FROM products WHERE name = 'Picanha'
        """)
        assert [linha["catalog_key"] for linha in chaves] == [str(produto.id)]


class TestOSetorDeImpressao:
    def test_o_setor_e_remapeado_pelo_nome_na_filial_de_destino(self, db):
        """A "Cozinha" da Aldeota é outra máquina, e é para ela que a cópia aponta."""
        _, centro, aldeota, _, produto = _rede_com_cardapio_em_uma_filial(db)
        cozinha_do_centro = PrintingSector(branch_id=centro.id, name="Cozinha")
        cozinha_da_aldeota = PrintingSector(branch_id=aldeota.id, name="Cozinha")
        db.add_all([cozinha_do_centro, cozinha_da_aldeota])
        db.flush()
        produto.printing_sector_id = cozinha_do_centro.id
        db.flush()

        _rodar_a_copia(db)

        copiado = _linhas(db, """
            SELECT printing_sector_id FROM products WHERE branch_id = :filial
        """, filial=aldeota.id)[0]
        assert copiado["printing_sector_id"] == cozinha_da_aldeota.id

    def test_sem_setor_de_mesmo_nome_a_copia_fica_sem_setor(self, db):
        """Nulo, e não o id do setor da outra loja.

        Nulo é um estado que o `check_restaurant.py` aponta, filial a filial.
        O id da outra loja seria gravar de saída exatamente o par que a FK
        composta desta revisão passa a proibir — e o `upgrade` morreria na
        criação dela, depois de a cópia já ter rodado.
        """
        _, centro, aldeota, _, produto = _rede_com_cardapio_em_uma_filial(db)
        so_no_centro = PrintingSector(branch_id=centro.id, name="Forno")
        db.add(so_no_centro)
        db.flush()
        produto.printing_sector_id = so_no_centro.id
        db.flush()

        _rodar_a_copia(db)

        copiado = _linhas(db, """
            SELECT printing_sector_id FROM products WHERE branch_id = :filial
        """, filial=aldeota.id)[0]
        assert copiado["printing_sector_id"] is None

class TestOIndiceDoRapi:
    def test_o_vetor_e_reaproveitado_em_vez_de_recomprado(self, db):
        """Zero chamada à OpenAI, e o Rapi respondendo na loja nova no boot.

        O `content_hash` copiado é o mesmo porque o conteúdo indexado (nome +
        código + categoria + descrição) é idêntico na cópia. É ele que faz a
        varredura seguinte cair no caminho "touched".
        """
        _, _, aldeota, _, produto = _rede_com_cardapio_em_uma_filial(db)
        vetor = "[" + ",".join(["0.1"] * DIMENSOES_DO_VETOR) + "]"
        db.execute(text("""
            INSERT INTO ai_product_embeddings
                   (restaurant_id, product_id, content, content_hash, metadata,
                    embedding, updated_at)
            VALUES (:restaurante, :produto, 'Nome: Picanha', 'hash-da-picanha',
                    '{}'::jsonb, CAST(:vetor AS vector), now())
        """), {
            "restaurante": produto.restaurant_id,
            "produto": produto.id,
            "vetor": vetor,
        })

        _rodar_a_copia(db)

        copiado = _linhas(db, """
            SELECT ape.content_hash, ape.content
              FROM ai_product_embeddings ape
              JOIN products p ON p.id = ape.product_id
             WHERE p.branch_id = :filial
        """, filial=aldeota.id)
        assert len(copiado) == 1
        assert copiado[0]["content_hash"] == "hash-da-picanha"
        assert copiado[0]["content"] == "Nome: Picanha"

    def test_o_carimbo_copiado_deixa_a_linha_pendente_de_varredura(self, db):
        """O metadata da cópia carrega o `product_id` da ORIGEM por algumas horas.

        Isso não afeta busca nenhuma (a busca lê as colunas de `products`,
        pela junção), mas precisa ser corrigido — e quem corrige é a varredura
        seguinte, de graça, porque `ape.updated_at` copiado é MENOR que o
        `products.updated_at` da linha recém-inserida.

        Se o carimbo viesse `now()`, a linha nasceria "em dia" e o metadata
        errado ficaria lá para sempre.
        """
        _, _, aldeota, _, produto = _rede_com_cardapio_em_uma_filial(db)
        vetor = "[" + ",".join(["0.1"] * DIMENSOES_DO_VETOR) + "]"
        db.execute(text("""
            INSERT INTO ai_product_embeddings
                   (restaurant_id, product_id, content, content_hash, metadata,
                    embedding, updated_at)
            VALUES (:restaurante, :produto, 'Nome: Picanha', 'hash-da-picanha',
                    '{}'::jsonb, CAST(:vetor AS vector), now() - interval '1 hour')
        """), {
            "restaurante": produto.restaurant_id,
            "produto": produto.id,
            "vetor": vetor,
        })

        _rodar_a_copia(db)

        pendente = _linhas(db, """
            SELECT ape.updated_at < p.updated_at AS pendente
              FROM ai_product_embeddings ape
              JOIN products p ON p.id = ape.product_id
             WHERE p.branch_id = :filial
        """, filial=aldeota.id)[0]
        assert pendente["pendente"] is True


class TestOsCasosDeBorda:
    def test_restaurante_com_uma_filial_so_nao_ganha_copia_nenhuma(self, db):
        """A maioria da base. A cópia tem que ser no-op para ela."""
        restaurante = fab.criar_restaurante(db, "Uma Loja So")
        matriz = fab.criar_filial(db, restaurante, "Matriz")
        categoria = fab.criar_categoria(db, restaurante, filial=matriz)
        fab.criar_produto(db, restaurante, categoria, nome="Prato Feito")
        db.flush()

        _rodar_a_copia(db)

        total = db.execute(text("""
            SELECT count(*) FROM products WHERE restaurant_id = :id
        """), {"id": restaurante.id}).scalar()
        assert total == 1

    def test_filial_desativada_nao_recebe_copia(self, db):
        """Loja desligada não vende, e um cardápio nela seria lixo com FK.

        Se ela for religada depois, o lojista cadastra o cardápio dela — que é
        o mesmo trabalho que teria numa loja nova.
        """
        restaurante = fab.criar_restaurante(db, "Com Loja Fechada")
        matriz = fab.criar_filial(db, restaurante, "Matriz")
        desligada = fab.criar_filial(db, restaurante, "Antiga")
        desligada.is_active = False
        db.flush()
        categoria = fab.criar_categoria(db, restaurante, filial=matriz)
        fab.criar_produto(db, restaurante, categoria, nome="Prato Feito")
        db.flush()

        _rodar_a_copia(db)

        na_desligada = db.execute(text("""
            SELECT count(*) FROM products WHERE branch_id = :id
        """), {"id": desligada.id}).scalar()
        assert na_desligada == 0

    def test_tres_filiais_recebem_uma_copia_cada(self, db):
        """A conta que o Júnior vai ver: N filiais, N cópias do cardápio."""
        restaurante = fab.criar_restaurante(db, "Tres Lojas")
        matriz = fab.criar_filial(db, restaurante, "Matriz")
        matriz.is_main = True
        fab.criar_filial(db, restaurante, "Segunda")
        fab.criar_filial(db, restaurante, "Terceira")
        db.flush()
        categoria = fab.criar_categoria(db, restaurante, filial=matriz)
        for indice in range(4):
            fab.criar_produto(db, restaurante, categoria, nome=f"Produto {indice}")
        db.flush()

        _rodar_a_copia(db)

        por_filial = _linhas(db, """
            SELECT branch_id, count(*) AS quantos
              FROM products WHERE restaurant_id = :id
             GROUP BY branch_id
        """, id=restaurante.id)
        assert len(por_filial) == 3
        assert {linha["quantos"] for linha in por_filial} == {4}


class TestARecusaDeCardapioSemFilial:
    def test_a_consulta_da_revisao_encontra_o_orfao(self, db):
        """A revisão para com mensagem em vez de deixar o SET NOT NULL falhar.

        O teste mede a CONSULTA, não a exceção: o `raise` está numa função que
        usa `op.get_bind()`, e o que pode envelhecer sem ninguém ver é o SQL
        que decide se há órfão.
        """
        restaurante = fab.criar_restaurante(db, "Sem Filial")
        categoria = fab.criar_categoria(db, restaurante)
        db.flush()

        # Simula o estado de meio da revisão: a coluna existe e está nula
        # porque o restaurante não tinha filial para adotar.
        db.execute(text("ALTER TABLE categories ALTER COLUMN branch_id DROP NOT NULL"))
        db.execute(
            text("UPDATE categories SET branch_id = NULL WHERE id = :id"),
            {"id": categoria.id},
        )

        orfaos = db.execute(text("""
            SELECT string_agg(DISTINCT r.slug, ', ')
              FROM categories t
              JOIN restaurants r ON r.id = t.restaurant_id
             WHERE t.branch_id IS NULL
        """)).scalar()
        assert orfaos == restaurante.slug


class TestAOrdemDosPassosDaRevisao:
    """A dependência que só quebra com dado dentro, e por isso quase escapou.

    `_trocar_indices_de_slug` tem que rodar **antes** da cópia. A cópia grava
    a segunda "Carnes" do mesmo restaurante, com o mesmo slug, numa filial
    diferente — e `uq_categories_restaurant_slug` recusa exatamente isso.

    Num banco vazio a cópia é no-op e a ordem errada passa despercebida: a
    suíte inteira fica verde. No Júnior, com duas lojas, o `alembic upgrade`
    morre no INSERT e o container entra em loop de restart (armadilha 5).
    """

    def test_a_copia_seria_recusada_pelo_indice_antigo(self, db):
        """Recria o índice antigo e prova que ele derruba a cópia.

        O teste afirma o CONTRÁRIO do que se quer em produção, de propósito:
        ele documenta por que a ordem do `upgrade` é a que é. Se alguém mover
        `_trocar_indices_de_slug` para depois da cópia, este teste continua
        verde — e o que fica vermelho é o de cima, que roda a cópia sob os
        índices novos.
        """
        _rede_com_cardapio_em_uma_filial(db)
        db.execute(text("DROP INDEX uq_categories_branch_slug"))
        db.execute(text(
            "CREATE UNIQUE INDEX uq_categories_restaurant_slug"
            " ON categories (restaurant_id, slug)"
        ))

        with pytest.raises(IntegrityError):
            _rodar_a_copia(db)

    def test_a_ordem_declarada_no_upgrade_poe_os_indices_antes_da_copia(self):
        """Lê a ordem das chamadas no `upgrade`, sem banco.

        É a rede que roda no laço rápido: um `upgrade` reordenado por engano
        aparece aqui antes de alguém subir o Postgres.
        """
        import inspect

        corpo = inspect.getsource(REVISAO.upgrade)
        posicao_dos_indices = corpo.index("_trocar_indices_de_slug()")
        posicao_da_copia = corpo.index("_copiar_cardapio_para_as_outras_filiais()")

        assert posicao_dos_indices < posicao_da_copia

        # E as amarras vêm DEPOIS da cópia: as cópias precisam existir para
        # satisfazer o NOT NULL e as FKs compostas.
        assert posicao_da_copia < corpo.index("_travar_not_null()")
        assert posicao_da_copia < corpo.index("_criar_amarras_de_filial()")

