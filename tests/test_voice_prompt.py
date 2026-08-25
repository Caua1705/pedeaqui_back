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


# --------------------------------------------------------------------------
# OS DOIS TETOS: quantos produtos se FALA, e quando o preco sai junto
#
# Nenhum dos dois tem rede do lado de ca. No texto, `_limitar_cartoes` apara o
# carrossel quando a regra do prompt nao pega; na voz os cartoes saem da NOSSA
# busca e o modelo nao escolhe nada — nao ha o que aparar. O prompt esta
# sozinho, e estes testes sao a unica coisa que percebe se a secao sumir.
# --------------------------------------------------------------------------


_INSTRUCOES = instructions_for("Nome do restaurante: Junior da Picanha", "Loja: Centro")

# O mesmo prompt com o espaco normalizado. Regra que cai na quebra de linha
# nao pode fazer o teste depender de ONDE a linha quebrou: reformatar um
# paragrafo nao muda a regra, e nao pode quebrar a suite.
_CORRIDO = " ".join(_INSTRUCOES.split())


def test_o_teto_de_produtos_falados_e_uma_secao_e_nao_um_bullet():
    """Ele JA era dois, enterrado em O CARDAPIO, e foi desobedecido — o
    atendente falou tres picanhas. A forma da regra e o que faz ela morder:
    secao propria com o caso enumerado, como a do `system_prompt.py`."""
    assert "\nNO MAXIMO DOIS PRODUTOS POR RESPOSTA\n" in _INSTRUCOES


def test_o_bullet_do_cardapio_aponta_para_a_secao_do_teto():
    """Duas redacoes do mesmo teto em lugares diferentes e a chance de as duas
    discordarem depois. O bullet virou ponteiro, e nao uma segunda regra."""
    assert "Quantos deles voce FALA esta em NO MAXIMO DOIS PRODUTOS POR RESPOSTA." in _INSTRUCOES


def test_o_teto_da_fala_nao_e_o_teto_da_busca():
    """A ferramenta continua trazendo cinco, e o prompt tem que dizer por que.
    Sem esta frase, "no maximo dois" convida a cortar a busca — e ai o modelo
    perde a margem para descartar e para saber o que existe antes de negar."""
    assert "Trazer cinco nao e ordem para falar cinco" in _INSTRUCOES


def test_a_regra_do_preco_conta_produtos_e_nao_o_tipo_da_pergunta():
    """A versao anterior tinha DOIS criterios que se contradiziam num caso
    real: "pergunta direta de preco" autorizava, "citando dois produtos"
    proibia, e "e a picanha quanto custa?" era os dois ao mesmo tempo. Quando
    o prompt se contradiz, o modelo resolve para o lado de falar."""
    assert "QUANTOS produtos a sua frase cita, e nao" in _INSTRUCOES
    assert "UM produto na frase" in _INSTRUCOES
    assert "DOIS produtos na frase" in _INSTRUCOES

    # E o criterio velho nao pode ter sobrado ao lado do novo.
    assert "pergunta direta de preco. Citando dois produtos" not in _INSTRUCOES


def test_o_preco_de_um_produto_so_continua_sendo_falado():
    """Calar o preco inteiro seria o erro oposto: quem esta com o telefone no
    ouvido nao ve a tela, e confirmar um pedido sem dizer o valor e pior do
    que falar demais."""
    assert "Confirmar um pedido sem dizer" in _INSTRUCOES


def test_nenhum_preco_dizivel_entrou_no_prompt():
    """A armadilha 44, que este arquivo ja pagou com "trinta e cinco e
    trinta": exemplo de SAIDA falada e molde, e o modelo preenche o molde
    sozinho na sessao seguinte. Os casos enumerados sao a fala do CLIENTE e a
    FALHA — nunca a resposta certa, nunca um numero pronto para ser dito.

    O unico par que pode existir e fonte -> fala, ancorado no campo `price`.
    """
    import re

    dizivel = re.compile(
        r"\b(zero|um|dois|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze|"
        r"treze|quatorze|quinze|dezesseis|dezessete|dezoito|dezenove|vinte|"
        r"trinta|quarenta|cinquenta|sessenta|setenta|oitenta|noventa|cem)\s+e\s+"
        r"(um|dois|tres|quatro|cinco|seis|sete|oito|nove|dez|vinte|trinta|"
        r"quarenta|cinquenta|sessenta|setenta|oitenta|noventa)\b",
        re.IGNORECASE,
    )
    achados = [
        trecho.group(0)
        for trecho in dizivel.finditer(_INSTRUCOES)
        # A UNICA excecao: o par fonte -> fala da regra de copiar o valor
        # exato, que ilustra a CONVERSAO e vem colado no "R$ 43,50" de origem.
        if "R$ 43,50" not in _INSTRUCOES[max(0, trecho.start() - 120) : trecho.start()]
    ]
    assert achados == [], f"preco dizivel solto no prompt: {achados}"


