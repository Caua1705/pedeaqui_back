"""A suite nao volta a dublar DADO com objeto de atributos livres.

Este arquivo existe porque a classe que ele vigia foi a que deixou passar os
piores defeitos deste repositorio, e ela volta sozinha: dublar uma filial com
`SimpleNamespace` e mais rapido do que construir uma, e a diferenca so aparece
meses depois, em producao, quando alguem acrescenta uma coluna.

O levantamento que zerou isto achou **141** dublês de dado. Nenhum era
malicioso; cada um era o caminho curto de alguem com pressa. O que os
denunciou foi mecanico, e por isso pode continuar rodando.

`scripts/dubles_de_dado.py` tem o metodo escrito por extenso — o que ele
compara, por que o limiar e 0.6, e por que `SimpleNamespace` continua CERTO
para dublar colaborador.
"""

from pathlib import Path

from scripts.dubles_de_dado import LIMIAR_PADRAO, achados, tipos_do_repositorio


RAIZ = Path(__file__).resolve().parent.parent


def test_nenhum_simplenamespace_dubla_model_schema_ou_dataclass():
    encontrados = achados(RAIZ / "tests", tipos_do_repositorio(), LIMIAR_PADRAO)

    assert not encontrados, (
        "Dublê de DADO na suite. Um `SimpleNamespace` no lugar de um tipo "
        "deste repositorio responde qualquer atributo que o teste escrever e "
        "nenhum que o teste esquecer — e fica verde descrevendo um objeto que "
        "a aplicacao nunca produz (CLAUDE.md).\n\n"
        + "\n".join(
            f"    {arquivo}:{linha}\n"
            f"        parece {tipo} ({pontuacao:.0%} das chaves): {', '.join(chaves)}"
            for arquivo, linha, tipo, pontuacao, chaves in encontrados
        )
        + "\n\nConstrua o tipo real. Para os do dominio ha `tests/fabricas.py` "
        "(transiente, sem banco); para os de banco, `tests/fabricas_db.py`.\n"
        "Se este achado for FALSO POSITIVO — o objeto nao e daquele tipo e o "
        "casamento foi coincidencia —, o conserto e continuar construindo o "
        "tipo certo, nao afrouxar o limiar."
    )


def test_o_varredor_enxerga_os_tipos_dos_tres_lugares():
    """Se ele parasse de enxergar, o teste acima passaria por vacuidade.

    Suite verde que nao verifica nada e pior que suite vermelha, e este e o
    modo de falha real deste varredor: um `ImportError` novo em `src/` o faria
    perder tipos em silencio (`walk_packages` engole excecao de import de
    proposito, para um modulo quebrado nao derrubar a varredura inteira).

    Os tres nomes escolhidos sao um de cada origem, e cada um so existe se a
    origem dele foi lida.
    """
    tipos = tipos_do_repositorio()

    assert "model:branches" in tipos, "as tabelas do Base.metadata sumiram"
    assert "schema:ProductResponse" in tipos, "os schemas Pydantic sumiram"
    assert "dataclass:DeliveryEstimateResult" in tipos, "as dataclasses sumiram"

    # E que os campos vieram junto, e nao um conjunto vazio que casaria com
    # tudo (ou com nada, conforme o lado da conta).
    assert "serves_people" in tipos["schema:ProductResponse"]
    assert "latitude" in tipos["dataclass:DeliveryEstimateResult"]
    assert "option_groups" in tipos["model:products"], (
        "os `relationship` do model precisam entrar: `option_groups` nao e "
        "coluna, e um dublê legitimo de Product o passa"
    )


def test_o_varredor_acusa_um_duble_de_dado_plantado(tmp_path):
    """A prova de que o vermelho existe.

    Sem isto, `test_nenhum_simplenamespace_dubla_model_schema_ou_dataclass`
    poderia estar verde porque o varredor nao acha NADA, nunca — e ninguem
    saberia a diferenca.
    """
    plantado = tmp_path / "test_plantado.py"
    plantado.write_text(
        "from types import SimpleNamespace\n"
        "def test_x():\n"
        "    filial = SimpleNamespace(\n"
        "        id=1, restaurant_id=2, name='Matriz', slug='matriz',\n"
        "        is_open=True, accepts_delivery=True, accepts_pickup=True,\n"
        "    )\n"
        "    assert filial.is_open\n",
        encoding="utf-8",
    )

    encontrados = achados(tmp_path, tipos_do_repositorio(), LIMIAR_PADRAO)

    assert len(encontrados) == 1
    assert encontrados[0][2] == "model:branches"


def test_o_varredor_deixa_passar_duble_de_colaborador(tmp_path):
    """O outro lado, que importa tanto quanto: `SimpleNamespace` continua certo.

    Um repositorio dublado nao tem contrato NOSSO a respeitar — quem o define
    e a chamada que o teste faz. Se este teste virar vermelho, o varredor
    passou a cobrar tipo real de colaborador, e ai a regra deixaria de ser
    seguivel.
    """
    plantado = tmp_path / "test_plantado.py"
    plantado.write_text(
        "from types import SimpleNamespace\n"
        "def test_x():\n"
        "    repositorio = SimpleNamespace(\n"
        "        get_active_by_id_and_restaurant=lambda a, b: None,\n"
        "        list_enabled_payment_methods=lambda a: [],\n"
        "    )\n"
        "    assert repositorio.list_enabled_payment_methods(1) == []\n",
        encoding="utf-8",
    )

    assert achados(tmp_path, tipos_do_repositorio(), LIMIAR_PADRAO) == []
