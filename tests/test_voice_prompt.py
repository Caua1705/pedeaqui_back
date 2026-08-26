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
    VOICE_INSTRUCTIONS,
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
# ELES SAIRAM DO PROMPT EM 25/08/2026, e este cabecalho registra a inversao
# porque ele dizia o contrario. Dizia: "nenhum dos dois tem rede do lado de ca
# (...) o prompt esta sozinho, e estes testes sao a unica coisa que percebe se
# a secao sumir".
#
# A rede passou a existir. `frase_para_o_modelo` monta a frase com no maximo
# dois produtos e decide o preco pelo tamanho da lista, entao os dois tetos
# agora sao codigo — e o que os testa e `test_voice_frase_da_ferramenta.py`,
# com dados, e nao procurando texto no prompt.
#
# O que sobrou aqui e a metade que continua sendo do prompt: que ele NAO
# carregue mais as regras que se mudaram de lugar. Duas redacoes da mesma
# regra, uma no codigo e outra no texto, e a chance de discordarem depois.
# --------------------------------------------------------------------------


_INSTRUCOES = instructions_for("Nome do restaurante: Junior da Picanha", "Loja: Centro")

# O mesmo prompt com o espaco normalizado. Regra que cai na quebra de linha
# nao pode fazer o teste depender de ONDE a linha quebrou: reformatar um
# paragrafo nao muda a regra, e nao pode quebrar a suite.
_CORRIDO = " ".join(_INSTRUCOES.split())


def test_o_teto_de_produtos_falados_saiu_do_prompt():
    """A secao inteira gastava 13 linhas pedindo ao modelo que ignorasse tres
    dos cinco produtos que a ferramenta mandava. Hoje a frase ja vem com dois,
    e a secao nao pode voltar: com o teto nos dois lugares, quem manda passa a
    depender de qual dos dois o modelo leu."""
    assert "NO MAXIMO DOIS PRODUTOS POR RESPOSTA" not in _INSTRUCOES
    assert "Trazer cinco nao e ordem para falar cinco" not in _INSTRUCOES


def test_a_regra_de_quando_falar_preco_saiu_do_prompt():
    """"UM produto na frase: fale o preco. DOIS: so os nomes" era uma regra
    sobre a frase que o modelo ainda estava montando. Virou o `if` de
    `frase_para_o_modelo`, e nao pode existir nos dois lugares."""
    assert "UM produto na frase" not in _INSTRUCOES
    assert "DOIS produtos na frase" not in _INSTRUCOES

    # E o criterio mais velho ainda nao pode ter voltado junto.
    assert "pergunta direta de preco. Citando dois produtos" not in _INSTRUCOES


def test_o_prompt_manda_dizer_a_frase_e_nao_montar_uma():
    """O que substituiu as duas secoes acima: uma regra, e ela aponta para o
    dado que chega pronto em vez de descrever como montar a resposta."""
    assert "FRASE ja esta pronta para ser dita" in _CORRIDO
    assert "Diga aquilo, palavra por palavra" in _CORRIDO
    assert "DADOS nao se le em voz alta" in _CORRIDO


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


def test_o_prompt_nao_cita_comida_de_restaurante_nenhum():
    """A REGRA DURA de 25/08/2026, e ela vale para o prompt INTEIRO — nao mais
    para uma secao, e sem lista de excecoes.

    A versao anterior deste teste vigiava so a secao do tom, e autorizava as
    linhas de caso enumerado a citar produto. Isso deixou o prompt acumular
    onze mencoes a um corte de carne, tres a um prato de feijao e uma a um
    refrigerante — todos do cardapio de UM restaurante.

    O problema nao e o molde (esse e o `test_nenhum_preco_dizivel...` ao lado).
    E que ESTE prompt e o mesmo para toda a base. Numa pizzaria, cada um desses
    nomes e token pago para ensinar um cardapio que nao existe la, e exemplo
    que empurra a busca para o lado errado. O que muda por restaurante e o DADO
    que a ferramenta devolve, nunca o texto do prompt.
    """
    comidas = (
        "picanha", "baiao", "baião", "feijoada", "guarana", "guaraná",
        "x-tudo", "pudim", "brownie", "black angus", "chocolate",
        "pizza", "hamburguer", "marmoreio",
    )

    # `VOICE_INSTRUCTIONS`, e nao `instructions_for(...)`: o nome do
    # restaurante e a loja sao DADO, colados no fim, e "Junior da Picanha" ali
    # esta certo. A regra e sobre o TEXTO, que e o mesmo para todo mundo.
    estatico = " ".join(VOICE_INSTRUCTIONS.split()).lower()
    for comida in comidas:
        assert comida not in estatico, f"{comida!r} voltou para o prompt generico"