# --------------------------------------------------------------------------
# BALCAO, NAO CALL CENTER
#
# O eixo que faltava. O prompt ja mandava ser DIRETO (quanto se fala) e nao
# mandava soar como PESSOA (como se fala) — e o segundo nao sai do primeiro:
# "temos disponiveis as seguintes opcoes" e empolado E longo.
#
# O que estes testes travam nao e a redacao das regras: e o bloco de
# ENROLACAO, que e a unica parte da secao que pode brigar com a brevidade se
# alguem a reescrever pensando so em simpatia.
# --------------------------------------------------------------------------


def test_a_naturalidade_e_uma_secao_propria():
    """Nao entrou como bullet de COMO FALAR de proposito: aquela secao e sobre
    QUANTO falar, e esta e sobre COMO. A licao do "busque calado", que so
    passou a morder depois de mudar de secao, e o precedente."""
    assert "\nBALCAO, NAO CALL CENTER\n" in _INSTRUCOES


def test_a_enrolacao_e_enumerada_e_nao_deixada_ao_criterio():
    """O UNICO ponto em que naturalidade brigaria com brevidade: particula de
    discurso e natural e e custo puro, em audio de saida, que nunca e
    cacheado. Criterio nao morde; enumeracao morde."""
    for particula in ('"olha"', '"entao"', '"pois e"', '"ne"', '"com certeza"', '"perfeito"'):
        assert particula in _INSTRUCOES, particula
    assert "Natural NAO e enrolado" in _INSTRUCOES


def test_a_lista_falada_continua_proibida_pelo_nome():
    assert '"primeiro"' in _INSTRUCOES
    assert '"as seguintes opcoes"' in _INSTRUCOES


def test_nenhum_exemplo_do_lado_certo_carrega_produto_ou_ingrediente():
    """A armadilha 44, na leitura precisa: o perigo nao e string dizivel, e
    string dizivel com FATO dentro. "tem sim" nao pode virar mentira sobre
    coisa nenhuma; "tem picanha importada" pode — e seria a mesma falha de
    2026 com outra roupa.

    A guarda e grosseira de proposito. Ela nao entende portugues: ela procura
    nome de comida em qualquer lugar do prompt fora dos casos enumerados de
    FALHA, que sao os unicos autorizados a citar produto.
    """
    comidas = (
        "picanha",
        "baiao",
        "sobremesa de chocolate",
        "brownie",
        "pudim",
        "feijoada",
        "black angus",
    )
    # As linhas que PODEM citar produto: as de caso enumerado (falha real) e
    # as de exemplo de BUSCA (o termo literal que o cliente falou).
    permitidas = [
        linha
        for linha in _INSTRUCOES.splitlines()
        if "ja aconteceu" in linha
        or "voce buscou" in linha
        or "ele disse" in linha
        or "respondido com" in linha
        or "busca " in linha
        or "quanto custa a picanha" in linha
        or "tem sobremesa" in linha.lower()
        or "picanhas seguidas" in linha
    ]
    inicio = _INSTRUCOES.index("BALCAO, NAO CALL CENTER")
    fim = _INSTRUCOES.index("NAO INVENTE", inicio)
    secao = _INSTRUCOES[inicio:fim]

    for comida in comidas:
        assert comida not in secao.lower(), f"{comida!r} virou molde na secao do tom"
    assert permitidas, "a guarda perdeu a referencia das linhas autorizadas"


def test_a_secao_do_tom_nao_traz_numero_nenhum():
    """Mesma razao. Um numero solto na secao que ensina a FALAR e um preco
    esperando para ser dito."""
    import re

    inicio = _INSTRUCOES.index("BALCAO, NAO CALL CENTER")
    fim = _INSTRUCOES.index("NAO INVENTE", inicio)
    assert re.search(r"\d", _INSTRUCOES[inicio:fim]) is None


# --------------------------------------------------------------------------
# OS QUATRO CASOS DA SESSAO DE 25/08/2026
#
# Todos vieram de log, e e por isso que estao enumerados no prompt sob "Isto
# ja aconteceu". O que estes testes guardam nao e a redacao: e o par
# prompt <-> contrato da ferramenta nao se soltar. Duas destas regras falam de
# CAMPOS que o `resumo_para_o_modelo` produz, e uma regra apontando para campo
# que nao existe mais e pior do que regra nenhuma.
# --------------------------------------------------------------------------


def test_o_prompt_explica_os_quatro_campos_da_busca():
    """A explicacao mora AQUI, e nao no resultado da ferramenta: o prompt e
    cacheado a partir do turno 2, e o resultado e cobrado inteiro em toda
    busca. Ver `test_o_formato_nao_e_explicado_dentro_do_resultado`."""
    assert "nome | preco em digitos | preco como se fala | descricao" in _INSTRUCOES


