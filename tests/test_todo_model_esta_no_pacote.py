"""Todo model de `src/models/` tem que ser alcancavel por `import src.models`.

O QUE ISTO EXISTE PARA IMPEDIR, e ja tinha acontecido: `DeliveryEstimate`
estava em `src/models/delivery_estimate_model.py`, era um model de verdade,
mapeava a tabela de verdade — e nao era importado pelo `__init__.py`.

Nada quebrava. O app continuava funcionando porque o repositorio importa o
modulo direto, e a suite inteira ficava verde. O estrago mora num lugar so:
`alembic/env.py` monta o `target_metadata` a partir de `import src.models`, e
so dele. Para o autogenerate, `delivery_estimates` NAO EXISTIA no ORM — e o
autogenerate propoe `DROP TABLE` em tudo que nao acha no ORM (armadilha 24).

Quer dizer: um `alembic revision --autogenerate` traria um `DROP TABLE
delivery_estimates` no meio do arquivo, e a unica defesa seria alguem ler o
diff com atencao as duas da manha. A regra do repositorio ("leia o arquivo
gerado e apague as linhas de drop") continua valendo; o que este teste faz e
tirar UMA armadilha da lista de coisas que essa leitura precisa pegar.

POR QUE A CONFERENCIA E POR ARQUIVO, e nao por `Base.metadata`. Dentro do
pytest, dezenas de modulos de `src` ja foram importados por outros testes, e
`Base.metadata` estaria completo de qualquer jeito — o teste passaria verde
justamente no caso que ele veio pegar. Aqui a pergunta e outra: cada arquivo
de model e lido, as classes dele sao coletadas, e o que se confere e se o
`__init__.py` as reexporta.
"""

import importlib
from pathlib import Path

from src.db.base import Base


RAIZ_DOS_MODELS = Path(__file__).resolve().parents[1] / "src" / "models"


def _classes_de_model_do_arquivo(caminho: Path) -> list[str]:
    """Os models declarados NAQUELE arquivo, pelo nome da classe.

    `__module__` filtra o que o arquivo apenas importou: quase todo model
    importa outros para as FKs, e sem esse filtro cada arquivo pareceria
    declarar meia dezena de classes que nao sao dele.
    """
    modulo = importlib.import_module(f"src.models.{caminho.stem}")
    return [
        nome
        for nome, objeto in vars(modulo).items()
        if isinstance(objeto, type)
        and issubclass(objeto, Base)
        and objeto is not Base
        and objeto.__module__ == modulo.__name__
    ]


def test_o_pacote_reexporta_todo_model():
    import src.models

    faltando: list[str] = []
    for caminho in sorted(RAIZ_DOS_MODELS.glob("*_model.py")):
        for classe in _classes_de_model_do_arquivo(caminho):
            if not hasattr(src.models, classe):
                faltando.append(f"{classe} ({caminho.name})")

    assert not faltando, (
        "estes models nao sao importados por src/models/__init__.py, entao nao "
        "existem para o `target_metadata` do alembic e o autogenerate proporia "
        f"DROP TABLE neles: {', '.join(faltando)}"
    )


def test_a_lista___all___acompanha_os_imports():
    """`__all__` desatualizado nao quebra o alembic, mas mente para quem le.

    Ele e a lista que responde "quais models existem?" sem abrir a pasta, e a
    resposta errada aqui e o comeco do erro que o teste acima pega.
    """
    import src.models

    importados = {
        nome
        for nome, objeto in vars(src.models).items()
        if isinstance(objeto, type) and issubclass(objeto, Base) and objeto is not Base
    }
    assert importados == set(src.models.__all__)
