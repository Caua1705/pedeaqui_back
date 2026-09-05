"""A revisão `20260905_0058` aplicada: o que saiu de `branches` e o que ficou.

Enquanto ela morava em `alembic/preparadas/`, quem a cobrava era
`test_revisoes_preparadas_de_branches.py` — rodando a migração à mão dentro de
uma transação que voltava. Ela entrou na cadeia em 05/09/2026, e a pergunta
mudou: não é mais "esta revisão ainda descreve o schema?", é "o schema depois
do `upgrade head` é o que ela prometeu?".

O schema que estes testes leem é o da fixture de sessão — baseline, `stamp` e
`upgrade head` (ver `tests/conftest.py`), que é a mesma cadeia que o
`docker-entrypoint.sh` roda em produção.

## O que se cobra, e por que cada metade importa

**As cinco sumiram.** `address_street`, `address_neighborhood`,
`address_city`, `address_state` e `address_zipcode` eram resto do schema
pré-Alembic. Ninguém escrevia nelas, e até a correção do `_build_address` elas
eram LIDAS PRIMEIRO — venciam o conjunto vivo. O efeito numa filial com elas
preenchidas: o lojista corrigia o endereço no painel, o painel exibia o valor
novo (ele lê `address`) e o app do cliente continuava mostrando o antigo, sem
erro e sem log.

**`address_number` FICOU**, e esta é a metade que ninguém lembra de testar. Ela
não tem par vivo: não existe `branches.number` nem campo de número no painel,
então é a única fonte do número da casa. Uma versão futura desta revisão que
derrubasse as seis passaria em qualquer teste que só olhasse as cinco — e
apagaria o número do endereço público de toda filial que o tenha preenchido,
sem nada para pôr no lugar.

**O ORM concorda com o banco.** É a armadilha 50 pelo lado bom: `nullable=` do
model nunca vira DDL, e model mapeando coluna que não existe mais não é um erro
de tipo — é `UndefinedColumn` em **todo `SELECT` de filial**, que é a rota do
cardápio inteira.
"""

import pytest
from sqlalchemy import inspect

from src.models.branch_model import Branch


pytestmark = pytest.mark.db


#: O que a revisão derrubou. Repetido aqui de propósito, e não importado da
#: revisão: o teste tem que falhar se alguém editar a lista da revisão, e um
#: import faria as duas mudarem juntas em silêncio.
COLUNAS_QUE_SAIRAM = (
    "address_street",
    "address_neighborhood",
    "address_city",
    "address_state",
    "address_zipcode",
)

#: O conjunto VIVO — o que `AdminBranchUpdate` grava e `_build_address` lê.
#: Não era assunto da revisão, e está aqui para que uma versão futura dela que
#: os alcançasse caia neste arquivo.
CONJUNTO_VIVO = ("address", "neighborhood", "city", "state", "zipcode")


def _colunas_do_banco(db) -> set[str]:
    return {info["name"] for info in inspect(db.connection()).get_columns("branches")}


class TestOBanco:
    def test_as_cinco_sairam(self, db):
        sobrando = sorted(set(COLUNAS_QUE_SAIRAM) & _colunas_do_banco(db))
        assert sobrando == [], f"a 0058 não derrubou: {sobrando}"

    def test_address_number_ficou(self, db):
        assert "address_number" in _colunas_do_banco(db), (
            "`address_number` é a exceção da 0058 e a única fonte do número da "
            "casa. Tirá-la exige antes decidir onde o número passa a ser escrito."
        )

    def test_o_conjunto_vivo_ficou_inteiro(self, db):
        faltando = sorted(set(CONJUNTO_VIVO) - _colunas_do_banco(db))
        assert faltando == [], f"o endereço que o painel grava perdeu: {faltando}"


class TestOModel:
    def test_o_model_nao_mapeia_mais_as_cinco(self):
        """Model mapeando coluna que saiu do banco quebra TODO `SELECT` de
        filial — não é erro de tipo, é `UndefinedColumn` em runtime."""
        mapeadas = set(Branch.__table__.columns.keys())
        assert set(COLUNAS_QUE_SAIRAM) & mapeadas == set()

    def test_o_model_continua_mapeando_address_number(self):
        assert "address_number" in Branch.__table__.columns.keys()

    def test_o_model_e_o_banco_concordam_sobre_branches(self, db):
        """A prova das duas metades juntas.

        Os dois testes acima passariam com o model certo e o banco errado (ou
        o contrário). Este é o que amarra: coluna que o ORM mapeia e o banco
        não tem é `UndefinedColumn`; e o outro sentido é a armadilha 50, que
        tem varredura própria (`scripts/divergencias_orm_schema.py`) — aqui
        só se cobra a tabela que esta revisão mexeu.
        """
        mapeadas = set(Branch.__table__.columns.keys())
        no_banco = _colunas_do_banco(db)

        assert mapeadas - no_banco == set(), "o ORM mapeia coluna que o banco não tem"