def test_o_prompt_nao_enumera_mais_caso_real():
    """Os treze blocos "Isto ja aconteceu" eram 721 tokens — 18% do prompt — e
    eram o sedimento da hipotese que nao se sustentou: um caso, uma regra, uma
    rodada. Todos citavam o cardapio de um restaurante so, e todos eram exemplo
    de SAIDA ruim, que a armadilha 44 trata como molde.

    Voltar a acrescentar um e voltar para a hipotese."""
    assert "Isto ja aconteceu" not in _INSTRUCOES


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
    assert "nome | preco como se fala | descricao | para quantas pessoas serve" in _INSTRUCOES

    # O campo em digitos SAIU em 25/08/2026, e o prompt nao pode continuar
    # prometendo um campo que a busca nao manda mais: ordinal errado aqui e
    # contradicao silenciosa, que e o defeito mais caro deste arquivo.
    assert "preco em digitos" not in _INSTRUCOES


def test_ordenar_e_comparar_exigem_a_ferramenta_mesmo_com_o_dado_na_conversa():
    """O CASO (25/08/2026): "E qual e mais cara?", logo depois de uma busca de
    picanhas, respondido de memoria — e errado, com a mais cara na MESMA lista
    que ele tinha recebido.

    A regra velha ("sem ordenacao voce nao responde superlativo") ja existia e
    ja falhou, porque ela se lia como "quando faltar dado". O eixo novo nao e
    ter o dado, e sim a resposta exigir ORDENAR ou COMPARAR.

    A ESCOLHA DA ORDENACAO saiu do modelo em 25/08/2026 — quem le "mais
    barato" e decide e `_reescrever_consulta`. O que sobrou aqui e o que o
    backend nao alcanca: o modelo precisa CHAMAR a ferramenta com as palavras
    dele, em vez de ordenar a lista que ja esta na conversa."""
    assert "Voce NAO ordena nem compara de" in _CORRIDO
    assert "nem quando os produtos ja estao na conversa" in _CORRIDO

    # E a mecanica que virou codigo nao pode ter sobrado no texto.
    assert "mais_barato_da_busca" not in _INSTRUCOES
    assert "_da_loja" not in _INSTRUCOES


def test_a_pergunta_de_seguimento_continua_respondendo_do_contexto():
    """A metade que impede a regra acima de virar "busque sempre".

    Forcar busca na LEITURA colidiria com o TERMO LITERAL — o termo literal de
    "essa serve quantas pessoas?" e um pronome — e a segunda busca poderia
    voltar com um conjunto diferente do que esta na tela."""
    assert "Pergunta sobre um produto JA CITADO se responde com o DADOS" in _CORRIDO
    assert "Na duvida entre as duas, CHAME" in _CORRIDO


def test_o_cardapio_inteiro_se_responde_com_listar_categorias():
    """"Quais sao as categorias?" respondido com um cardapio inventado
    (25/08/2026). Nao ha regra que conserte isso: a busca por significado nao
    tem assunto para morder numa pergunta sobre o cardapio inteiro."""
    assert "listar_categorias" in _INSTRUCOES
    assert "Voce NAO sabe que tipos de comida esta loja tem" in _CORRIDO


def test_a_escolha_de_quais_categorias_falar_saiu_do_prompt():
    """"Fale no maximo DUAS, as maiores, e nao leia o numero em voz alta" era o
    mesmo desenho do teto de produtos: mandar o modelo ignorar dez das doze
    linhas que a ferramenta acabou de entregar. Hoje a frase chega com duas
    (`frase_das_categorias`) e a contagem vive so no bloco de DADOS, que a
    regra geral ja manda nao ler.

    E a metade que o prompt sozinho nunca teve: qual PAR falar na segunda
    pergunta. Isso e o cursor da sessao (revisao 20260825_0042), e regra de
    texto nao tem memoria de um turno para o outro."""
    assert "as maiores" not in _CORRIDO
    assert "nao leia o numero em voz alta" not in _CORRIDO

    assert "ela sabe o que ja te mandou e devolve outras" in _CORRIDO
    assert "Nao repita as que voce ja falou" in _CORRIDO


