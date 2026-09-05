"""Responde "este restaurante esta pronto?" antes do primeiro pedido de verdade.

Confere os CINCO passos do onboarding cuja omissao nao gera erro em lugar
nenhum — nem no boot, nem no log, nem na tela. Eles estao listados em
`docs/onboarding-de-restaurante.md`, na tabela "Os cinco que quebram em
silencio", e o padrao deles e sempre o mesmo: o restaurante entra no ar,
parece pronto, e falha no primeiro pedido de verdade.

    1. Horarios da filial (3.1)      todo pedido recusado, inclusive retirada
    2. Taxa de entrega por km (3.2)  "fora da area" num endereco a 500 m
    3. Setor de impressao (5.2)      sai so a via do cliente; a cozinha nao recebe nada
    4. Segredo do webhook (6.4)      o cliente paga e o pedido nunca sai de "aguardando"
    5. Comissao negociada (7)        todo pedido congela 10%, e NAO tem conserto depois

E uma SEXTA conferencia, que nao e um dos cinco e nao se comporta como eles:

    6. Canal de WhatsApp (WA)        por qual numero cada loja avisa o cliente

Ela NUNCA sai como ERRO, e isso e deliberado: os cinco de cima bloqueiam o
primeiro pedido, e a falta de WhatsApp nao bloqueia nada — o pedido entra, a
comanda sai, o dinheiro entra. O que ela responde e a pergunta que nao tem
tela nem log: "por qual numero ESTA loja fala?". A filial que HERDA o numero
do restaurante e a filial que nao tem numero nenhum sao indistinguiveis de
fora, e so uma delas avisa o cliente.

SO LEITURA. Nenhum INSERT, UPDATE ou DDL — este script nao conserta nada e
nao deve passar a consertar. As cinco correcoes tem donos diferentes (duas
sao tela do lojista, tres sao SQL ou script no servidor) e escolher por
conta propria seria escrever em producao sem ninguem pedir.

Uso:

    python scripts/check_restaurant.py junior-da-picanha
    python scripts/check_restaurant.py --todos
    docker exec pedeaqui-api python scripts/check_restaurant.py junior-da-picanha

Sai com 1 quando ha ERRO, para servir de porta num script de instalacao.
ATENCAO nao derruba a saida: quase todo ATENCAO daqui pode ser uma decisao
legitima do lojista (domingo fechado, refrigerante sem comanda de cozinha), e
so quem conhece a loja sabe a diferenca.
"""

import argparse
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.constants import DEFAULT_PLATFORM_COMMISSION_PERCENT
from src.db.session import SessionLocal


OK = "OK"
ATENCAO = "ATENCAO"
ERRO = "ERRO"

# Sem acento e sem travessao em NADA que va para o console: no Windows do
# balcao ele abre na codepage do sistema (850/1252 no Brasil) e um caractere
# de fora dela sai como "?" ou derruba o proprio print (armadilha 29).
DIAS = ("segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo")


@dataclass
class Conferencia:
    """O resultado de UMA conferencia.

    `passo` e o numero no `docs/onboarding-de-restaurante.md` nos cinco
    silenciosos. A do WhatsApp usa `WA` porque o passo dela nao mora naquele
    documento: mora em `docs/whatsapp.md`, secoes 9.2 e 9.3. Inventar um
    numero de onboarding para ela mandaria quem le procurar uma secao que nao
    existe.
    """

    passo: str
    titulo: str
    situacao: str
    linhas: list[str] = field(default_factory=list)
    o_que_fazer: str = ""


@dataclass
class Restaurante:
    id: str
    slug: str
    name: str


# ---------------------------------------------------------------------------
# 1. Horarios da filial (passo 3.1)
# ---------------------------------------------------------------------------


