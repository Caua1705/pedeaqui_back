"""o cardapio passa a ser por filial

Revision ID: 20260820_0026
Revises: 20260818_0025
Create Date: 2026-08-20

## O que muda, em uma frase

`categories` e `products` deixam de ser do restaurante e passam a ser da
FILIAL. Cada loja tem os proprios produtos, os proprios precos e a propria
disponibilidade. **Nao ha heranca e nao ha sobrescrita** — ao contrario do
termo comercial da revisao 20260818_0025, aqui nao existe "NULL = herda".

## Por que nao heranca, ja que o passo 2 escolheu heranca para os precos

Sao perguntas diferentes. `min_order_value` e o mesmo numero negociado pela
marca, e a filial diverge por excecao. Cardapio nao: a loja do shopping nao
vende costela, a do Centro nao tem forno de pizza, e o preco da picanha muda
com o aluguel da rua. "Cardapio do restaurante com sobrescrita por filial"
descreveria uma rede em que as lojas sao quase iguais — e se fossem, o
lojista nao teria pedido isto.

O custo e real e foi aceito: cadastrar um produto novo na rede e cadastra-lo
N vezes. Foi por isso que `catalog_key` entrou (ver abaixo).

## `catalog_key`, a unica coisa que nao dava para deixar para depois

Coluna OPCIONAL em `products`, sem semantica nenhuma de heranca: nada no
cardapio, no pedido ou no Rapi a le para decidir preco ou disponibilidade.
Ela existe para uma pergunta so — "quanto vendi de picanha nas duas lojas" —
e sem ela essa pergunta deixa de ter resposta por falta de chave.

E a unica parte deste passo que nao daria para acrescentar depois sem
recadastrar tudo: acrescentar a coluna e facil, mas ligar as duas linhas que
JA existem, uma em cada loja, exigiria alguem dizer a mao quais sao pares.
Aqui a copia ainda sabe de quem ela e copia, entao a chave nasce preenchida
de graca: `catalog_key` = id do produto de origem, no original E nas copias.

Nao e o `products.code`: aquele e o codigo que o lojista imprime e busca no
painel, texto livre, sem unicidade, e entra no conteudo indexado do Rapi.
Sobrepor uma segunda semantica a ele mudaria o significado das linhas que ja
estao gravadas.

`order_items.catalog_key_snapshot` vem junto e pelo mesmo motivo que
`product_name_snapshot` existe: `product_id` aponta para a linha de UMA
filial, e a linha pode ser renomeada depois. Congelada no item, a chave faz
`/admin/reports/products` somar as duas lojas na mesma linha mesmo que uma
delas renomeie o produto.

## As quatro FKs compostas: erro possivel vira erro impossivel de gravar

Ate aqui, "produto da filial A apontando para o setor de impressao da filial
B" era um estado GRAVAVEL, e o sistema o tratava depois — a via "SEM SETOR"
de `admin_printing_service.py` existe exatamente para ele (armadilha 13).
Com o produto tendo filial, da para fechar a porta no banco:

    products (branch_id, category_id)        -> categories (branch_id, id)
    products (branch_id, printing_sector_id) -> printing_sectors (branch_id, id)
    products (restaurant_id, branch_id)      -> branches (restaurant_id, id)
    categories (restaurant_id, branch_id)    -> branches (restaurant_id, id)

As duas ultimas amarram o `restaurant_id` que fica nas duas tabelas: ele e
redundante (a filial ja diz o restaurante) e continua la porque metade das
consultas do repositorio filtra por ele — a FK e o que impede os dois de
divergirem sem ninguem ver.

`printing_sector_id` e nulavel e continua podendo ser nulo: FK composta com
`MATCH SIMPLE` (o default) passa quando QUALQUER coluna do par e nula. O
"este produto nao imprime via de producao" nao foi afetado.

Custam tres UNIQUE novos, em tabelas pequenas. Se algum dado atual violar,
esta revisao RECUSA aplicar — que e o ponto: melhor descobrir no deploy que
em producao.

## A copia de dados

1. **A filial padrao adota as linhas existentes, em lugar.** Mesmos ids,
   mesmos slugs, mesmas imagens. A regra e a de `BranchRepository.
   get_default_branch` — principal se houver, senao a primeira ativa em ordem
   alfabetica — e ela e a mesma que `/menu` e a rota publica de produto usam
   quando o `branch_id` nao vem. **E o que faz todo link ja divulgado
   continuar abrindo exatamente o produto que abria.**

2. **Cada outra filial ativa recebe uma copia** de categorias, produtos,
   grupos de opcao e opcoes. Cadastro nenhum e refeito a mao.

3. **O setor de impressao da copia e remapeado pelo NOME**, dentro da filial
   de destino. Sem correspondencia, fica nulo — e o `check_restaurant.py`
   aponta. Copiar o id do setor de origem seria gravar de saida exatamente o
   estado que as FKs acima passam a proibir.

4. **Os embeddings do Rapi sao copiados junto, reaproveitando o vetor.** O
   conteudo indexado e nome + codigo + categoria + descricao
   (`build_product_content`), identico na copia, entao o `content_hash` bate:
   a varredura seguinte cai no caminho "touched", corrige o `metadata` e NAO
   compra um embedding sequer. Sem isto o Rapi ficaria cego na filial nova
   ate o worker rodar uma vez por produto, pagando uma chamada a OpenAI em
   cada uma.

   O `metadata` copiado carrega `product_id` e `category_id` da ORIGEM por
   algumas horas. Nao afeta busca nenhuma — `similarity_search` le nome,
   preco e id das colunas de `products`, pela juncao, e do `metadata` so
   aproveita `short_description` e `category_name`, que sao iguais nas duas
   linhas.

## Restaurante sem nenhuma filial

Nao ha para onde mandar o cardapio dele, e `branch_id` e NOT NULL. A revisao
CONTA as linhas orfas e para com mensagem explicita, em vez de deixar o
`SET NOT NULL` falhar com "column contains null values" — que nao diz a
ninguem o que fazer.

## O downgrade perde as lojas

Ele apaga os produtos e categorias das filiais nao-padrao, INCLUSIVE os que o
lojista criou depois do deploy. Nao existe resposta melhor com uma coluna so.

E ele RECUSA descer se algum `order_items` ja apontar para uma dessas copias:
apagar o produto apagaria o historico de pedido que o cliente ainda consulta,
e isso e pior que nao descer.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260820_0026"
down_revision: Union[str, None] = "20260818_0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A filial que ADOTA as linhas que ja existem, uma por restaurante.
#
# `is_main DESC, name ASC` entre as ATIVAS e exatamente a regra de
# `BranchRepository.get_default_branch`, e e a mesma que `/menu` e a rota
# publica de produto aplicam quando `branch_id` nao vem. E o que faz todo
# link ja divulgado continuar resolvendo para a linha que ele sempre
# resolveu.
#
# O `is_active DESC` na frente e o degrau para o restaurante que nao tem
# NENHUMA filial ativa: uma filial desligada ainda e um lugar melhor para o
# cardapio do que nulo — o lojista religa a loja e o cardapio esta la. Entre
# filiais ativas ele nao muda nada, porque todas empatam nele.
#
# UMA definicao, usada na subida E na descida: se a descida derivasse a
# filial por outro caminho, ela apagaria o lado errado do cardapio.
FILIAL_QUE_ADOTA = """
SELECT DISTINCT ON (restaurant_id)
       restaurant_id,
       id AS branch_id
  FROM branches
 ORDER BY restaurant_id, is_active DESC NULLS LAST, is_main DESC NULLS LAST, name ASC