def test_o_que_a_comida_E_nao_se_inventa():
    """A ferramenta nao manda sabor, maciez nem origem, e nao vai mandar. Sem
    esta regra o buraco e preenchido com o que costuma ser verdade num
    restaurante — que e a NAO INVENTE, na forma mais dificil de flagrar,
    porque "bem macia" nao soa como um fato ate alguem conferir."""
    for palavra in ("sabor", "maciez", "textura", "corte", "origem", "tempero"):
        assert palavra in _CORRIDO

    # "marmoreio" e o exemplo de fala ("bem macia", "temperinho da casa")
    # sairam com a limpeza de 25/08/2026: marmoreio e vocabulario de
    # churrascaria, e os dois exemplos eram frases prontas de SAIDA, que a
    # armadilha 44 trata como molde. A lista de ATRIBUTOS basta, e ela serve
    # para pizzaria igual.
    assert "marmoreio" not in _CORRIDO
    assert "temperinho da casa" not in _CORRIDO


def test_o_prompt_manda_copiar_o_preco_falado_em_vez_de_converter():
    """A regra velha ("copie o valor EXATO e mude so a forma de falar")
    mandava fazer duas operacoes e chamava as duas de copia. A segunda era
    traducao de numero para palavras, e foi ela que errou."""
    assert "sai do segundo campo, copiado palavra por palavra" in _CORRIDO
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
    excecao = _INSTRUCOES.index("Pedido de recomendacao")
    fim_da_secao = _INSTRUCOES.index("\nO QUE NAO E COM VOCE")

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


# --------------------------------------------------------------------------
# A NEGATIVA, e o que dela sobrou no prompt
#
# O caso: "voces tem picanha?" numa churrascaria (o Whisper transcreveu
# "Nino"). O modelo nao entendeu a palavra, chamou `listar_categorias` em vez
# da busca, e respondeu "nao tem no cardapio, mas tem Executivos e Bebidas".
# Dois turnos depois negou bebida, tendo ele mesmo listado "Bebidas".
#
# O CONSERTO E DE CODIGO: a negativa virou uma FRASE que so
# `buscar_no_cardapio` devolve (`_NEGATIVA`, em `search_service.py`), e quem a
# testa com dados e `test_voice_frase_da_ferramenta.py`. Enquanto ela fosse
# texto que o modelo escrevia, nenhuma regra impedia que ele a escrevesse sem
# ter buscado — e a regra que mandava buscar antes de negar ja existia.
#
# O que sobra aqui sao as quatro linhas que o codigo nao consegue impor.
# --------------------------------------------------------------------------


def test_a_negativa_nao_e_do_modelo():
    """A frase da negativa chega pronta da busca; montar uma com as palavras
    dele e o caminho de volta ao defeito."""
    assert "A NEGATIVA NAO E SUA" in _CORRIDO
    assert "Voce nunca monta uma negativa com as suas palavras" in _CORRIDO


def test_listar_categorias_nao_autoriza_negativa():
    """Foi por esta porta que o defeito entrou: chamar UMA ferramenta passou
    por "eu busquei"."""
    assert "listar_categorias NAO e busca e nunca autoriza negativa" in _CORRIDO


def test_nao_entendi_nunca_vira_nao_tem():
    """A NAO INVENTE ao contrario: sem entender a palavra, negar o produto e
    pior do que qualquer invencao — o cliente ouve que a churrascaria nao tem
    picanha e desliga."""
    assert "Sem ter chamado buscar_no_cardapio neste turno, NAO EXISTE negativa" in _CORRIDO
    assert '"nao entendi" nunca vira "nao tem"' in _CORRIDO


def test_categoria_recem_listada_nao_pode_ser_negada():
    """O segundo turno do relato: negou bebida dois turnos depois de oferecer
    "Bebidas"."""
    assert "Categoria que voce acabou de listar e coisa que a loja TEM" in _CORRIDO


def test_nao_pegar_a_palavra_e_nao_conhecer_a_palavra_sao_casos_diferentes():
    """A contradicao que o caso desenterrou: "nome que voce nao conhece:
    busque esse nome mesmo assim" convivia com "se nao entendeu bem o nome,
    pergunte antes de buscar". Sao dois casos, e agora estao escritos como
    tais."""
    assert "Nao pegou a palavra que ele falou? Pergunte o nome de novo" in _CORRIDO
    assert "Pegou a palavra mas nao conhece? Busque com ela mesma" in _CORRIDO


def test_a_regra_antiga_da_negativa_nao_ficou_junto_da_nova():
    """Duas redacoes da mesma regra sao a chance de discordarem depois — e a
    antiga era a que o modelo conseguia contornar."""
    assert "NUNCA diga que algo nao existe, nao tem, acabou" not in _CORRIDO
    assert "Diga que aqui nao temos o que ele pediu" not in _CORRIDO