def conferir_horarios(db: Session, restaurante: Restaurante) -> Conferencia:
    """Dia sem faixa utilizavel = dia fechado, e isso recusa ate retirada.

    "Utilizavel" exclui a linha marcada `is_closed` e a linha com horario
    nulo: as duas existem no banco e nenhuma das duas abre a loja. Nao existe
    "a faixa mais parecida" — ou a hora cai dentro de uma faixa, ou esta
    fechado (armadilha 10).
    """
    filiais = _filiais_ativas(db, restaurante)
    if not filiais:
        return Conferencia("3.1", "Horarios de funcionamento", ERRO,
                           ["o restaurante nao tem nenhuma filial ativa"],
                           "Crie a filial (passo 1 do onboarding).")

    linhas, situacao = [], OK
    for filial in filiais:
        faltando = _dias_sem_faixa(db, filial["id"])
        if not faltando:
            linhas.append(f"  {filial['name']}: os sete dias configurados")
            continue

        if len(faltando) == 7:
            situacao = ERRO
            linhas.append(f"  {filial['name']}: NENHUM dia configurado - todo pedido e recusado")
            continue

        situacao = ATENCAO if situacao == OK else situacao
        nomes = ", ".join(DIAS[dia] for dia in faltando)
        linhas.append(f"  {filial['name']}: fechada em {nomes}")

    return Conferencia(
        "3.1", "Horarios de funcionamento", situacao, linhas,
        "Painel > Configuracoes > Horarios. Lembre que o PUT substitui a semana "
        "INTEIRA: dia que nao vier no corpo deixa de existir.",
    )


def _dias_sem_faixa(db: Session, branch_id: str) -> list[int]:
    """Os weekdays (0 = segunda) sem nenhuma faixa que abra a loja."""
    linhas = db.execute(text(
        """
        SELECT DISTINCT weekday
          FROM branch_business_hours
         WHERE branch_id = CAST(:branch_id AS uuid)
           AND is_closed IS FALSE
           AND opens_at IS NOT NULL
           AND closes_at IS NOT NULL
        """
    ), {"branch_id": branch_id}).scalars().all()
    return [dia for dia in range(7) if dia not in set(linhas)]


# ---------------------------------------------------------------------------
# 2. Taxa de entrega por km (passo 3.2)
# ---------------------------------------------------------------------------


def conferir_taxa_de_entrega(db: Session, restaurante: Restaurante) -> Conferencia:
    """Base ou por-km nulo derruba TODA entrega daquela filial para a contingencia.

    E a contingencia (`default_delivery_fee`) so existe se for MAIOR QUE ZERO:
    a coluna nasce zero na maioria das linhas sem ninguem ter escolhido isso, e
    tratar esse zero como "entrega gratis" faria uma queda do Google virar
    frete gratis para a plataforma inteira (armadilha 11).

    Tudo aqui e POR FILIAL desde a revisao 20260818_0025 — inclusive
    "aceita entrega". Uma rede pode ter o quiosque que so faz retirada ao lado
    da loja de rua que entrega, e a conferencia precisa passar filial a filial
    para nao dar OK global por causa de uma delas.
    """
    linhas, situacao = [], OK
    for filial in _filiais_ativas(db, restaurante):
        if not filial["accepts_delivery"]:
            linhas.append(f"  {filial['name']}: nao aceita entrega; nada a conferir")
            continue

        faltando = [
            nome for nome in ("delivery_base_fee", "delivery_fee_per_km")
            if filial[nome] is None
        ]
        if not faltando:
            linhas.append(f"  {filial['name']}: base {filial['delivery_base_fee']} "
                          f"+ {filial['delivery_fee_per_km']}/km")
            continue

        contingencia = filial["contingencia"]
        if contingencia is None or contingencia <= 0:
            situacao = ERRO
            linhas.append(f"  {filial['name']}: {' e '.join(faltando)} sem valor, e "
                          "default_delivery_fee e zero - TODA entrega vira 'fora da area'")
            continue

        situacao = ATENCAO if situacao == OK else situacao
        linhas.append(f"  {filial['name']}: {' e '.join(faltando)} sem valor; toda entrega "
                      f"sai pela contingencia de {contingencia}, a mesma para 500 m e 15 km")

    if not linhas:
        linhas.append("  o restaurante nao tem nenhuma filial ativa")

    return Conferencia(
        "3.2", "Taxa de entrega por km", situacao, linhas,
        "Painel > Configuracoes > Filial. Entrega gratis e base 0 E por-km 0; "
        "deixar nulo nao e gratis, e quebrado.",
    )


