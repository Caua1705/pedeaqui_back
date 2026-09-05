"""Filtro por EXCLUSAO sobre coluna de enum — a armadilha 47, no portao.

A armadilha 47 diz, com o caso concreto:

    Quando um booleano vira enum de tres valores, todo filtro que era
    `== True` vira `== <o valor especifico>`, e nunca `!= <o outro>`. A
    negacao era equivalente quando havia dois valores; ela deixa de ser no
    minuto em que entra o terceiro, e o novo valor cai silenciosamente do
    lado permissivo.

`restaurant_coupons.is_public` virou `visibility` com tres valores, e
`visibility != 'private'` — a traducao literal do booleano — publicava os
cupons de SEGMENTO na vitrine anonima do cardapio, com codigo. Sem erro, sem
log, e o lojista descobrindo pela receita.

## Por que uma ferramenta

Aquele caso foi achado por leitura humana, no minuto em que alguem estava
olhando. Depois dele a rodada acrescentou **dois estados** a
`CustomerCouponState` e mexeu na lista de formas de pagamento — e nada alem
de leitura humana garantia que nenhum `!=` daquela familia tivesse ficado
para tras. Lista escrita a mao envelhece calada; varredura no portao, nao.

## O criterio: o VALOR comparado pertence a um conjunto fechado

Nao e "achou um `!=`" — sao 85 no `src/`, e a esmagadora maioria compara id,
senha, hash ou tamanho. O que interessa e a negacao sobre um valor de
**conjunto fechado**, e conjunto fechado e o que esta DECLARADO em algum
lugar do repositorio:

1. `alembic/schema_baseline.sql` — os `CHECK (col = ANY (ARRAY[...]))` do
   `pg_dump` de producao. E a verdade do banco para tudo que nasceu antes da
   revisao 0012;
2. `alembic/versions/*.py` — os CHECKs das revisoes seguintes (`IN (...)` ou
   `= ANY (ARRAY[...])`), que e por onde entram as colunas novas;
3. `src/models/*.py` — os `CheckConstraint("col IN (...)")` do ORM;
4. `src/**` — as classes `str, Enum` (que sao contrato do `/openapi.json`) e
   as tuplas MAIUSCULAS de literais no nivel do modulo (`ORDER_STATUSES`,
   `PAYMENT_METHODS`, `COUPON_VISIBILITIES`).

**Conjunto que nao esta declarado em lugar nenhum e invisivel para este
varredor**, e isso e limite conhecido, nao descuido: nao ha como saber que
`"applied"` e um de dois valores possiveis se ninguem escreveu os dois. O
conserto, quando aparecer, e declarar o conjunto — nao adivinhar aqui.

O outro lado da comparacao e resolvido em tres formas: literal (`!= 'paid'`),
constante de um valor (`!= PAPEL_DE_MAQUINA`) e **constante de TUPLA**
(`.notin_(NON_BILLABLE_ORDER_STATUSES)`). A terceira entrou depois da
primeira rodada, e ela era um ponto cego caro: `billable_order_conditions`
tinha as duas metades do mesmo defeito lado a lado, e so a que comparava com
um literal aparecia. A que ficava escondida era a do STATUS do pedido — a
comissao da plataforma, calculada por exclusao.

O que continua invisivel e o **parametro**: `.notin_(exclude_statuses)`, com o
conjunto vindo de quem chama, nao da para resolver sem seguir a cadeia de
chamadas. Sao dois sitios em `courier_repository`, e os dois recebem
`TERMINAL_ORDER_STATUSES` do service de proposito (quem sabe o que e terminal
e a maquina de estados).

## As TRES formas, e so uma delas e a armadilha

O varredor nao tenta distinguir: distinguir exige entender o fluxo, e um
varredor que erra a classificacao e pior que um que reporta tudo. Quem
distingue e a pessoa, uma vez, e o resultado fica escrito em `ESPERADOS`:

| Forma | Exemplo | Para onde cai o valor NOVO |
|---|---|---|
| **guarda invertida** | `if x != A: return` | fora da acao — equivale a `if x == A: agir`. Fecha |
| **negacao completa** | `if x not in (todos os valores)` | recusado. Fecha |
| **negacao de permitidos** | `if x not in PERMITIDOS: recusar` | fora da lista, recusado. Fecha |
| **filtro de conjunto** | `WHERE x != A`, `.notin_(EXCLUIDOS)` | **do lado permissivo. E a armadilha 47** |

As duas do meio se parecem e nao sao a mesma: **o que decide e se a lista
negada e a dos PERMITIDOS ou a dos EXCLUIDOS.** Negar a lista de permitidos e
falha fechada — o valor novo cai fora dela e e recusado. Negar a lista de
excluidos e a armadilha — o valor novo cai fora dela e e ACEITO. As duas se
escrevem `not in`, e e por isso que ler `not in` e concluir alguma coisa nao
funciona: `payment_status not in PAYABLE_STATUSES` e seguro e
`status.notin_(NON_BILLABLE_ORDER_STATUSES)` era a comissao calculada por
exclusao, e as duas linhas tem a mesma cara.

`ESPERADOS` guarda a chave `arquivo:expressao` e o motivo — e o motivo tem
que dizer **para onde cai o valor novo**, que e a pergunta que a armadilha
faz. Sitio novo fora da lista e achado; entrada da lista que sumir do codigo
tambem, para ela nao virar cemiterio.

A chave e a EXPRESSAO e nao a linha: linha muda com qualquer edicao acima, e
uma lista que precisa ser renumerada a cada commit e uma lista que se aprende
a ignorar.

    python scripts/filtros_por_exclusao.py
    python scripts/filtros_por_exclusao.py --tudo

Somente leitura: le codigo-fonte. Nao abre banco.
"""

