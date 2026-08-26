"""Os diagramas de `docs/modelo-de-dados.md` cobrem todas as tabelas do ORM.

DIAGRAMA DESATUALIZADO NAO PARECE DESATUALIZADO. Ele continua bonito, continua
correto no que mostra, e o que falta nele e invisivel — ninguem olha um mapa e
percebe a rua que nao foi desenhada. O jeito de descobrir e alguem procurar uma
tabela ali, nao achar, e concluir que ela nao existe.

E o custo disso nao e teorico: metade das armadilhas deste repositorio e sobre
uma tabela pender de filial e nao de restaurante. Um diagrama que perde a
tabela nova perde exatamente essa informacao.

O teste NAO confere as cardinalidades — `||--o{` contra `|o--o{` — e isso e
deliberado: derivar isso do `nullable` das FKs transformaria o documento numa
segunda cópia do ORM, gerada, e um diagrama gerado deixa de poder AGRUPAR e
EXPLICAR, que e a unica coisa que ele faz melhor que `\\d` no psql. O que se
trava aqui e a cobertura: toda tabela aparece em algum diagrama.
"""

import re
from pathlib import Path

import src.models  # noqa: F401  (registra os models no Base)
from src.db.base import Base


DOCUMENTO = Path(__file__).resolve().parents[1] / "docs" / "modelo-de-dados.md"

BLOCO_MERMAID = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def _tabelas_desenhadas() -> set[str]:
    """Os nomes que aparecem como ponta de relacao em algum `erDiagram`.

    A linha de relacao do Mermaid e `esquerda <cardinalidade> direita : rotulo`.
    Ler so as duas pontas evita casar com palavra solta do rotulo — que e
    texto em portugues e tem nome de tabela dentro de vez em quando.
    """
    relacao = re.compile(r"^\s*(\w+)\s+[|}o][|o{}-]*[|o{]\s*--\s*[|o{}-]*[|o{]\s+(\w+)\s*:")
    nomes: set[str] = set()
    for bloco in BLOCO_MERMAID.findall(DOCUMENTO.read_text(encoding="utf-8")):
        for linha in bloco.splitlines():
            casou = relacao.match(linha)
            if casou:
                nomes.update(casou.groups())
    return nomes


def test_o_regex_esta_lendo_os_diagramas():
    """Sem isto, um regex que parasse de casar deixaria o teste abaixo verde.

    Um `_tabelas_desenhadas()` vazio faria "toda tabela do ORM esta em
    nenhum diagrama" virar "nao ha nada faltando", que e o modo de falha
    silencioso que este arquivo inteiro existe para nao ter.
    """
    desenhadas = _tabelas_desenhadas()
    assert len(desenhadas) > 30, f"o parser leu so {len(desenhadas)} tabelas dos diagramas"
    assert "orders" in desenhadas


def test_toda_tabela_do_orm_aparece_em_algum_diagrama():
    faltando = sorted(set(Base.metadata.tables) - _tabelas_desenhadas())
    assert not faltando, (
        "estas tabelas existem no ORM e nao aparecem em nenhum diagrama de "
        f"docs/modelo-de-dados.md: {', '.join(faltando)}"
    )


def test_nenhum_diagrama_desenha_tabela_que_nao_existe():
    """A direcao contraria: tabela removida por revisao que ficou no desenho.

    E o erro mais confuso dos dois, porque manda procurar uma coisa que nao
    esta la. Ja aconteceu no repositorio com `menu_events`, que nasceu e foi
    removida em duas revisoes de agosto de 2026.
    """
    inventadas = sorted(_tabelas_desenhadas() - set(Base.metadata.tables))
    assert not inventadas, (
        "estes nomes estao desenhados em docs/modelo-de-dados.md e nao existem "
        f"no ORM: {', '.join(inventadas)}"
    )