# ---------------------------------------------------------------------------
# 3. Setor de impressao dos produtos (passo 5.2)
# ---------------------------------------------------------------------------


def conferir_setores_de_impressao(db: Session, restaurante: Restaurante) -> Conferencia:
    """`printing_sector_id` nulo e indistinguivel de "esqueci de configurar".

    Nulo significa NAO IMPRIMIR via de producao, e e uma decisao legitima (a
    lata que sai da geladeira do balcao). O que nao e decisao e o cardapio
    INTEIRO assim: sai so a via do cliente e a cozinha nao recebe nada, sem
    erro em lugar nenhum.

    **Conferencia FILIAL A FILIAL desde a revisao 20260820_0026**, pelo mesmo
    motivo da taxa de entrega: cada loja tem o proprio cardapio e as proprias
    impressoras, e uma contagem global daria OK pela loja bem configurada
    enquanto a outra nao imprime nada.

    E ela e a conferencia mais importante logo depois daquela migracao: o
    remapeamento de setor das copias e por NOME, e todo produto que nao
    encontrou um setor de mesmo nome na loja de destino ficou com nulo.
    """
    filiais = _filiais_ativas(db, restaurante)
    if not filiais:
        return Conferencia("5.2", "Setor de impressao dos produtos", ERRO,
                           ["  o restaurante nao tem nenhuma filial ativa"],
                           "Crie a filial (passo 1 do onboarding).")

    linhas: list[str] = []
    situacao = OK
    for filial in filiais:
        situacao = _pior(situacao, _conferir_setores_da_filial(db, filial, linhas))

    return Conferencia(
        "5.2", "Setor de impressao dos produtos", situacao, linhas,
        "Use PATCH /admin/categories/{id}/printing-sector, que aponta a categoria "
        "inteira. Produto a produto, um cardapio de 200 itens nunca sai do lugar.",
    )


def _conferir_setores_da_filial(db: Session, filial: dict, linhas: list[str]) -> str:
    """A situacao de UMA loja, escrevendo o diagnostico dela em `linhas`."""
    nome = filial["name"]
    total = _contar_por_id(db, SQL_PRODUTOS_ATIVOS, filial["id"])
    if total == 0:
        linhas.append(f"  {nome}: NENHUM produto ativo - esta loja nao vende nada")
        return ERRO

    setores = _contar_por_id(db, SQL_SETORES_ATIVOS, filial["id"])
    sem_setor = _contar_por_id(db, SQL_PRODUTOS_SEM_SETOR, filial["id"])
    setor_morto = _contar_por_id(db, SQL_PRODUTOS_COM_SETOR_DESATIVADO, filial["id"])

    linhas.append(f"  {nome}: {total - sem_setor} de {total} produtos ativos com setor")

    # Os diagnosticos se ACUMULAM em vez de o primeiro encerrar a conferencia:
    # "nenhum setor cadastrado" e "produto apontando para setor desativado" sao
    # dois problemas diferentes e podem valer juntos. Ver so o primeiro faria o
    # lojista consertar um e rodar de novo para descobrir o outro.
    situacao = OK
    if setores == 0:
        situacao = ERRO
        linhas.append(f"  {nome}: nenhum setor cadastrado - a cozinha nao recebe NADA")
    elif sem_setor == total:
        situacao = ERRO
        linhas.append(f"  {nome}: NENHUM produto tem setor - sai so a via do cliente")
    elif sem_setor:
        situacao = ATENCAO
        linhas.append(f"  {nome}: {sem_setor} sem setor (pode ser proposital: "
                      "bebida de balcao)")

    if setor_morto:
        situacao = ERRO
        linhas.append(f"  {nome}: {setor_morto} produtos apontam para setor DESATIVADO - "
                      "vao para a via 'SEM SETOR'")
    return situacao