"""


# A COPIA, na ordem, como constante — e nao inline no corpo da funcao.
#
# Mesmo arranjo do `SQL_COPIA_O_ESTADO_DO_DIA` da revisao 20260818_0025, e
# pelo mesmo motivo: e a unica parte desta revisao que pode dar errado EM
# SILENCIO. O resto e DDL, e DDL errado derruba o `alembic upgrade` e vira
# loop de restart antes de o Uvicorn subir (armadilha 5). Uma copia errada
# nao levanta nada — ela so deixa a segunda loja com meio cardapio, ou com
# produto apontando para a categoria da loja errada.
#
# Como constante, `tests/test_migracao_cardapio_por_filial_db.py` executa
# exatamente estes comandos contra um Postgres de verdade.
#
# A ORDEM E OBRIGATORIA: categoria -> produto -> grupo -> opcao. Cada nivel
# usa o mapa (origem -> copia) do nivel de cima para apontar para a copia
# certa, e nao para a linha original de outra loja.
SQL_COPIA_O_CARDAPIO = (
    # 1. Que categorias serao copiadas, e para qual filial.
    #
    # So filial ATIVA recebe copia. A juncao ja cuida do restaurante que nao
    # tem nenhuma: ele nao produz par nenhum e sai daqui com o cardapio
    # inteiro na filial que o adotou.
    """
    CREATE TEMP TABLE mapa_categorias ON COMMIT DROP AS
    SELECT c.id                AS origem,
           gen_random_uuid()   AS copia,
           b.id                AS branch_id
      FROM categories c
      JOIN branches b
        ON b.restaurant_id = c.restaurant_id
       AND b.is_active IS TRUE
       AND b.id <> c.branch_id
    """,
    """
    INSERT INTO categories (
        id, restaurant_id, branch_id, name, slug, sort_order, is_active,
        created_at, updated_at
    )
    SELECT m.copia, c.restaurant_id, m.branch_id, c.name, c.slug,
           c.sort_order, c.is_active, c.created_at, now()
      FROM mapa_categorias m
      JOIN categories c ON c.id = m.origem
    """,
    # 2. Os produtos. O mapa sai do de CATEGORIAS: assim a copia do produto
    # cai na mesma filial que a copia da categoria dele, sem uma segunda
    # juncao com `branches` que pudesse discordar.
    """
    CREATE TEMP TABLE mapa_produtos ON COMMIT DROP AS
    SELECT p.id              AS origem,
           gen_random_uuid() AS copia,
           m.branch_id       AS branch_id,
           m.copia           AS category_copia
      FROM products p
      JOIN mapa_categorias m ON m.origem = p.category_id
    """,
    # O setor sai por SUBCONSULTA e nao por dois LEFT JOIN encadeados porque
    # ela se le como uma frase — "o setor de mesmo nome na filial de destino"
    # — enquanto a corrente de juncoes so faz sentido lida inteira.
    #
    # Ela nao pode devolver mais de uma linha: `uq_printing_sectors_branch_name`
    # (revisao 20260809_0011) ja garante um nome por filial. O `LIMIT 1` fica
    # como cinto, nao como regra.
    """
    INSERT INTO products (
        id, restaurant_id, branch_id, category_id, printing_sector_id,
        code, catalog_key, name, slug, description, price, image_path,
        is_active, is_available, sort_order, created_at, updated_at
    )
    SELECT mp.copia,
           p.restaurant_id,
           mp.branch_id,
           mp.category_copia,
           (
               SELECT destino.id
                 FROM printing_sectors destino
                 JOIN printing_sectors origem ON origem.id = p.printing_sector_id
                WHERE destino.branch_id = mp.branch_id
                  AND destino.name = origem.name
                ORDER BY destino.sort_order, destino.id
                LIMIT 1
           ),
           p.code, p.catalog_key, p.name, p.slug, p.description, p.price,
           p.image_path, p.is_active, p.is_available, p.sort_order,
           p.created_at, now()
      FROM mapa_produtos mp
      JOIN products p ON p.id = mp.origem
    """,
    # 3. Grupos de opcao e opcoes. Sem eles, a copia do produto aparece no
    # cardapio sem os complementos e o pedido dela e recusado por grupo
    # obrigatorio faltando.
    """
    CREATE TEMP TABLE mapa_grupos ON COMMIT DROP AS
    SELECT g.id              AS origem,
           gen_random_uuid() AS copia,
           mp.copia          AS product_copia
      FROM product_option_groups g
      JOIN mapa_produtos mp ON mp.origem = g.product_id
    """,
    """
    INSERT INTO product_option_groups (
        id, product_id, name, description, min_select, max_select,
        is_required, sort_order, is_active
    )
    SELECT mg.copia, mg.product_copia, g.name, g.description,
           g.min_select, g.max_select, g.is_required, g.sort_order, g.is_active
      FROM mapa_grupos mg
      JOIN product_option_groups g ON g.id = mg.origem
    """,
    """
    INSERT INTO product_options (
        id, option_group_id, name, description, additional_price,
        sort_order, is_active
    )
    SELECT gen_random_uuid(), mg.copia, o.name, o.description,
           o.additional_price, o.sort_order, o.is_active
      FROM mapa_grupos mg
      JOIN product_options o ON o.option_group_id = mg.origem
    """,
    # 4. O indice do Rapi. O vetor e REAPROVEITADO: mesmo conteudo indexado,
    # mesmo `content_hash`. `updated_at` mantem o carimbo da ORIGEM, menor
    # que o `updated_at` da copia recem-inserida — entao a varredura pega a
    # linha uma vez, confere o hash, corrige o metadata e nao gera embedding.
    #
    # Sem isto o Rapi ficaria cego na filial nova ate o worker rodar uma vez
    # por produto, pagando uma chamada a OpenAI em cada uma.
    """
    INSERT INTO ai_product_embeddings (
        restaurant_id, product_id, content, content_hash, metadata,
        embedding, updated_at
    )
    SELECT ape.restaurant_id, mp.copia, ape.content, ape.content_hash,
           ape.metadata, ape.embedding, ape.updated_at
      FROM mapa_produtos mp
      JOIN ai_product_embeddings ape ON ape.product_id = mp.origem
    """,
)


def upgrade() -> None:
    """A ORDEM DESTAS OITO LINHAS NAO E ARBITRARIA.

    Duas dependencias entre elas, e as duas quebram em producao e nao num
    banco vazio — que e o pior lugar para descobrir:

    **`_trocar_indices_de_slug` vem ANTES da copia.** A copia grava a segunda
    "Carnes" do mesmo restaurante, com o mesmo slug, numa filial diferente.
    Enquanto `uq_categories_restaurant_slug` estiver de pe, esse INSERT viola
    unicidade e o `alembic upgrade` morre. Num banco de teste vazio a copia e
    no-op e a ordem errada passa despercebida; no Junior, com duas lojas, ela
    derruba o deploy.

    **`_travar_not_null` e `_criar_amarras_de_filial` vem DEPOIS da copia.**
    As copias precisam existir para satisfazer o NOT NULL e as FKs compostas;
    criando as amarras antes, cada INSERT da copia passaria a ser validado
    contra elas sem necessidade.
    """
    _criar_colunas()
    _adotar_cardapio_existente()
    _recusar_cardapio_sem_filial()
    _semear_catalog_key()
    _trocar_indices_de_slug()
    _copiar_cardapio_para_as_outras_filiais()
    _travar_not_null()
    _criar_amarras_de_filial()


def downgrade() -> None:
    _recusar_downgrade_com_pedido_nas_copias()
    _remover_amarras_de_filial()
    _apagar_copias()
    _restaurar_indices_de_slug()

    op.drop_column("order_items", "catalog_key_snapshot")
    op.drop_column("products", "catalog_key")
    op.drop_column("products", "branch_id")
    op.drop_column("categories", "branch_id")


# --------------------------------------------------------------------------
# Subida
# --------------------------------------------------------------------------


def _criar_colunas() -> None:
    """As colunas nascem NULAVEIS. O NOT NULL vem depois de preencher."""
    op.add_column("categories", sa.Column("branch_id", sa.UUID(), nullable=True))
    op.add_column("products", sa.Column("branch_id", sa.UUID(), nullable=True))
    op.add_column("products", sa.Column("catalog_key", sa.Text(), nullable=True))
    op.add_column("order_items", sa.Column("catalog_key_snapshot", sa.Text(), nullable=True))


def _adotar_cardapio_existente() -> None:
    """A filial padrao fica com as linhas que ja existem, sem mover nada."""
    for tabela in ("categories", "products"):
        op.execute(
            f"""
            UPDATE {tabela}
               SET branch_id = adotante.branch_id
              FROM ({FILIAL_QUE_ADOTA}) AS adotante
             WHERE adotante.restaurant_id = {tabela}.restaurant_id
            """
        )


def _recusar_cardapio_sem_filial() -> None:
    """Para com mensagem util em vez de deixar o SET NOT NULL falhar.

    Restaurante com cardapio e sem NENHUMA filial cadastrada nao tem para
    onde ir. E defeito de cadastro, nao caso de operacao — e quem le o erro
    do `alembic upgrade` as 8h da manha precisa saber qual restaurante abrir.
    """
    conexao = op.get_bind()
    for tabela in ("categories", "products"):
        orfaos = conexao.execute(
            sa.text(
                f"""
                SELECT string_agg(DISTINCT r.slug, ', ')
                  FROM {tabela} t
                  JOIN restaurants r ON r.id = t.restaurant_id
                 WHERE t.branch_id IS NULL
                """
            )
        ).scalar()
        if orfaos:
            raise RuntimeError(
                f"Ha linhas em `{tabela}` cujo restaurante nao tem NENHUMA filial "
                f"cadastrada, nem ativa nem inativa: {orfaos}. Cardapio agora pende "
                "de filial. Cadastre uma filial para esses restaurantes e rode o "
                "`alembic upgrade head` de novo."
            )


def _semear_catalog_key() -> None:
    """Todo produto que ja existe nasce com chave, e as copias herdam a dele.

    O id de origem e a unica chave disponivel que ja distingue os produtos
    entre si sem ninguem digitar nada. Ela nao aparece em tela nenhuma; e
    identificador de relatorio.
    """
    op.execute("UPDATE products SET catalog_key = CAST(id AS text) WHERE catalog_key IS NULL")


def _copiar_cardapio_para_as_outras_filiais() -> None:
    """Cada filial ativa que nao adotou recebe uma copia completa do cardapio.

    O conteudo esta em `SQL_COPIA_O_CARDAPIO`, que documenta cada passo. Os
    mapas (origem -> copia) sao tabelas TEMP `ON COMMIT DROP`: a revisao
    inteira roda em uma transacao, entao elas somem sozinhas — inclusive
    quando algo falha no meio e o Postgres desfaz tudo.
    """
    for comando in SQL_COPIA_O_CARDAPIO:
        op.execute(comando)


def _travar_not_null() -> None:
    op.alter_column("categories", "branch_id", nullable=False)
    op.alter_column("products", "branch_id", nullable=False)


def _trocar_indices_de_slug() -> None:
    """O slug passa a ser unico DENTRO da filial.

    Roda ANTES da copia, e nao depois: a copia grava o mesmo slug numa outra
    filial do mesmo restaurante, e o indice antigo recusaria (ver `upgrade`).

    Mantendo a unicidade por restaurante, a picanha da segunda loja teria que
    se chamar `picanha-2` — e o slug e a URL publica do produto. O cliente
    leria no endereco que esta na "segunda" loja, e o lojista teria que
    inventar nome de produto para satisfazer um indice.
    """
    op.drop_index("uq_products_restaurant_slug", table_name="products")
    op.drop_index("uq_categories_restaurant_slug", table_name="categories")
    op.create_index(
        "uq_products_branch_slug", "products", ["branch_id", "slug"], unique=True
    )
    op.create_index(
        "uq_categories_branch_slug", "categories", ["branch_id", "slug"], unique=True
    )
    # Parcial: `catalog_key` nulo e o estado normal do produto que o lojista
    # criar depois e nao quiser ligar a nenhum par. Sem o WHERE, duas linhas
    # sem chave colidiriam.
    op.create_index(
        "uq_products_branch_catalog_key",
        "products",
        ["branch_id", "catalog_key"],
        unique=True,
        postgresql_where=sa.text("catalog_key IS NOT NULL"),
    )


def _criar_amarras_de_filial() -> None:
    """Os tres UNIQUE e as quatro FKs compostas. Ver o cabecalho da revisao."""
    op.create_unique_constraint("uq_branches_restaurant_id", "branches", ["restaurant_id", "id"])
    op.create_unique_constraint("uq_categories_branch_id", "categories", ["branch_id", "id"])
    op.create_unique_constraint(
        "uq_printing_sectors_branch_id", "printing_sectors", ["branch_id", "id"]
    )

    op.create_foreign_key(
        "fk_categories_branch_do_restaurante",
        "categories",
        "branches",
        ["restaurant_id", "branch_id"],
        ["restaurant_id", "id"],
    )
    op.create_foreign_key(
        "fk_products_branch_do_restaurante",
        "products",
        "branches",
        ["restaurant_id", "branch_id"],
        ["restaurant_id", "id"],
    )
    # ON DELETE CASCADE para casar com o `products_category_id_fkey` que ja
    # existia: sem ele, a FK nova bloquearia um DELETE de categoria que a
    # antiga cascateia, e as duas discordariam sobre o mesmo apagamento.
    op.create_foreign_key(
        "fk_products_categoria_da_mesma_filial",
        "products",
        "categories",
        ["branch_id", "category_id"],
        ["branch_id", "id"],
        ondelete="CASCADE",
    )

    # A de setor SUBSTITUI a antiga: manter as duas deixaria a checagem de
    # uma coluna so passando por cima, e e justamente ela que aceitava o
    # setor da loja errada.
    op.drop_constraint("products_printing_sector_id_fkey", "products", type_="foreignkey")
    op.create_foreign_key(
        "fk_products_setor_da_mesma_filial",
        "products",
        "printing_sectors",
        ["branch_id", "printing_sector_id"],
        ["branch_id", "id"],
    )


# --------------------------------------------------------------------------
# Descida
# --------------------------------------------------------------------------


def _recusar_downgrade_com_pedido_nas_copias() -> None:
    """Nao desce se houver pedido apontando para produto de filial copiada.

    O downgrade apaga esses produtos, e `order_items.product_id` os
    referencia por FK. Apagar quebraria o historico que o cliente ainda
    consulta pelo `tracking_token` — pior do que nao descer.
    """
    conexao = op.get_bind()
    presos = conexao.execute(
        sa.text(
            f"""
            SELECT count(*)
              FROM order_items oi
              JOIN products p ON p.id = oi.product_id
             WHERE p.branch_id IS DISTINCT FROM (
                       SELECT adotante.branch_id
                         FROM ({FILIAL_QUE_ADOTA}) AS adotante
                        WHERE adotante.restaurant_id = p.restaurant_id
                   )
            """
        )
    ).scalar()
    if presos:
        raise RuntimeError(
            f"{presos} itens de pedido apontam para produtos de filiais que este "
            "downgrade apagaria. Descer aqui quebraria o historico de pedido que o "
            "cliente ainda consulta. Resolva a mao antes: ou mova esses pedidos, ou "
            "aceite a perda apagando as linhas explicitamente."
        )


def _remover_amarras_de_filial() -> None:
    op.drop_constraint("fk_products_setor_da_mesma_filial", "products", type_="foreignkey")
    op.create_foreign_key(
        "products_printing_sector_id_fkey",
        "products",
        "printing_sectors",
        ["printing_sector_id"],
        ["id"],
    )
    op.drop_constraint("fk_products_categoria_da_mesma_filial", "products", type_="foreignkey")
    op.drop_constraint("fk_products_branch_do_restaurante", "products", type_="foreignkey")
    op.drop_constraint("fk_categories_branch_do_restaurante", "categories", type_="foreignkey")

    op.drop_constraint("uq_printing_sectors_branch_id", "printing_sectors", type_="unique")
    op.drop_constraint("uq_categories_branch_id", "categories", type_="unique")
    op.drop_constraint("uq_branches_restaurant_id", "branches", type_="unique")


def _apagar_copias() -> None:
    """Deixa so o cardapio da filial padrao. Perde as outras lojas.

    De baixo para cima (opcao -> grupo -> produto -> categoria) porque as FKs
    ainda valem durante o DELETE.
    """
    copia = f"""
        SELECT p.id
          FROM products p
         WHERE p.branch_id IS DISTINCT FROM (
                   SELECT adotante.branch_id
                     FROM ({FILIAL_QUE_ADOTA}) AS adotante
                    WHERE adotante.restaurant_id = p.restaurant_id
               )
    """
    op.execute(
        f"""
        DELETE FROM product_options
         WHERE option_group_id IN (
               SELECT id FROM product_option_groups WHERE product_id IN ({copia})
         )
        """
    )
    op.execute(f"DELETE FROM product_option_groups WHERE product_id IN ({copia})")
    op.execute(f"DELETE FROM ai_product_embeddings WHERE product_id IN ({copia})")
    op.execute(f"DELETE FROM products p WHERE p.id IN ({copia})")
    op.execute(
        f"""
        DELETE FROM categories c
         WHERE c.branch_id IS DISTINCT FROM (
                   SELECT adotante.branch_id
                     FROM ({FILIAL_QUE_ADOTA}) AS adotante
                    WHERE adotante.restaurant_id = c.restaurant_id
               )
        """
    )


def _restaurar_indices_de_slug() -> None:
    op.drop_index("uq_products_branch_catalog_key", table_name="products")
    op.drop_index("uq_categories_branch_slug", table_name="categories")
    op.drop_index("uq_products_branch_slug", table_name="products")
    op.create_index(
        "uq_products_restaurant_slug", "products", ["restaurant_id", "slug"], unique=True
    )
    op.create_index(
        "uq_categories_restaurant_slug", "categories", ["restaurant_id", "slug"], unique=True
    )