import argparse
import ast
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

# `col IN ('a', 'b')` — a forma que o ORM e as revisoes escrevem.
RE_IN = re.compile(r"([A-Za-z_][\w.]*)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
# `col = ANY (ARRAY['a'::text, 'b'::text])` — a forma que o `pg_dump` produz.
RE_ANY = re.compile(r"([A-Za-z_][\w.]*)\s*=\s*ANY\s*\(\s*ARRAY\[([^\]]*)\]", re.IGNORECASE)
RE_LITERAL = re.compile(r"'([^']*)'")

# Metodos do SQLAlchemy que sao o `NOT IN` do SQL.
METODOS_DE_EXCLUSAO = ("notin_", "not_in")

# Conjunto de UM valor nao e enum: e uma constante. Exigir dois evita que
# `CHECK (status IN ('ok'))` vire conjunto e transforme todo `!= 'ok'` do
# repositorio em achado.
MINIMO_DE_VALORES = 2


# Cada chave e `arquivo:expressao`; cada motivo diz PARA ONDE CAI O VALOR
# NOVO, que e a pergunta da armadilha 47.
ESPERADOS = {
    "src/core/startup_checks.py:settings.MERCADOPAGO_ENVIRONMENT not in ('test', 'production')": (
        "NEGACAO COMPLETA: a tupla e o conjunto inteiro. Ambiente novo nao "
        "reconhecido derruba o boot com a lista dos que valem, que e o "
        "comportamento certo — a variavel escolhe qual credencial e usada"
    ),
    "src/repositories/coupon_repository.py:redemption.status != 'applied'": (
        "GUARDA INVERTIDA: `!= 'applied': return` e `== 'applied': estornar` "
        "escrito ao contrario. Status novo de redencao nao e estornado, que e "
        "o lado que fecha"
    ),
    "src/schemas/coupon_schema.py:self.discount_type != 'percent'": (
        "GUARDA INVERTIDA: `max_discount_amount` so e aceito no percentual. "
        "Tipo de desconto novo nasce SEM poder ter teto, que e o lado que "
        "fecha — o banco recusaria de qualquer jeito"
    ),
    "src/repositories/admin_report_repository.py:Order.order_type != 'delivery'": (
        "NEGACAO COMPLETA em forma de complemento: e a CONTAGEM do que ficou "
        "de fora de /reports/neighborhoods, que so lista entrega. Tipo de "
        "pedido novo cai aqui dentro, e e o lado que fecha — este numero "
        "existe para a diferenca entre esta tela e /reports/summary ser "
        "explicavel, e um tipo fora dos dois lados reabriria o buraco. Por "
        "isso o campo chama `non_delivery_orders_count` e nao `pickup`"
    ),
    "src/services/admin_courier_service.py:order.order_type != 'delivery'": (
        "GUARDA INVERTIDA: tipo de pedido novo nao e atribuivel a entregador "
        "(`not_delivery`). Retirada nao tem motoboy, e um tipo novo tambem "
        "nao tem ate alguem decidir que tem"
    ),
    "src/services/admin_user_service.py:admin_user.role != PAPEL_DE_DONO": (
        "GUARDA INVERTIDA: so o DONO e protegido contra ficar sem substituto. "
        "Papel novo nao entra na protecao, que e o certo — a regra e sobre o "
        "dono, nominalmente"
    ),
    "src/services/admin_user_service.py:changes.get('role', PAPEL_DE_DONO) != PAPEL_DE_DONO": (
        "GUARDA INVERTIDA em forma de booleano ('esta deixando de ser "
        "dono?'). Papel novo conta como "
        "rebaixamento e ATIVA a protecao do ultimo dono — o lado que fecha"
    ),
    "src/services/whatsapp_webhook_service.py:atualizacao.status not in WHATSAPP_MESSAGE_STATUSES": (
        "NEGACAO DE PERMITIDOS: a lista e a dos status que NOS temos, e o "
        "que cai fora dela e ignorado. O conjunto que cresce sozinho aqui e "
        "o DELES — a Meta tem `deleted` e `warning`, que nao decidem nada "
        "nosso. Status novo e recusado, que e o lado que fecha: grava-lo "
        "morreria no CHECK e derrubaria o POST inteiro"
    ),
    "src/services/order_review_service.py:order.status != REVIEWABLE_ORDER_STATUS": (
        "GUARDA INVERTIDA: so pedido `completed` e avaliavel. Status novo nao "
        "e avaliavel ate alguem decidir que e"
    ),
    "src/services/order_service.py:payload.order_type != 'delivery'": (
        "GUARDA INVERTIDA em dois sitios (endereco obrigatorio e estimativa "
        "de entrega). **Atencao ao acrescentar tipo a `ORDER_TYPES`**: um "
        "tipo novo nasce sem endereco obrigatorio e SEM taxa de entrega, e o "
        "segundo e dinheiro. Os dois passam por aqui"
    ),
    "src/services/order_state_machine.py:order_type != 'delivery'": (
        "GUARDA INVERTIDA: so entrega sai para entrega. Tipo novo nao alcanca "
        "`out_for_delivery`, que e o lado que fecha"
    ),
    "src/services/order_state_machine.py:new_status != 'cancelled'": (
        "GUARDA INVERTIDA: so o cancelamento pede segundo clique. Status novo "
        "nao pede confirmacao — e `rejected` ja esta de fora de proposito, "
        "porque o grafo so o alcanca a partir de `pending`"
    ),
    "src/services/payment_refund_service.py:order.payment_flow != 'online'": (
        "GUARDA INVERTIDA: so cobranca online tem o que estornar. Fluxo novo "
        "e tratado como pago na entrega — nao ha chamada ao gateway, que e o "
        "lado que fecha"
    ),
    "src/services/payment_refund_service.py:local_status != 'paid'": (
        "GUARDA INVERTIDA: grava o `paid` intermediario quando o gateway "
        "aprovou entre o cancelamento e agora (armadilha 25). Estado novo "
        "diferente de `paid` continua ganhando esse passo, que e o certo — o "
        "que se quer evitar e gravar `paid` sobre `paid`"
    ),
    "src/services/payment_service.py:order.payment_flow != 'online'": (
        "GUARDA INVERTIDA: so pedido de fluxo online recebe cobranca. Fluxo "
        "novo nao abre cobranca ate alguem decidir que abre"
    ),
    "src/services/payment_service.py:payment_method != 'pix'": (
        "GUARDA INVERTIDA: so o pix tem QR com prazo. Forma de pagamento nova "
        "responde `None`, que e 'nao tem o que expirar' — o lado que fecha"
    ),
    "src/services/payment_service.py:payment_status != 'paid'": (
        "GUARDA INVERTIDA no estorno automatico do webhook: so o que foi PAGO "
        "e devolvido. Estado novo nao dispara estorno — o dinheiro fica onde "
        "esta e a varredura de `list_orders_awaiting_refund` e quem o alcanca"
    ),
    "src/services/print_layout.py:order.order_type != 'delivery'": (
        "GUARDA INVERTIDA na comanda: o que nao e entrega imprime 'RETIRADA "
        "NO BALCAO'. Tipo novo imprimiria isso — errado, mas VISIVEL na via, "
        "que e o unico lugar desta lista onde o erro aparece sozinho"
    ),
    "src/integrations/payment_gateway.py:payment_method not in MERCADOPAGO_SUPPORTED_PAYMENT_METHODS": (
        "NEGACAO DE PERMITIDOS: forma de pagamento nova nao e suportada pelo "
        "gateway ate alguem a acrescentar. Recusa antes da chamada, que e o "
        "lado que fecha"
    ),
    "src/integrations/payment_gateway.py:payment_status not in PAYMENT_STATUSES": (
        "NEGACAO COMPLETA: e a traducao do status do gateway para o nosso "
        "conjunto. Estado que nao reconhecemos nao e gravado em `orders`, e o "
        "CHECK da coluna recusaria de qualquer jeito"
    ),
    "src/schemas/coupon_schema.py:forma not in PAYMENT_METHODS": (
        "NEGACAO COMPLETA: valida `allowed_payment_methods` do cupom contra a "
        "lista inteira. Forma nova so e aceita depois de entrar em "
        "`PAYMENT_METHODS` — que e a armadilha 15, e ela quer exatamente isso"
    ),
    "src/services/admin_order_service.py:order_status not in ORDER_STATUSES": (
        "NEGACAO COMPLETA: valida o status contra o conjunto inteiro antes de "
        "a maquina de estados decidir a transicao"
    ),
    "src/services/admin_order_service.py:payload.status not in ORDER_STATUSES": (
        "NEGACAO COMPLETA, par da de cima: o status que o painel manda no "
        "corpo tem que existir"
    ),
    "src/services/admin_printing_service.py:order.payment_status not in PAYMENT_STATUSES_THAT_RELEASE_ORDER": (
        "NEGACAO DE PERMITIDOS: a via de PRODUCAO nao sai. Estado de "
        "pagamento novo nao libera a cozinha ate entrar na lista — armadilha "
        "13, e o lado que fecha e a praca nao preparar comida nao paga"
    ),
    "src/services/admin_user_service.py:admin_user.role not in PAPEIS_DE_PESSOA": (
        "NEGACAO DE PERMITIDOS: 404 para quem nao e pessoa. Papel novo nao e "
        "editavel pelas rotas de equipe ate alguem o declarar como cargo de "
        "gente. E o par do `role.in_(PAPEIS_DE_PESSOA)` do repositorio: sem "
        "ele, o que sumiu da lista voltaria pela edicao por id"
    ),
    "src/services/coupon_service.py:order_type not in ORDER_TYPES": (
        "NEGACAO COMPLETA: valida o tipo de pedido contra o conjunto inteiro "
        "na avaliacao do cupom"
    ),
    "src/services/coupon_service.py:payload.order_type not in ORDER_TYPES": (
        "NEGACAO COMPLETA, par da de cima, no corpo da requisicao"
    ),
    "src/services/coupon_service.py:payment_method not in PAYMENT_METHODS": (
        "NEGACAO COMPLETA: a forma de pagamento que o cliente manda para "
        "avaliar o cupom tem que existir"
    ),
    "src/services/order_service.py:order_type not in ORDER_TYPES": (
        "NEGACAO COMPLETA na criacao do pedido"
    ),
    "src/services/order_service.py:payment_method not in PAYMENT_METHODS": (
        "NEGACAO COMPLETA na criacao do pedido. E a metade da armadilha 15 "
        "que recusa `payment_method='banana'`"
    ),
    "src/services/order_state_machine.py:new_status not in KITCHEN_ORDER_STATUSES": (
        "NEGACAO DE PERMITIDOS, e a UNICA da lista que merece aviso: status "
        "novo escapa da regra de 'pagamento online libera o pedido'. Se o "
        "status novo mandar o pedido para a COZINHA, ele tem que entrar em "
        "`KITCHEN_ORDER_STATUSES` no mesmo commit — senao o pedido vai para a "
        "praca com o pix em aberto"
    ),
    "src/services/payment_refund_service.py:order.payment_status not in PAYMENT_STATUSES_WITH_LIVE_CHARGE": (
        "NEGACAO DE PERMITIDOS: sem cobranca viva no gateway nao ha o que "
        "devolver. Estado novo nao dispara chamada ao gateway; se ele for um "
        "estado com dinheiro retido, entra na lista junto"
    ),
    "src/services/payment_refund_service.py:order.status not in NON_BILLABLE_ORDER_STATUSES": (
        "GUARDA INVERTIDA: a acao (estornar) so acontece PARA `cancelled` e "
        "`rejected`. Status novo nao dispara estorno automatico — e "
        "`completed` esta fora de proposito, porque e o unico terminal em que "
        "HOUVE venda (armadilha 25)"
    ),
    "src/services/payment_service.py:order.payment_status not in PAYABLE_STATUSES": (
        "NEGACAO DE PERMITIDOS: estado novo nao recebe cobranca ate alguem "
        "decidir que recebe. `failed` esta dentro de proposito — e o 'tente "
        "outro cartao' da armadilha 48"
    ),
}


class Achado:
    def __init__(self, arquivo: str, expressao: str, linhas: list[int], conjuntos: list[str]):
        self.arquivo = arquivo
        self.expressao = expressao
        self.linhas = linhas
        self.conjuntos = conjuntos

    @property
    def chave(self) -> str:
        return f"{self.arquivo}:{self.expressao}"

    def __str__(self) -> str:
        onde = ", ".join(str(linha) for linha in self.linhas)
        return f"{self.arquivo}:{onde}\n      {self.expressao}\n      conjunto: {self.conjuntos[0]}"


def _valores_do_texto(texto: str) -> list[tuple[str, set[str]]]:
    """(coluna, valores) de todo `IN (...)` e `= ANY (ARRAY[...])` do texto."""
    encontrados = []
    for regex in (RE_IN, RE_ANY):
        for coluna, lista in regex.findall(texto):
            valores = set(RE_LITERAL.findall(lista))
            if len(valores) >= MINIMO_DE_VALORES:
                encontrados.append((coluna, valores))
    return encontrados


def _arquivos_python(raiz: Path, diretorio: str):
    alvo = raiz / diretorio
    if not alvo.exists():
        return
    for arquivo in sorted(alvo.rglob("*.py")):
        if "__pycache__" not in arquivo.parts:
            yield arquivo


def _tupla_de_literais(no: ast.AST, constantes: dict[str, str]) -> set[str] | None:
    """Os valores de `("a", "b")` / `["a"]` / `frozenset({...})`, ou `None`.

    `Name` e resolvido pelas constantes de UM literal ja vistas — e o que faz
    `COUPON_VISIBILITIES = (COUPON_VISIBILITY_PUBLIC, ...)` virar conjunto em
    vez de ser descartado por nao ser literal.
    """
    elementos = None
    if isinstance(no, (ast.Tuple, ast.List, ast.Set)):
        elementos = no.elts
    elif isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id == "frozenset":
        if no.args and isinstance(no.args[0], (ast.Tuple, ast.List, ast.Set)):
            elementos = no.args[0].elts
    if elementos is None:
        return None

    valores = set()
    for elemento in elementos:
        if isinstance(elemento, ast.Constant) and isinstance(elemento.value, str):
            valores.add(elemento.value)
        elif isinstance(elemento, ast.Name) and elemento.id in constantes:
            valores.add(constantes[elemento.id])
        else:
            # Um elemento que nao da para resolver invalida o conjunto: meio
            # conjunto responderia "nao pertence" para valor que pertence.
            return None
    return valores if len(valores) >= MINIMO_DE_VALORES else None


class ConjuntosFechados:
    """Os valores de cada coluna de enum, lidos de onde eles sao DECLARADOS.

    `constantes` e `membros_de_enum` existem para resolver o outro lado da
    comparacao: `!= PAPEL_DE_MAQUINA` e `!= CustomerCouponState.APPLICABLE`
    sao a mesma pergunta que `!= 'print_agent'`.
    """

    def __init__(self, raiz: Path):
        self.raiz = raiz
        self.por_nome: dict[str, set[str]] = {}
        self.constantes: dict[str, str] = {}
        self.tuplas: dict[str, set[str]] = {}
        self.membros_de_enum: dict[str, str] = {}
        self._ler_sql_da_baseline()
        self._ler_revisoes()
        self._ler_fontes()
        self.por_valor: dict[str, list[str]] = {}
        for nome, valores in self.por_nome.items():
            for valor in valores:
                self.por_valor.setdefault(valor, []).append(nome)

    def _guardar(self, origem: str, texto: str) -> None:
        for coluna, valores in _valores_do_texto(texto):
            self.por_nome.setdefault(f"{origem}:{coluna}", set()).update(valores)

    def _ler_sql_da_baseline(self) -> None:
        baseline = self.raiz / "alembic" / "schema_baseline.sql"
        if baseline.exists():
            self._guardar("baseline", baseline.read_text(encoding="utf-8"))

    def _ler_revisoes(self) -> None:
        versoes = self.raiz / "alembic" / "versions"
        if not versoes.exists():
            return
        for arquivo in sorted(versoes.glob("*.py")):
            self._guardar(f"revisao {arquivo.stem[:13]}", arquivo.read_text(encoding="utf-8"))

    def _ler_fontes(self) -> None:
        for arquivo in _arquivos_python(self.raiz, "src"):
            relativo = arquivo.relative_to(self.raiz).as_posix()
            arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
            self._ler_checks_do_orm(relativo, arvore)
            self._ler_constantes_do_modulo(relativo, arvore)
            self._ler_enums(relativo, arvore)

    def _ler_checks_do_orm(self, relativo: str, arvore: ast.Module) -> None:
        for no in ast.walk(arvore):
            if isinstance(no, ast.Constant) and isinstance(no.value, str):
                self._guardar(f"model {relativo}", no.value)

    def _ler_constantes_do_modulo(self, relativo: str, arvore: ast.Module) -> None:
        for no in arvore.body:
            if not isinstance(no, ast.Assign) or len(no.targets) != 1:
                continue
            if not isinstance(no.targets[0], ast.Name):
                continue
            nome = no.targets[0].id
            if not nome.isupper():
                continue
            if isinstance(no.value, ast.Constant) and isinstance(no.value.value, str):
                self.constantes[nome] = no.value.value
                continue
            valores = _tupla_de_literais(no.value, self.constantes)
            if valores is not None:
                self.por_nome[f"{relativo}:{nome}"] = valores
                # Pelo nome NU, para resolver `.notin_(NON_BILLABLE_...)` do
                # outro lado da comparacao. Nome repetido entre modulos so
                # amplia o que o varredor enxerga, nunca o contrario.
                self.tuplas.setdefault(nome, set()).update(valores)

    def _ler_enums(self, relativo: str, arvore: ast.Module) -> None:
        for no in ast.walk(arvore):
            if not isinstance(no, ast.ClassDef):
                continue
            if not any("Enum" in ast.unparse(base) for base in no.bases):
                continue
            valores = set()
            for corpo in no.body:
                if not isinstance(corpo, ast.Assign) or len(corpo.targets) != 1:
                    continue
                alvo, valor = corpo.targets[0], corpo.value
                if not isinstance(alvo, ast.Name) or not isinstance(valor, ast.Constant):
                    continue
                if not isinstance(valor.value, str):
                    continue
                valores.add(valor.value)
                self.membros_de_enum[f"{no.name}.{alvo.id}"] = valor.value
            if len(valores) >= MINIMO_DE_VALORES:
                self.por_nome[f"{relativo}:{no.name}"] = valores

    def valores_de(self, no: ast.AST) -> list[str]:
        """Os literais que este no representa, se der para saber.

        Lista e nao valor unico por causa da terceira forma: o nome de uma
        tupla constante (`NON_BILLABLE_ORDER_STATUSES`) representa varios
        valores de uma vez, e e a forma em que o defeito mais caro estava.
        """
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            return [no.value]
        if isinstance(no, ast.Name):
            if no.id in self.constantes:
                return [self.constantes[no.id]]
            return sorted(self.tuplas.get(no.id, ()))
        if isinstance(no, ast.Attribute):
            membro = self.membros_de_enum.get(ast.unparse(no))
            return [membro] if membro is not None else []
        return []

    def conjuntos_com(self, valor: str) -> list[str]:
        return self.por_valor.get(valor, [])


def _comparados_por_exclusao(no: ast.AST) -> list[ast.AST]:
    """Os nos que estao do lado DIREITO de uma negacao."""
    if isinstance(no, ast.Compare):
        return [
            comparado
            for operador, comparado in zip(no.ops, no.comparators)
            if isinstance(operador, (ast.NotEq, ast.NotIn))
        ]
    if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute):
        if no.func.attr in METODOS_DE_EXCLUSAO:
            return list(no.args)
    return []