SQL_PRODUTOS_ATIVOS = """
    SELECT count(*) FROM products
     WHERE branch_id = CAST(:id AS uuid) AND is_active IS TRUE
"""

SQL_PRODUTOS_SEM_SETOR = """
    SELECT count(*) FROM products
     WHERE branch_id = CAST(:id AS uuid)
       AND is_active IS TRUE
       AND printing_sector_id IS NULL
"""

SQL_SETORES_ATIVOS = """
    SELECT count(*) FROM printing_sectors
     WHERE branch_id = CAST(:id AS uuid) AND is_active IS TRUE
"""

# Setor de OUTRA filial nao aparece aqui e nao precisa: a FK composta
# (branch_id, printing_sector_id) da revisao 20260820_0026 nao deixa gravar
# esse par. Sobrou o setor desativado, que e estado e nao integridade.
SQL_PRODUTOS_COM_SETOR_DESATIVADO = """
    SELECT count(*) FROM products p
      JOIN printing_sectors ps ON ps.id = p.printing_sector_id
     WHERE p.branch_id = CAST(:id AS uuid)
       AND p.is_active IS TRUE
       AND ps.is_active IS FALSE
"""


# ---------------------------------------------------------------------------
# 4. Credencial e segredo do webhook do Mercado Pago (passo 6.4)
# ---------------------------------------------------------------------------


def conferir_webhook_do_pagamento(db: Session, restaurante: Restaurante) -> Conferencia:
    """O silencioso numero 1, e o unico que ja mordeu.

    So vale a pena para quem OFERECE pagamento online: sem metodo online
    cadastrado nao ha cobranca no gateway e nao ha webhook para chegar. Com
    metodo online e `webhook_secret_encrypted` nulo, o webhook daquele
    restaurante responde 503 e o pedido nunca sai de "aguardando pagamento" —
    depois de o dinheiro ja ter saido da conta do cliente.

    O ambiente conferido e o ATIVO (`MERCADOPAGO_ENVIRONMENT`), que e global
    para a plataforma: credencial de teste cadastrada nao ajuda em nada se a
    plataforma esta em production.
    """
    ambiente = settings.MERCADOPAGO_ENVIRONMENT
    metodos = _contar(db, """
        SELECT count(*) FROM branch_payment_methods bpm
          JOIN branches b ON b.id = bpm.branch_id
         WHERE b.restaurant_id = CAST(:id AS uuid)
           AND b.is_active IS TRUE AND bpm.enabled IS TRUE
           AND (bpm.payment_flow = 'online' OR bpm.requires_gateway IS TRUE)
    """, restaurante)

    if metodos == 0:
        return Conferencia("6.4", f"Webhook do Mercado Pago (ambiente {ambiente})", OK,
                           ["  nenhuma forma de pagamento online oferecida; nada a conferir"],
                           "Se for ligar pagamento online, faca o passo 6 ANTES de "
                           "cadastrar o metodo na tela.")

    credencial = db.execute(text(
        "SELECT webhook_secret_encrypted IS NOT NULL AS tem_segredo"
        "  FROM restaurant_payment_credentials"
        " WHERE restaurant_id = CAST(:id AS uuid) AND environment = :ambiente"
    ), {"id": restaurante.id, "ambiente": ambiente}).mappings().one_or_none()

    o_que_fazer = (
        "python scripts/register_restaurant_payment_credential.py "
        f"--restaurant-slug {restaurante.slug} --environment {ambiente} --webhook-secret-only"
    )
    titulo = f"Webhook do Mercado Pago (ambiente {ambiente})"

    if credencial is None:
        return Conferencia("6.4", titulo, ERRO, [
            f"  {metodos} forma(s) de pagamento online oferecida(s)",
            f"  e NAO ha credencial cadastrada para o ambiente '{ambiente}'",
        ], o_que_fazer)

    if not credencial["tem_segredo"]:
        return Conferencia("6.4", titulo, ERRO, [
            f"  {metodos} forma(s) de pagamento online oferecida(s)",
            "  credencial cadastrada, mas SEM segredo de webhook - o webhook responde 503",
            "  sintoma: o cliente paga, o dinheiro sai, e o pedido nunca chega ao painel",
        ], o_que_fazer)

    return Conferencia("6.4", titulo, OK, [
        f"  credencial e segredo de webhook cadastrados para '{ambiente}'",
    ])