def test_o_prompt_manda_copiar_o_preco_falado_em_vez_de_converter():
    """A regra velha ("copie o valor EXATO e mude so a forma de falar")
    mandava fazer duas operacoes e chamava as duas de copia. A segunda era
    traducao de numero para palavras, e foi ela que errou."""
    assert "JA E o preco escrito como se fala" in _INSTRUCOES
    assert "Voce nao converte numero nenhum de cabeca" in _CORRIDO

    # E a regra velha nao pode ter sobrado ao lado da nova.
    assert "mude so a forma de falar" not in _INSTRUCOES


def test_negar_o_que_a_busca_devolveu_e_tratado_como_invencao():
    """A NAO INVENTE ao contrario. Ela so pode ser exigida depois de a
    descricao passar a viajar no resumo — antes, o modelo nao tinha o dado, e
    a regra estaria exigindo o que o contrato nao entregava."""
    assert "tao falso quanto inventar" in _CORRIDO


def test_recomendacao_e_excecao_do_termo_literal_e_esta_colada_nele():
    """A licao do cumprimento, que ja custou uma rodada: excecao lida tres
    paragrafos depois da regra nao e lida junto dela. Buscar "recomenda" nao
    devolve nada, e sem a excecao ali o modelo devolve a pergunta."""
    literal = _INSTRUCOES.index("Busque com A PALAVRA QUE O CLIENTE FALOU")
    excecao = _INSTRUCOES.index("pedido de recomendacao")
    fim_da_secao = _INSTRUCOES.index("\nNO MAXIMO DOIS PRODUTOS")

    assert literal < excecao < fim_da_secao
    assert "Nunca devolva a pergunta" in _INSTRUCOES


def test_recomendar_nao_autoriza_inventar_motivo():
    """O caminho facil de uma recomendacao e a justificativa, e nenhuma delas
    o modelo tem como saber."""
    # Espaco normalizado: estas frases caem na quebra de linha do prompt, e um
    # teste que depende de onde a linha quebrou quebra junto com a reformatacao.
    for invencao in ('"e o mais pedido"', '"sai muito"', '"todo mundo gosta"'):
        assert invencao in _CORRIDO, invencao


def test_xingamento_tem_tratamento_proprio_e_apaga_o_produto():
    """"Pergunta nova apaga o produto anterior" nao pegou porque xingamento
    nao e pergunta — leitura literal, e o modelo leu literal."""
    assert "Xingamento, agressao ou provocacao" in _INSTRUCOES
    assert "QUALQUER coisa que nao seja sobre comida apaga" in _CORRIDO


def test_xingamento_nao_pede_para_repetir():
    """A resposta da primeira vez ("nao to entendendo, pode repetir?") esta
    certa de tom e errada de efeito: pedir para repetir um xingamento e
    convidar o segundo."""
    assert "Nao peca para ele repetir" in _INSTRUCOES


# --------------------------------------------------------------------------
# A COMANDA QUE NAO EXISTE
#
# O unico defeito desta serie que chega ao fim do funil: "vou anotar como dois
# baioes e uma picanha", dito por quem nao anota nada. O cliente desliga
# achando que tem pedido montado e chega no checkout com o carrinho vazio.
#
# A regra "voce nao fecha pedido" ja existia e falhou pelo motivo de sempre:
# proibia sem autorizar o substituto.
# --------------------------------------------------------------------------


def test_as_frases_de_anotar_pedido_sao_enumeradas():
    """Criterio nao morde, enumeracao morde. E aqui a enumeracao e barata: sao
    as frases que um atendente de balcao de verdade diria, e e exatamente por
    isso que o modelo as produz."""
    for proibida in ('"vou anotar"', '"ja anotei"', '"adicionei"', '"ja esta no carrinho"'):
        assert proibida in _CORRIDO, proibida


def test_a_proibicao_de_anotar_vem_com_o_substituto():
    """A licao do "busque calado": regra que so proibe deixa silencio onde o
    cliente acabou de pedir alguma coisa, e o modelo preenche."""
    assert "O que voce FAZ e mostrar" in _CORRIDO
    assert "e o cliente que toca neles para adicionar" in _CORRIDO


def test_o_prompt_nao_manda_mais_perguntar_quantidade():
    """A contradicao que o caso desenterrou: perguntar "quantos?" pressupoe
    que alguem anota. Era o convite mais direto possivel para "vou anotar"."""
    assert "escolha entre dois produtos, faixa de preco" in _CORRIDO
    assert "escolha entre opcoes, quantidade, ponto da carne" not in _CORRIDO
    assert "Nao pergunte quantidade nem ponto da carne" in _CORRIDO
