"""A tabela de serviços de `docs/operacao.md` bate com o `docker-compose.yml`.

**Container que não está na tabela é container que ninguém confere.** É o achado
§9.1 da auditoria de 05/09: o documento dizia "cinco serviços" e listava cinco;
são seis desde 04/09/2026, e o que faltava era o `whatsapp-reenvio`. A linha do
`estorno` também estava incompleta — o compose roda dois scripts encadeados e a
tabela citava um.

O documento diz, uma seção acima da tabela, que *"quando um deles morre, nada na
API muda de aparência — o sintoma é indireto"*. É por isso que a lista existe: se
o `whatsapp-reenvio` cair, o sintoma é aviso de pedido que não chega ao cliente,
e quem for procurar a causa às duas da manhã lê a tabela, não o
`docker-compose.yml`.

## Por que isto é teste e não revisão

A tabela já estava desatualizada por dois dias quando a auditoria a encontrou, e
ela nasceu correta. Enquanto manter as duas coisas juntas depender de alguém
lembrar, é uma recomendação — a armadilha 46, aplicada a documento. Um serviço
novo no compose agora deixa a suíte vermelha no mesmo commit que o cria.

**O que este teste NÃO faz é conferir a cadência nem os scripts**, e a omissão é
deliberada: derivar isso do `sh -c` do compose transformaria o documento numa
segunda cópia gerada, e um documento gerado deixa de poder EXPLICAR — que é a
única coisa que ele faz melhor que `docker compose config`. A cobertura aqui é a
mesma de `tests/test_diagrama_er_cobre_o_schema.py`: nomes, nos dois sentidos.
"""

import re
from pathlib import Path

import yaml


RAIZ = Path(__file__).resolve().parents[1]
COMPOSE = RAIZ / "docker-compose.yml"
DOCUMENTO = RAIZ / "docs" / "operacao.md"

#: A linha da tabela é `| `nome` | ... | ... |`. O nome vem em crase na
#: primeira coluna — casar a linha inteira evita pegar o mesmo nome citado na
#: prosa em volta, que é texto em português e fala dos serviços o tempo todo.
LINHA_DA_TABELA = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|", re.M)

#: Escrito por extenso no documento ("São **seis** serviços"), e conferido
#: contra a contagem real. Sem isto, acrescentar a linha e esquecer a palavra
#: deixaria o documento se contradizendo dentro do mesmo parágrafo.
NUMERO_POR_EXTENSO = {
    4: "quatro",
    5: "cinco",
    6: "seis",
    7: "sete",
    8: "oito",
    9: "nove",
    10: "dez",
}


def _servicos_do_compose() -> set[str]:
    return set(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])


def _servicos_da_tabela() -> set[str]:
    return set(LINHA_DA_TABELA.findall(DOCUMENTO.read_text(encoding="utf-8")))


def test_o_parser_esta_lendo_as_duas_coisas():
    """Sem isto, um regex que parasse de casar deixaria os dois testes abaixo
    verdes por vacuidade — "nenhum serviço faltando" porque nenhum foi lido.

    É a mesma guarda de `test_diagrama_er_cobre_o_schema.py`, e ela existe
    porque o modo de falha de um varredor é ficar cego, não ficar errado.
    """
    assert len(_servicos_do_compose()) >= 5
    assert len(_servicos_da_tabela()) >= 5
    assert "pedeaqui-api" in _servicos_da_tabela()


def test_todo_servico_do_compose_esta_na_tabela():
    faltando = sorted(_servicos_do_compose() - _servicos_da_tabela())

    assert not faltando, (
        f"estes serviços rodam em produção e não estão na tabela de "
        f"docs/operacao.md: {', '.join(faltando)}. Container fora da tabela é "
        "container que ninguém confere quando morre."
    )


def test_a_tabela_nao_lista_servico_que_nao_existe():
    """A direção contrária, e é a mais confusa das duas: ela manda procurar um
    container que não está lá."""
    inventados = sorted(_servicos_da_tabela() - _servicos_do_compose())

    assert not inventados, (
        f"estes nomes estão na tabela de docs/operacao.md e não existem no "
        f"docker-compose.yml: {', '.join(inventados)}"
    )


def test_o_numero_por_extenso_bate_com_a_contagem():
    quantos = len(_servicos_do_compose())
    esperado = NUMERO_POR_EXTENSO[quantos]
    texto = DOCUMENTO.read_text(encoding="utf-8")

    assert f"São **{esperado}** serviços" in texto, (
        f"o compose tem {quantos} serviços e o documento não diz "
        f'"São **{esperado}** serviços".'
    )