# ---------------------------------------------------------------------------
# 5. Comissao negociada (passo 7)
# ---------------------------------------------------------------------------


def conferir_comissao(db: Session, restaurante: Restaurante) -> Conferencia:
    """O unico dos cinco que NAO tem conserto depois.

    O percentual e congelado em cada pedido no momento da criacao. Por isso a
    contagem de pedidos ja criados entra aqui: com zero pedidos, comissao no
    padrao ainda e uma pergunta ("o contrato foi 10%?"); com pedidos, e um
    prejuizo que so ajuste manual fora do sistema resolve.

    Nao ha como este script saber qual foi o contrato. Ele diz qual valor
    esta valendo e quantos pedidos ja congelaram nele; a conferencia contra o
    contrato e humana.
    """
    padrao = Decimal(DEFAULT_PLATFORM_COMMISSION_PERCENT)
    configurado = db.execute(text(
        "SELECT platform_commission_percent FROM restaurant_settings"
        " WHERE restaurant_id = CAST(:id AS uuid)"
    ), {"id": restaurante.id}).scalar()
    pedidos = _contar(db, "SELECT count(*) FROM orders WHERE restaurant_id = CAST(:id AS uuid)",
                      restaurante)

    o_que_fazer = (
        "UPDATE restaurant_settings SET platform_commission_percent = <negociado> "
        f"WHERE restaurant_id = '{restaurante.id}';  -- nao ha tela, e de proposito"
    )

    if configurado is None:
        linhas = [f"  sem linha em restaurant_settings; vale o padrao de {padrao}%"]
        if pedidos:
            linhas.append(f"  e {pedidos} pedido(s) JA congelaram esse percentual, sem volta")
        return Conferencia("7", "Comissao da plataforma", ERRO if pedidos else ATENCAO,
                           linhas, o_que_fazer)

    if configurado == padrao:
        linhas = [f"  {configurado}%, que e exatamente o padrao - foi negociado ou foi omissao?"]
        if pedidos:
            linhas.append(f"  {pedidos} pedido(s) ja congelaram esse percentual, sem volta")
        return Conferencia("7", "Comissao da plataforma", ERRO if pedidos else ATENCAO,
                           linhas, o_que_fazer)

    return Conferencia("7", "Comissao da plataforma", OK,
                       [f"  {configurado}%, diferente do padrao: foi escolhido"])


# ---------------------------------------------------------------------------
# 6. Canal de WhatsApp (docs/whatsapp.md, 9.2 e 9.3)
# ---------------------------------------------------------------------------