def _valores_comparados(alvo: ast.AST) -> list[ast.AST]:
    """Um `not in ('a', 'b')` compara com dois valores, nao com uma tupla."""
    if isinstance(alvo, (ast.Tuple, ast.List, ast.Set)):
        return list(alvo.elts)
    return [alvo]


def auditar(raiz: Path | None = None) -> dict:
    """`raiz` existe para o teste montar uma arvore PLANTADA — mesmo motivo do
    `raiz` de `escrita_e_transacao` e de `estado_entre_workers`."""
    raiz = raiz or ROOT_DIR
    fechados = ConjuntosFechados(raiz)

    encontrados: dict[str, Achado] = {}
    for arquivo in _arquivos_python(raiz, "src"):
        relativo = arquivo.relative_to(raiz).as_posix()
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"), filename=str(arquivo))
        for no in ast.walk(arvore):
            for alvo in _comparados_por_exclusao(no):
                for comparado in _valores_comparados(alvo):
                    conjuntos = [
                        conjunto
                        for valor in fechados.valores_de(comparado)
                        for conjunto in fechados.conjuntos_com(valor)
                    ]
                    if not conjuntos:
                        continue
                    expressao = ast.unparse(no)
                    chave = f"{relativo}:{expressao}"
                    if chave in encontrados:
                        encontrados[chave].linhas.append(no.lineno)
                    else:
                        encontrados[chave] = Achado(relativo, expressao, [no.lineno], conjuntos)

    fora_da_lista = [
        achado for chave, achado in sorted(encontrados.items()) if chave not in ESPERADOS
    ]
    declarados_sumidos = [chave for chave in sorted(ESPERADOS) if chave not in encontrados]
    return {
        "achados": fora_da_lista,
        "declarados": declarados_sumidos,
        "conjuntos": fechados.por_nome,
        "revisados": len(encontrados) - len(fora_da_lista),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Acha filtro por exclusao sobre coluna de enum (somente leitura)."
    )
    parser.add_argument("--tudo", action="store_true", help="lista tambem o que esta em ESPERADOS")
    args = parser.parse_args()

    resultado = auditar()
    total = len(resultado["achados"]) + len(resultado["declarados"])

    print("=" * 78)
    print(
        f"{total} achado(s)  |  {resultado['revisados']} sitio(s) revisados  |  "
        f"{len(resultado['conjuntos'])} conjunto(s) fechado(s)"
    )
    print("=" * 78)

    print()
    print(f"## Negacao sobre valor de conjunto fechado, NAO declarada  ({len(resultado['achados'])})")
    print()
    if not resultado["achados"]:
        print("  Nenhuma.")
    for achado in resultado["achados"]:
        print(f"  {achado}")
        print()

    print(f"## Declarado em ESPERADOS e nao encontrado no codigo (lista velha)  ({len(resultado['declarados'])})")
    print()
    if not resultado["declarados"]:
        print("  Nenhum.")
    for chave in resultado["declarados"]:
        print(f"  {chave}")

    if args.tudo:
        print()
        print("## Os sitios revisados, e para onde cai o valor NOVO em cada um")
        print()
        for chave, motivo in sorted(ESPERADOS.items()):
            print(f"  {chave}")
            print(f"      {motivo}")

    print()
    print("Negacao sobre enum nao e erro por si so: `if x != A: return` e uma")
    print("guarda invertida e fecha. O que a armadilha 47 cobra e que alguem")
    print("tenha respondido, por escrito, PARA ONDE CAI O VALOR NOVO.")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
