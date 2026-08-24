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
from src.ai.voice.voice_prompt import (
    SAUDACOES_COM_NOME,
    SAUDACOES_SEM_NOME,
    branch_context_for,
    instructions_for,
    primeiro_nome_dizivel,
    saudacao_para,
)
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


# --------------------------------------------------------------------------
# A SAUDACAO
#
# Ela e falada em TODA sessao, antes de o cliente dizer qualquer coisa — e o
# unico texto do produto que sai da boca do atendente sem ninguem ter pedido.
# O que estes testes travam nao e a redacao: e o campo `customers.name` ser
# texto livre, e o que ha nele nem sempre ser um nome.
# --------------------------------------------------------------------------


def test_o_primeiro_nome_e_so_o_primeiro():
    assert primeiro_nome_dizivel("Joao da Silva Sauro") == "Joao"


def test_nome_composto_por_hifen_ou_apostrofo_continua_dizivel():
    """Nao e caso de borda inventado: e como se escreve meio Nordeste."""
    assert primeiro_nome_dizivel("Jean-Pierre Aragao") == "Jean-Pierre"
    assert primeiro_nome_dizivel("D'Angelo Souza") == "D'Angelo"


def test_o_que_nao_da_para_falar_vira_none():
    """O que esta listado aqui esta no banco de verdade. Falar "um dois tres
    quatro cinco" em voz alta e pior do que nao falar nome nenhum."""
    assert primeiro_nome_dizivel(None) is None
    assert primeiro_nome_dizivel("") is None
    assert primeiro_nome_dizivel("   ") is None
    assert primeiro_nome_dizivel("12345") is None
    assert primeiro_nome_dizivel("maria@exemplo.com") is None
    assert primeiro_nome_dizivel("J") is None
    assert primeiro_nome_dizivel("A" * 21) is None


def test_a_saudacao_com_nome_diz_o_nome():
    saudacao = saudacao_para("Maria Aparecida")

    assert "Maria" in saudacao
    assert "Aparecida" not in saudacao


def test_cadastro_com_lixo_cai_na_variacao_sem_nome():
    """A queda tem que ser uma saudacao INTEIRA, e nao a frase com um buraco
    onde o nome estaria."""
    assert saudacao_para("12345") in SAUDACOES_SEM_NOME


def test_toda_variacao_com_nome_tem_onde_por_o_nome():
    """Uma variacao sem `{nome}` sairia sorteada de vez em quando, e o cliente
    seria cumprimentado pelo nome so as vezes — sintoma que ninguem liga ao
    sorteio."""
    for variacao in SAUDACOES_COM_NOME:
        assert "{nome}" in variacao
    for variacao in SAUDACOES_SEM_NOME:
        assert "{nome}" not in variacao


def test_a_saudacao_e_curta():
    """Ela e audio de SAIDA, o item mais caro da conta, e acontece em toda
    sessao — inclusive nas que o cliente abandona no segundo seguinte. O teto
    e generoso de proposito: o que ele barra e a frase que cresceu sem
    ninguem notar."""
    for variacao in SAUDACOES_COM_NOME + SAUDACOES_SEM_NOME:
        assert len(variacao) <= 45


def test_a_saudacao_nao_esta_no_prompt():
    """O par fonte -> saida da armadilha 44: frase pronta dentro das
    instrucoes e molde, e molde o modelo preenche sozinho depois. A saudacao
    viaja na instrucao de UM turno, e o prompt nao a conhece."""
    instrucoes = instructions_for("Nome do restaurante: Junior da Picanha", "Loja: Centro")

    for variacao in SAUDACOES_COM_NOME + SAUDACOES_SEM_NOME:
        assert variacao not in instrucoes