def conferir_canal_de_whatsapp(db: Session, restaurante: Restaurante) -> Conferencia:
    """Por qual numero cada loja avisa o cliente — e se ela avisa.

    NUNCA devolve ERRO. Aviso de WhatsApp e opcional: sem canal o pedido
    entra, a comanda sai e o dinheiro entra do mesmo jeito. Bloquear a
    instalacao por causa dele seria o script mentindo sobre o que "pronto"
    significa. Todo achado daqui e ATENCAO, que e "confirme com o lojista".

    O que ela existe para mostrar sao os tres estados que se parecem de fora
    e pedem coisas diferentes:

        herda o numero do restaurante   avisa, e esta certo
        numero proprio DESLIGADO        nao avisa, e NAO cai no do
                                        restaurante (ver `resolve_for_branch`)
        sem numero nenhum               nao avisa, e ninguem sabe

    A resolucao e repetida em Python aqui, e o preco disso e o mesmo de
    `_filiais_ativas`: se `resolve_for_branch` mudar, muda aqui. A alternativa
    seria importar o repositorio e abrir uma sessao do ORM, e este script
    conversa com o banco em SQL cru de proposito — ele roda sem subir o app.
    """
    canais = _canais_do_restaurante(db, restaurante)
    filiais = _filiais_ativas(db, restaurante)
    o_que_fazer = (
        "docs/whatsapp.md 9.2 (script) ou 9.3 (painel). Religar depois de uma "
        "desconexao da Meta comeca no WhatsApp da loja, e SO DEPOIS aqui."
    )

    if not canais:
        return Conferencia("WA", "Canal de WhatsApp", ATENCAO, [
            "  nenhum numero cadastrado - o cliente nao recebe aviso de pedido nenhum",
            "  (pode ser decisao: a frente inteira e opcional)",
        ], o_que_fazer)

    linhas, situacao = [], OK
    if not settings.WHATSAPP_NOTIFICATIONS_ENABLED:
        situacao = ATENCAO
        linhas.append("  WHATSAPP_NOTIFICATIONS_ENABLED esta FALSA na plataforma:")
        linhas.append("  ha numero cadastrado e MESMO ASSIM nenhum aviso sai, de loja nenhuma")

    if not settings.WHATSAPP_APP_SECRET:
        situacao = _pior(situacao, ATENCAO)
        linhas.append("  WHATSAPP_APP_SECRET ausente: o webhook responde 503, entao")
        linhas.append("  'entregue' e 'lido' nunca voltam e a janela de 24h nunca abre")

    do_restaurante = _canal_do_restaurante(canais)
    for filial in filiais:
        situacao = _pior(situacao, _linha_do_canal_da_filial(
            filial, canais, do_restaurante, linhas
        ))

    return Conferencia("WA", "Canal de WhatsApp", situacao, linhas, o_que_fazer)


def _linha_do_canal_da_filial(
    filial: dict,
    canais: list[dict],
    do_restaurante: dict | None,
    linhas: list[str],
) -> str:
    """Uma linha por filial, dizendo por qual numero ela fala.

    A ordem das perguntas e a de `resolve_for_branch`, e ela nao pode ser
    invertida: quem tem linha PROPRIA nunca cai na do restaurante, nem quando
    a propria esta desligada.
    """
    propria = _canal_da_filial(canais, filial["id"])

    if propria is not None and _utilizavel(propria):
        linhas.append(f"  {filial['name']}: numero proprio {propria['display_phone_number']}")
        return OK

    if propria is not None:
        linhas.append(f"  {filial['name']}: numero proprio {propria['display_phone_number']} "
                      f"{_por_que_nao_manda(propria)}")
        linhas.append("    esta loja nao avisa ninguem, e NAO cai no numero do restaurante")
        return ATENCAO

    if do_restaurante is not None and _utilizavel(do_restaurante):
        linhas.append(f"  {filial['name']}: herda o numero do restaurante "
                      f"{do_restaurante['display_phone_number']}")
        return OK

    if do_restaurante is not None:
        linhas.append(f"  {filial['name']}: herdaria o numero do restaurante, que esta "
                      f"{_por_que_nao_manda(do_restaurante)}")
        return ATENCAO

    linhas.append(f"  {filial['name']}: SEM numero - nenhum aviso sai desta loja")
    return ATENCAO


