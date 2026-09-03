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

## As TRES formas, e so uma delas e a armadilha

O varredor nao tenta distinguir: distinguir exige entender o fluxo, e um
varredor que erra a classificacao e pior que um que reporta tudo. Quem
distingue e a pessoa, uma vez, e o resultado fica escrito em `ESPERADOS`:

| Forma | Exemplo | Para onde cai o valor NOVO |
|---|---|---|
| **guarda invertida** | `if x != A: return` | fora da acao — equivale a `if x == A: agir`. Fecha |
| **negacao completa** | `if x not in (todos os valores)` | recusado. Fecha |
| **filtro de conjunto** | `WHERE x != A`, `x.notin_(...)` | **do lado permissivo. E a armadilha 47** |

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
    "src/repositories/coupon_repository.py:Order.status.notin_(('cancelled', 'rejected'))": (
        "FILTRO DE CONJUNTO, e e divida aberta. E o 'ja comprou aqui?' do "
        "cupom de primeira compra: status NOVO passa a contar como compra "
        "valida, e o cliente perde o cupom. Cai do lado restritivo para o "
        "DINHEIRO (ninguem ganha desconto a mais), e por isso nao foi "
        "trocado por lista positiva — a lista dos status que 'valem como "
        "compra' nao existe hoje, e cria-la e decisao de produto. Status "
        "novo em `ORDER_STATUSES` tem que passar por aqui"
    ),
    "src/repositories/coupon_repository.py:redemption.status != 'applied'": (
        "GUARDA INVERTIDA: `!= 'applied': return` e `== 'applied': estornar` "
        "escrito ao contrario. Status novo de redencao nao e estornado, que e "
        "o lado que fecha"
    ),
    "src/repositories/order_repository.py:Order.payment_status != 'refunded'": (
        "FILTRO DE CONJUNTO, e e a divida mais cara da lista. E o WHERE do "
        "FATURAMENTO: `payment_status` novo nasce FATURAVEL, e a plataforma "
        "cobra comissao sobre um estado que ninguem classificou. A forma "
        "positiva existe (listar os que faturam), mas trocar por ela move a "
        "base da comissao — decisao de produto, nao refatoracao. E a mesma "
        "familia da armadilha 48 ('`== \"failed\"`, nunca `!= \"paid\"`'), "
        "pelo lado do extrato"
    ),
    "src/schemas/coupon_schema.py:self.discount_type != 'percent'": (
        "GUARDA INVERTIDA: `max_discount_amount` so e aceito no percentual. "
        "Tipo de desconto novo nasce SEM poder ter teto, que e o lado que "
        "fecha — o banco recusaria de qualquer jeito"
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

    def valor_de(self, no: ast.AST) -> str | None:
        """O literal que este no representa, se der para saber."""
        if isinstance(no, ast.Constant) and isinstance(no.value, str):
            return no.value
        if isinstance(no, ast.Name):
            return self.constantes.get(no.id)
        if isinstance(no, ast.Attribute):
            return self.membros_de_enum.get(ast.unparse(no))
        return None

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
                    valor = fechados.valor_de(comparado)
                    if valor is None:
                        continue
                    conjuntos = fechados.conjuntos_com(valor)
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
