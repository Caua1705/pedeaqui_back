"""Os dois models cuja armadilha so nao disparou porque ninguem os construiu.

`AIProductEmbedding.content` e `CouponTemplate.image_path` sao a intersecao
mais perigosa do levantamento de `scripts/divergencias_orm_schema.py`:

    o banco diz NOT NULL  +  a coluna NAO tem DEFAULT  +  o model diz nullable

Nessa combinacao, `Modelo(...)` **sem** aquela coluna passa no type checker,
passa no `flush()` do SQLAlchemy — que nem manda a coluna, porque a anotacao
diz que ela e opcional — e estoura `IntegrityError` no INSERT, em runtime.
Nenhum teste veria isso antes de acontecer.

**Hoje nao acontece, e por um motivo fragil: nada instancia os dois.** O indice
vetorial e escrito por `INSERT` cru em `AIRepository` (o tipo `vector` do
pgvector nao passa pelo ORM), e `coupon_templates` nao tem rota de escrita — o
painel escolhe uma arte que ja existe, nunca cria. As duas ausencias sao
circunstancia, nao decisao: a primeira linha de codigo que escrever
`CouponTemplate(name=..., discount_type=...)` — que e o que um seeder de arte
nova pareceria — leva o defeito para producao.

Este arquivo e a trava. Ele nao proibe instanciar: ele obriga quem instanciar a
ler a razao e passar a coluna. Se voce chegou aqui por causa de um vermelho,
a correcao esta na mensagem do `assert`.
"""

import ast
from pathlib import Path

import pytest

from tests.fabricas_db import criar_categoria, criar_produto, criar_restaurante


RAIZ = Path(__file__).resolve().parent.parent

# Os diretorios de codigo de aplicacao. `tests/` fica de FORA de proposito: e
# aqui que os dois models PODEM ser construidos, inclusive logo abaixo, e um
# teste que se proibisse a si mesmo nao poderia provar nada.
DIRETORIOS = ("src", "scripts")

# Nome do model -> a coluna que o banco exige e o model deixa parecer opcional.
MODELS_VIGIADOS = {
    "AIProductEmbedding": "content",
    "CouponTemplate": "image_path",
}


def _instanciacoes(caminho: Path) -> list[tuple[str, int, str]]:
    """Onde o arquivo CHAMA um dos models vigiados.

    AST e nao grep porque `AIProductEmbedding.content_hash` (um atributo, usado
    de verdade em `AIRepository`) e `AIProductEmbedding(...)` sao a mesma
    string para o grep e coisas opostas para o assunto aqui. `ast.Call` so casa
    com a chamada.
    """
    arvore = ast.parse(caminho.read_text(encoding="utf-8"), filename=str(caminho))
    achados = []
    for no in ast.walk(arvore):
        if not isinstance(no, ast.Call):
            continue
        alvo = no.func
        nome = alvo.id if isinstance(alvo, ast.Name) else getattr(alvo, "attr", None)
        if nome in MODELS_VIGIADOS:
            achados.append((str(caminho.relative_to(RAIZ)).replace("\\", "/"), no.lineno, nome))
    return achados


@pytest.mark.parametrize("model, coluna_obrigatoria", sorted(MODELS_VIGIADOS.items()))
def test_o_model_continua_sem_ser_instanciado_na_aplicacao(model, coluna_obrigatoria):
    achados = [
        achado
        for diretorio in DIRETORIOS
        for arquivo in (RAIZ / diretorio).rglob("*.py")
        if "__pycache__" not in arquivo.parts
        for achado in _instanciacoes(arquivo)
        if achado[2] == model
    ]

    assert not achados, (
        f"{model}(...) passou a ser construido em:\n"
        + "\n".join(f"    {arquivo}:{linha}" for arquivo, linha, _ in achados)
        + f"\n\nIsto NAO e proibido — mas `{model.lower()}.{coluna_obrigatoria}` e NOT NULL no\n"
        f"banco e NAO tem DEFAULT, enquanto o model a declara opcional. Um\n"
        f"`{model}(...)` sem `{coluna_obrigatoria}=` passa no type checker e estoura\n"
        "IntegrityError no INSERT, em producao, sem nenhum teste ter visto.\n\n"
        "O que fazer, nesta ordem:\n"
        f"  1. passe `{coluna_obrigatoria}=` explicitamente em toda construcao nova;\n"
        f"  2. corrija o model para `nullable=False` (a anotacao esta mentindo);\n"
        "  3. so entao apague este teste, dizendo no commit que a trava saiu\n"
        "     porque o motivo dela deixou de existir.\n\n"
        "Ver `scripts/divergencias_orm_schema.py` e a secao 1 de\n"
        "`scratchpad/rodada-back-2.md`."
    )


@pytest.mark.db
def test_embedding_sem_content_estoura_no_insert(db):
    """O que a trava acima esta evitando, mostrado uma vez.

    Aqui o `content` e omitido exatamente como um `AIProductEmbedding(...)`
    distraido o omitiria. O model diz `Mapped[str | None]`, entao o SQLAlchemy
    nem manda a coluna no INSERT — e quem recusa e o banco.

    Contra o Postgres de verdade e nao com dublê porque o ponto INTEIRO deste
    teste e que a regra mora no DDL e nao no ORM. Um dublê do banco
    responderia o que o ORM acha, que e justamente o lado errado.
    """
    from sqlalchemy.exc import IntegrityError

    from src.models.ai_product_embedding_model import AIProductEmbedding

    restaurante = criar_restaurante(db)
    produto = criar_produto(db, restaurante, criar_categoria(db, restaurante))

    db.add(
        AIProductEmbedding(
            restaurant_id=restaurante.id,
            product_id=produto.id,
            embedding="[" + ",".join(["0"] * 1536) + "]",
        )
    )

    with pytest.raises(IntegrityError) as erro:
        db.flush()

    assert "content" in str(erro.value)
