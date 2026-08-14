"""Nenhum teste consulta relatorio com data escrita a mao.

## O defeito que este arquivo existe para nao deixar voltar

`test_auditoria_papeis.py` tinha:

    PERIODO = "start_date=2026-06-01&end_date=2026-08-12"

As fabricas criam pedido sem `created_at`, entao ele nasce com o `now()` do
banco. No dia 14/08 o pedido passou a cair FORA da janela, o relatorio voltou
`orders_count: 0` e o teste quebrou.

**Quebrar foi a sorte.** O mesmo literal estava em
`test_auditoria_isolamento_e2e.py`, e la ele nao quebra: aquele teste afirma
que a listagem NAO contem dado do outro restaurante. Com o relatorio vazio a
afirmacao continua verdadeira, o teste passa, e para de provar qualquer coisa
— sem uma linha vermelha em lugar nenhum.

Um teste que envelhece para vermelho custa dez minutos. Um que envelhece para
verde custa a proxima auditoria de isolamento inteira.

## O que e proibido, e o que nao e

So o cruzamento: **data literal dentro de uma querystring de periodo.** E o
unico caso em que o valor escrito a mao e comparado com uma linha carimbada
pelo relogio do banco.

Data literal em geral continua liberada, e a suite esta cheia delas com razao
— `test_order_repository_db.py` grava o pedido com `created_at` explicito e
monta a janela a partir do MESMO instante. Ali os dois lados sao fixos, nada
depende do relogio, e nada envelhece. Proibir isso tambem seria trocar um
defeito real por burocracia.

A saida para quem precisar de uma janela: `fabricas_db.periodo_de_relatorio()`.
"""

import re
from pathlib import Path

import pytest


DIRETORIO_DOS_TESTES = Path(__file__).resolve().parent

# `start_date=` ou `end_date=` seguido de uma data escrita a mao. So pega o
# literal: `start_date={inicio}` (f-string) e `?{periodo}` nao casam.
PERIODO_LITERAL = re.compile(r"(?:start|end)_date=\d{4}-\d{2}-\d{2}")

# Este arquivo cita o defeito no docstring para explica-lo. Sem a excecao, ele
# se acusaria — e a mensagem de erro ensinaria o contrario do que o arquivo diz.
ARQUIVO_DESTE_TESTE = Path(__file__).name


def arquivos_de_teste() -> list[Path]:
    return sorted(
        caminho
        for caminho in DIRETORIO_DOS_TESTES.glob("*.py")
        if caminho.name != ARQUIVO_DESTE_TESTE
    )


@pytest.mark.parametrize(
    "arquivo", arquivos_de_teste(), ids=lambda caminho: caminho.name
)
def test_nenhum_periodo_de_relatorio_e_escrito_a_mao(arquivo: Path):
    linhas = arquivo.read_text(encoding="utf-8").splitlines()
    achados = []
    for numero, linha in enumerate(linhas, start=1):
        # Comentario nao chama rota nenhuma, e ha um que CITA um periodo para
        # explicar o teto de 92 dias ("um start_date=2020-01-01 varre a tabela
        # inteira"). Acusa-lo faria a saida deste teste pedir a correcao de uma
        # frase em portugues.
        if linha.lstrip().startswith("#"):
            continue
        if PERIODO_LITERAL.search(linha):
            achados.append(f"{arquivo.name}:{numero}: {linha.strip()}")

    assert not achados, (
        "periodo de relatorio com data escrita a mao:\n\n"
        + "\n".join(achados)
        + "\n\nEssa data envelhece: o pedido que a fabrica cria nasce com o "
        "`now()` do banco e, passado o `end_date`, cai fora da janela. O "
        "relatorio volta vazio — e nem sempre isso deixa o teste vermelho.\n\n"
        "Use `fabricas_db.periodo_de_relatorio()`."
    )
