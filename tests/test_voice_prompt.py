"""O contexto da LOJA dentro das instrucoes da sessao de voz.

Nao leva marcador `db`: nada aqui toca no banco, autentica ou fala com a
OpenAI — sao duas funcoes puras e uma string.

Por que existe. Desde a revisao `20260820_0026` o cardapio e da filial, e a
busca ja devolve so o que aquela loja vende. O que ESTE arquivo trava e a
outra metade, a que nenhuma consulta pega: o modelo saber em qual loja ele
esta. Sem isso a negativa dele ("nao temos pudim") sai como afirmacao sobre o
restaurante inteiro, em audio, para um cliente que nao tem tela onde conferir.
"""

from src.ai.voice.search_service import VoiceSearchService
from src.ai.voice.voice_prompt import branch_context_for, instructions_for
from src.models.branch_model import Branch


def _filial(nome: str, nome_de_tela: str | None = None) -> Branch:
    """Uma filial solta, sem sessao. Só os dois campos de nome importam aqui."""
    return Branch(name=nome, display_name=nome_de_tela)


def test_a_loja_e_o_nome_que_o_lojista_escolheu_mostrar():
    assert branch_context_for(_filial("centro", "Centro")) == "Loja: Centro"


def test_sem_nome_de_tela_vale_o_interno():
    """`display_name` e anulavel, e filial sem ele nao pode deixar o modelo
    sem loja nenhuma."""
    assert branch_context_for(_filial("Aldeota")) == "Loja: Aldeota"


def test_a_loja_entra_numa_secao_propria_das_instrucoes():
    """Separada do bloco do restaurante de proposito: junto, "Loja: Centro"
    seria mais uma linha sobre a casa, e a regra que fala em "esta loja" nao
    teria a que apontar."""
    instrucoes = instructions_for("Nome do restaurante: Junior da Picanha", "Loja: Centro")

    assert "\nA LOJA\nLoja: Centro" in instrucoes
    assert "O RESTAURANTE\nNome do restaurante: Junior da Picanha" in instrucoes


def test_a_negativa_do_prompt_e_a_da_ferramenta_falam_a_mesma_lingua():
    """O modelo le as DUAS: as instrucoes e o resultado da tool call. Se uma
    delas disser "nao temos" sem recorte, e ela que ele repete — a mais
    frouxa das duas e a que vale."""
    instrucoes = instructions_for("Nome do restaurante: Junior da Picanha", "Loja: Centro")

    assert "AQUI nao temos" in instrucoes
    assert "aqui nao temos" in instrucoes
    assert VoiceSearchService.resumo_para_o_modelo([]) == "Nenhum produto encontrado nesta loja."
