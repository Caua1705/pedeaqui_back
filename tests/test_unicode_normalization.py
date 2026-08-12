"""Uma forma Unicode so, da escrita ate a busca.

"Filé" tem duas representacoes legitimas: composta (`é`, um code point) e
decomposta (`e` + acento combinante, dois). Sao identicas na tela do painel e
sao bytes diferentes para o Postgres, que compara `=`, `LIKE` e `ILIKE` byte a
byte. Conferido no banco de producao (PG 17.6):

    'Filé'(NFC) = 'Filé'(NFD)        -> false
    'Filé'(NFD) ILIKE '%Filé%'(NFC)  -> false

O estrago, se as duas formas convivessem: o lojista digita "Filé" na busca do
cardapio, o produto gravado decomposto nao aparece, ele conclui que nao esta
cadastrado e cadastra de novo. Ninguem ve erro nenhum.

A defesa tem DOIS lados, e um sozinho nao resolve:

1. **Escrita em NFC** — mantem a base inteira numa forma so.
2. **Termo de busca em NFC** — faz a consulta casar com o que foi gravado.

E o `slugify` fica de fora de proposito: ele ja e imune (NFKD + ascii ignore
colapsa as duas formas), e este arquivo trava essa imunidade para ninguem
trocar o NFKD por NFC e mudar URLs do cardapio publico sem querer.
"""

import unicodedata
from uuid import uuid4

import pytest

from src.schemas.admin_menu_schema import (
    AdminCategoryCreate,
    AdminProductCreate,
    AdminProductUpdate,
)
from src.schemas.admin_printing_schema import PrintingSectorCreate
from src.utils.normalization import normalize_text, slugify


# Itens reais do cardapio do Júnior da Picanha, nas duas formas.
CARDAPIO = ["Picanha à Moda", "Filé à Parmegiana", "Sortidão", "Coração de Frango"]


def nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


class TestFixtureSanity:
    """Se as duas formas fossem iguais, todo teste abaixo passaria a toa."""

    @pytest.mark.parametrize("nome", CARDAPIO)
    def test_the_two_forms_really_are_different_strings(self, nome):
        assert nfd(nome) != nome
        assert nfd(nome).encode("utf-8") != nome.encode("utf-8")
        # ...e representam o mesmo texto para um humano.
        assert unicodedata.normalize("NFC", nfd(nome)) == nome


class TestNormalizeText:
    @pytest.mark.parametrize("nome", CARDAPIO)
    def test_it_composes_and_strips(self, nome):
        assert normalize_text(f"  {nfd(nome)}  ") == nome

    def test_it_is_idempotent(self):
        once = normalize_text(nfd("Filé"))
        assert normalize_text(once) == once


class TestWritePathKeepsTheBaseInOneForm:
    """O que entra pelo painel e gravado composto, venha como vier."""

    @pytest.mark.parametrize("nome", CARDAPIO)
    def test_a_product_name_is_stored_composed(self, nome):
        product = AdminProductCreate(name=nfd(nome), price=10, category_id=uuid4())

        assert product.name == nome

    @pytest.mark.parametrize("nome", CARDAPIO)
    def test_a_partial_update_normalizes_too(self, nome):
        # O UPDATE e o caminho que traz nome novo para um produto que ja
        # existe: se ele nao normalizar, a base volta a ter as duas formas.
        assert AdminProductUpdate(name=nfd(nome)).name == nome

    def test_an_update_without_a_name_stays_none(self):
        assert AdminProductUpdate(sort_order=3).name is None

    def test_a_category_name_is_stored_composed(self):
        assert AdminCategoryCreate(name=nfd("Porções")).name == "Porções"

    def test_a_printing_sector_name_is_stored_composed(self):
        # Nome de setor vai impresso na comanda e escolhe a impressora do
        # agente. Ver print-agent/print_agent/escpos.py.
        assert PrintingSectorCreate(name=nfd("Praça Quente")).name == "Praça Quente"


class TestSearchTermMatchesWhatWasStored:
    """O termo digitado vira a mesma forma que o banco guarda."""

    @pytest.mark.parametrize("nome", CARDAPIO)
    def test_the_product_search_term_is_composed(self, nome):
        from src.repositories.admin_menu_repository import _build_product_search_condition

        # O que interessa e o valor que vai para o SQL: ele precisa ser o
        # composto, que e a forma em que os nomes estao gravados.
        condition = _build_product_search_condition(nfd(nome))
        literal = str(condition.compile(compile_kwargs={"literal_binds": True}))

        assert nome in literal
        assert nfd(nome) not in literal

    def test_the_order_search_term_is_composed(self):
        from src.repositories.order_repository import _build_search_condition

        condition = _build_search_condition(nfd("Antônio"))
        literal = str(condition.compile(compile_kwargs={"literal_binds": True}))

        assert "Antônio" in literal

    def test_the_customer_search_term_is_composed(self):
        from src.repositories.admin_customer_repository import (
            _build_customer_search_condition,
        )

        condition = _build_customer_search_condition(nfd("Antônio"))
        literal = str(condition.compile(compile_kwargs={"literal_binds": True}))

        assert "Antônio" in literal

    def test_a_digits_only_search_still_goes_to_order_number(self):
        # A normalizacao nao pode ter estragado o ramo do numero do pedido.
        from src.repositories.order_repository import _build_search_condition

        literal = str(
            _build_search_condition(" 5471 ").compile(
                compile_kwargs={"literal_binds": True}
            )
        )

        assert "5471" in literal
        assert "ILIKE" not in literal.upper()

    def test_wildcards_are_still_escaped_after_normalizing(self):
        # A normalizacao entrou ANTES do escape; se tivesse entrado depois,
        # um "%" digitado voltaria a listar a base inteira.
        from src.repositories.admin_menu_repository import _build_product_search_condition

        literal = str(
            _build_product_search_condition("100%").compile(
                compile_kwargs={"literal_binds": True}
            )
        )

        assert "100\\%" in literal


class TestSlugifyStaysFormIndependent:
    """O slug vira URL publica: ele nao pode depender da forma da entrada."""

    @pytest.mark.parametrize("nome", CARDAPIO)
    def test_both_forms_produce_the_same_slug(self, nome):
        assert slugify(nome) == slugify(nfd(nome))

    def test_the_slug_is_plain_ascii(self):
        assert slugify("Filé à Parmegiana") == "file-a-parmegiana"