def _por_que_nao_manda(canal: dict) -> str:
    """As duas razoes, e elas pedem consertos em lugares diferentes.

    Desligado aqui se resolve aqui. Desconectado pela Meta comeca no WhatsApp
    da loja, e clicar em "conectar" no nosso painel antes disso deixa o dono
    achando que resolveu (docs/whatsapp.md, 9.3).
    """
    if canal["disconnected_at"] is not None:
        motivo = canal["disconnect_reason"] or "sem motivo registrado"
        return f"DESCONECTADO PELA META em {canal['disconnected_at']:%d/%m/%Y} ({motivo})"
    return "DESLIGADO no painel"


def _utilizavel(canal: dict) -> bool:
    """A MESMA regra de `canal_utilizavel`, em cima do dicionario do SQL cru.

    Sao duas condicoes que nao se substituem: `is_active` e "eu nao
    desliguei", `disconnected_at` e "a Meta nao tirou o acesso".
    """
    return bool(canal["is_active"]) and canal["disconnected_at"] is None


def _canal_da_filial(canais: list[dict], branch_id) -> dict | None:
    for canal in canais:
        if canal["branch_id"] is not None and str(canal["branch_id"]) == str(branch_id):
            return canal
    return None


def _canal_do_restaurante(canais: list[dict]) -> dict | None:
    """A linha de queda: `branch_id` NULO, e so nulo significa isso."""
    for canal in canais:
        if canal["branch_id"] is None:
            return canal
    return None


def _canais_do_restaurante(db: Session, restaurante: Restaurante) -> list[dict]:
    """Todas as linhas, ATIVAS OU NAO.

    Filtrar pelas utilizaveis aqui apagaria justamente o achado: um numero
    desconectado que nao aparecesse na lista ficaria indistinguivel de um
    numero que nunca existiu, e as duas situacoes pedem coisas opostas do
    dono.
    """
    linhas = db.execute(text(
        """
        SELECT branch_id, display_phone_number, is_active,
               disconnected_at, disconnect_reason
          FROM whatsapp_channels
         WHERE restaurant_id = CAST(:id AS uuid)
        """
    ), {"id": restaurante.id}).mappings().all()
    return [dict(linha) for linha in linhas]


# ---------------------------------------------------------------------------
# Leituras compartilhadas
# ---------------------------------------------------------------------------


def _pior(atual: str, novo: str) -> str:
    """A situacao acumulada de varias filiais. So piora.

    Uma loja em ordem nao apaga o erro da outra — sem isto, o restaurante com
    a segunda loja sem nenhum setor sairia com OK global.
    """
    ordem = (OK, ATENCAO, ERRO)
    return atual if ordem.index(atual) >= ordem.index(novo) else novo


def _contar(db: Session, sql: str, restaurante: Restaurante) -> int:
    return _contar_por_id(db, sql, restaurante.id)


def _contar_por_id(db: Session, sql: str, identificador: str) -> int:
    """O mesmo `_contar`, com o id solto.

    Existe porque as contagens de cardapio passaram a ser por FILIAL, e a
    filial e um `dict` (vem de `_filiais_ativas`), nao um `Restaurante`.
    """
    return db.execute(text(sql), {"id": identificador}).scalar() or 0


def _filiais_ativas(db: Session, restaurante: Restaurante) -> list[dict]:
    """As filiais, ja com a operacao RESOLVIDA.

    `contingencia` sai do COALESCE porque e assim que
    `resolve_branch_operation` le: o valor da filial quando ela sobrescreveu,
    o do restaurante quando nao. Repetir a regra em SQL e o preco de esta
    conferencia rodar sem subir o app; se ela mudar la, muda aqui.
    """
    linhas = db.execute(text(
        """
        SELECT b.id,
               b.name,
               b.delivery_base_fee,
               b.delivery_fee_per_km,
               b.accepts_delivery,
               COALESCE(b.default_delivery_fee, s.default_delivery_fee) AS contingencia
          FROM branches AS b
          LEFT JOIN restaurant_settings AS s ON s.restaurant_id = b.restaurant_id
         WHERE b.restaurant_id = CAST(:id AS uuid) AND b.is_active IS TRUE
         ORDER BY b.is_main DESC NULLS LAST, b.name
        """
    ), {"id": restaurante.id}).mappings().all()
    return [dict(linha) for linha in linhas]


def buscar_restaurante(db: Session, slug: str) -> Restaurante | None:
    linha = db.execute(text(
        "SELECT id, slug, name FROM restaurants WHERE slug = :slug"
    ), {"slug": slug}).mappings().one_or_none()
    return Restaurante(str(linha["id"]), linha["slug"], linha["name"]) if linha else None


def listar_restaurantes_ativos(db: Session) -> list[Restaurante]:
    linhas = db.execute(text(
        "SELECT id, slug, name FROM restaurants WHERE is_active IS TRUE ORDER BY slug"
    )).mappings().all()
    return [Restaurante(str(l["id"]), l["slug"], l["name"]) for l in linhas]


# ---------------------------------------------------------------------------
# Saida
# ---------------------------------------------------------------------------


def conferir_tudo(db: Session, restaurante: Restaurante) -> list[Conferencia]:
    """Na ordem do onboarding, nao na ordem de gravidade: e assim que quem
    esta instalando vai andar pela lista."""
    return [
        conferir_horarios(db, restaurante),
        conferir_taxa_de_entrega(db, restaurante),
        conferir_setores_de_impressao(db, restaurante),
        conferir_webhook_do_pagamento(db, restaurante),
        conferir_comissao(db, restaurante),
        conferir_canal_de_whatsapp(db, restaurante),
    ]


def imprimir(restaurante: Restaurante, conferencias: list[Conferencia]) -> None:
    print()
    print("=" * 76)
    print(f"{restaurante.name}  ({restaurante.slug})")
    print("=" * 76)

    for conferencia in conferencias:
        print()
        print(f"[{conferencia.situacao:<7}] {conferencia.passo:<4} {conferencia.titulo}")
        for linha in conferencia.linhas:
            print(linha)
        if conferencia.situacao != OK and conferencia.o_que_fazer:
            print(f"  -> {conferencia.o_que_fazer}")

    print()
    print("-" * 76)
    print(f"  {_veredito(conferencias)}")
    print("-" * 76)


def _veredito(conferencias: list[Conferencia]) -> str:
    erros = [c for c in conferencias if c.situacao == ERRO]
    if erros:
        passos = ", ".join(c.passo for c in erros)
        return f"NAO ESTA PRONTO. Resolva os passos {passos} antes do primeiro pedido."

    atencoes = [c for c in conferencias if c.situacao == ATENCAO]
    if atencoes:
        passos = ", ".join(c.passo for c in atencoes)
        return f"Nenhum erro. Confirme com o lojista os passos {passos} - podem ser decisao dele."

    return "Pronto: os cinco silenciosos estao cobertos, e o WhatsApp esta no ar."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slug", nargs="?", help="slug do restaurante")
    parser.add_argument("--todos", action="store_true",
                        help="confere todos os restaurantes ativos")
    argumentos = parser.parse_args()

    if not argumentos.slug and not argumentos.todos:
        parser.error("informe o slug do restaurante, ou --todos")

    db = SessionLocal()
    try:
        if argumentos.todos:
            restaurantes = listar_restaurantes_ativos(db)
        else:
            encontrado = buscar_restaurante(db, argumentos.slug)
            if encontrado is None:
                print(f"Nenhum restaurante com slug '{argumentos.slug}'.", file=sys.stderr)
                return 2
            restaurantes = [encontrado]

        houve_erro = False
        for restaurante in restaurantes:
            conferencias = conferir_tudo(db, restaurante)
            imprimir(restaurante, conferencias)
            houve_erro = houve_erro or any(c.situacao == ERRO for c in conferencias)
    finally:
        db.close()

    return 1 if houve_erro else 0


if __name__ == "__main__":
    raise SystemExit(main())
