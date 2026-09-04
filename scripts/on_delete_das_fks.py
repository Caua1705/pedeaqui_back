"""Toda FK que APAGA ou ANULA sozinha esta declarada — e o que sobra e barreira.

O levantamento de 05/09/2026 (`docs/modelo-de-dados.md` secao 7) contou 84 FKs:
39 `NO ACTION`, 35 `CASCADE`, 8 `SET NULL`, 2 `RESTRICT`. O numero ficou escrito
num `.md` e ninguem o cobra — que e a forma como todo levantamento deste
repositorio envelhece.

## A assimetria que decide o que este portao olha

    NO ACTION / RESTRICT   o DELETE do pai FALHA         -> ALTO
    CASCADE                 o filho some junto            -> CALADO
    SET NULL                 a coluna do filho vira nula   -> CALADO

Errar para o lado `NO ACTION` da erro na cara de quem apagou, na hora. Errar
para o lado `CASCADE` da sucesso, e o dia em que doer e o dia em que ja apagou.
**Por isso este portao cobra as 43 destrutivas, e nao as 41 que barram.**

## O que ele faz

Le `pg_constraint` e cobra que toda FK `CASCADE` ou `SET NULL` esteja em
`DESTRUTIVAS`, dizendo **para onde vai a linha filha quando o pai morre**. FK
destrutiva que apareca e nao esteja na lista e achado; entrada na lista que
nao exista mais no banco tambem e, para a lista nao virar cemiterio.

**E isso cobre os dois estragos com um mecanismo so**, que e o motivo de nao
haver uma segunda lista de "barreiras":

- **tabela nova com `CASCADE`** — nasce fora da lista, vermelha;
- **barreira derrubada** — `order_items.product_id` e `NO ACTION` de proposito
  (o historico que o cliente ainda consulta, armadilha 13). No dia em que
  alguem a "arrumar" para `CASCADE` porque o produto esta sendo apagado de
  qualquer jeito, ela vira uma destrutiva **que nao esta na lista**, e cai aqui.
  A barreira nao precisa ser declarada: ela e o complemento.

## O fechamento, que e o relatorio e nao o portao

`--fechamento` responde, para as raizes que alguem poderia mesmo apagar
(`restaurants`, `branches`, `customers`, `orders`), tres coisas que a tabela
de contagens nao responde: **o que vai junto**, **o que fica com coluna nula**
e **o que hoje BARRA o DELETE**. E a leitura que a decisao pendente da secao 7
pede — quais tabelas operacionais e descartaveis deveriam mesmo cascatear —, e
ela nao cabe em prosa: e um fecho transitivo.

    venv/Scripts/python scripts/on_delete_das_fks.py --url postgresql+psycopg://... --fechamento

Somente leitura: consulta `pg_constraint`. Nao escreve nada.

## Onde ele nao chega

Ele le o banco de TESTE, o mesmo do `alembic upgrade head` do CI. FK criada a
mao no Supabase nao aparece — e a armadilha 33, e o que a fecha e a regra de so
mudar schema por revisao.

E ele nao sabe se a regra esta CERTA. Ele sabe se ela foi ESCRITA por alguem.
A diferenca e a mesma de `filtros_por_exclusao.py`: classificar exige entender
o fluxo, e um varredor que erra a classificacao e pior que um que reporta tudo.
"""

import argparse

from sqlalchemy import create_engine, text


# Como o Postgres guarda a regra em `pg_constraint.confdeltype`.
REGRA = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

# As duas que agem sozinhas. `RESTRICT` fica de fora de proposito: ele BARRA,
# como o `NO ACTION`, e a unica diferenca entre os dois e o momento em que o
# Postgres avalia (RESTRICT nao adia para o fim da transacao). Nenhum dos dois
# apaga nada.
DESTRUTIVAS_REGRAS = {"CASCADE", "SET NULL"}

# As raizes do fechamento: as tabelas em que um DELETE e um caminho concebivel
# de operacao. `orders` esta na lista mesmo nunca sendo apagado hoje — e
# justamente por isso que vale saber o que sairia junto se um dia for.
RAIZES = ("restaurants", "branches", "customers", "orders")


# constraint -> para onde vai a linha filha quando o pai morre.
#
# Toda FK CASCADE ou SET NULL do banco entra aqui. Uma que apareca e nao esteja
# na lista e a cascata nova que ninguem conferiu — inclusive a que nasce de uma
# barreira derrubada.
DESTRUTIVAS: dict[str, str] = {
    # --- filhas do RESTAURANTE: configuracao, nao historico -----------------
    #
    # Nenhuma delas significa coisa alguma sem o restaurante, e nenhuma e
    # consultada depois. O DELETE do restaurante nao acontece hoje: ele bate em
    # `orders_restaurant_id_fkey` no primeiro pedido ja feito.
    "branches_restaurant_id_fkey": "a loja e do restaurante; sem ele nao ha o que operar",
    "admin_users_restaurant_id_fkey": "conta de lojista so existe dentro do restaurante dela",
    "admin_error_reports_restaurant_id_fkey": "relatorio de erro do painel daquele restaurante",
    "restaurant_settings_restaurant_id_fkey": "a linha 1-para-1 de configuracao",
    "restaurant_banners_restaurant_id_fkey": "hero e destaque da vitrine",
    "restaurant_payment_credentials_restaurant_id_fkey": "credencial do gateway daquele lojista",
    "restaurant_coupons_restaurant_id_fkey": (
        "a campanha e do restaurante. Nao chega a rodar: "
        "`coupon_redemptions_coupon_id_fkey` e RESTRICT e barra antes"
    ),
    "cashback_rules_restaurant_id_fkey": "a regra de cashback e do restaurante",
    "categories_restaurant_id_fkey": "o cardapio e da loja, e a loja e do restaurante",
    "products_restaurant_id_fkey": (
        "idem. Nao chega a rodar com pedido feito: "
        "`order_items_product_id_fkey` e NO ACTION e barra"
    ),
    "customer_payment_profiles_restaurant_id_fkey": (
        "o perfil de pagamento e por (cliente, restaurante) — sem o "
        "restaurante ele nao aponta para conta nenhuma do gateway"
    ),
    "whatsapp_channels_restaurant_id_fkey": "o numero e da rede daquele lojista",
    # --- filhas da FILIAL: o dia a dia de uma loja --------------------------
    "branch_business_hours_branch_id_fkey": "a agenda da semana daquela loja",
    "branch_delivery_time_bands_branch_id_fkey": "as faixas de prazo daquela loja",
    "branch_payment_methods_branch_id_fkey": "o que aquela loja aceita receber",
    "cashback_rules_branch_id_fkey": "a regra sobrescrita por filial",
    "fk_whatsapp_channels_branch": "o numero daquela loja",
    # --- filhas de um AGREGADO: a linha nao existe fora do pai --------------
    #
    # Sao as cascatas que ninguem discute: a linha e parte do pai, nao um
    # registro proprio. Removidas do pai, elas viram lixo referencial.
    "cashback_rule_weekdays_rule_id_fkey": "os dias da semana SAO a regra",
    "customer_saved_cards_payment_profile_id_fkey": "o cartao salvo vive dentro do perfil",
    "product_option_groups_product_id_fkey": "o grupo de adicionais e do produto",
    "product_options_option_group_id_fkey": "a opcao e do grupo",
    "products_category_id_fkey": "produto sem categoria nao aparece no cardapio",
    "fk_products_categoria_da_mesma_filial": (
        "a metade COMPOSTA da de cima — as duas descrevem a mesma aresta, e e "
        "por isso que as duas sao CASCADE. Se so uma fosse, a outra barraria"
    ),
    "whatsapp_contact_windows_channel_id_fkey": (
        "a janela de 24h e estado do canal, nao registro: sem o canal ela nao "
        "autoriza nada. Nao confundir com `whatsapp_messages`, que e NO ACTION"
    ),
    "order_items_order_id_fkey": "o item E o pedido; nao ha item avulso",
    "order_item_options_order_item_id_fkey": "o adicional e do item",
    "order_status_history_order_id_fkey": "a linha do tempo e do pedido",
    # --- filhas do CLIENTE --------------------------------------------------
    #
    # A exclusao de conta neste sistema e ANONIMIZACAO, nao DELETE
    # (`customer_anonymization_service`). Estas cascatas existem para o
    # `DELETE` que nao acontece — e `orders_customer_id_fkey`, que e NO ACTION,
    # e o que impede alguem de trocar um mecanismo pelo outro sem perceber.
    "customer_addresses_customer_id_fkey": "endereco salvo da conta",
    "customer_social_identities_customer_id_fkey": "o vinculo com Google/Apple",
    "email_verification_codes_customer_id_fkey": "codigo de uso unico, com prazo",
    "password_reset_codes_customer_id_fkey": "idem",
    "customer_payment_profiles_customer_id_fkey": "o perfil no gateway e da pessoa",
    "cashback_transactions_customer_id_fkey": (
        "**e a unica cascata do cliente que leva DINHEIRO.** Hoje ela nao roda: "
        "`orders_customer_id_fkey` (NO ACTION) e `coupon_redemptions_customer_id_fkey` "
        "(RESTRICT) barram todo cliente que ja pediu. Cliente que nunca pediu "
        "nao tem saldo, entao o conjunto que ela alcanca e vazio — por "
        "circunstancia, nao por desenho"
    ),
    # --- filhas do PEDIDO ---------------------------------------------------
    "coupon_redemptions_order_id_fkey": (
        "a redencao e do pedido. Reparar que a MESMA tabela barra pelo cupom "
        "(RESTRICT) e cascateia pelo pedido: sao duas perguntas diferentes"
    ),
    "whatsapp_messages_order_id_fkey": (
        "o aviso e do pedido. A outra FK da mesma tabela — `channel_id` — e "
        "NO ACTION de proposito: apagar o CANAL apagaria o registro de que o "
        "cliente foi avisado, e esse registro nao pende do pedido"
    ),
    # --- SET NULL: a linha fica, o vinculo some -----------------------------
    #
    # Silencioso do mesmo jeito que o CASCADE, e pior de achar: nao falta
    # linha, falta sentido numa coluna.
    "orders_customer_address_id_fkey": (
        "o pedido guarda o endereco em SNAPSHOT; a FK e so a origem. O cliente "
        "apagar um endereco salvo nao pode reescrever a entrega ja feita"
    ),
    "orders_coupon_id_fkey": (
        "o desconto ja esta congelado no pedido (`coupon_discount_amount`). "
        "A FK aponta para a campanha, que pode deixar de existir"
    ),
    "idempotency_keys_order_id_fkey": (
        "a chave sobrevive ao pedido por ate 24h por desenho; o vinculo e "
        "conveniencia de leitura"
    ),
    "admin_users_branch_id_fkey": "o gerente perde a filial e vira do restaurante",
    "admin_error_reports_admin_user_id_fkey": "quem reportou saiu da equipe; o erro fica",
    "admin_error_reports_branch_id_fkey": "idem para a filial",
    "cashback_transactions_order_id_fkey": (
        "o razao guarda `amount`; o pedido e a origem. **O prazo NAO se perde "
        "aqui**: quem responde validade e `expires_at_from_last_order`, que "
        "olha o ULTIMO PEDIDO do par (cliente, restaurante) — armadilha 26"
    ),
    "cashback_transactions_restaurant_id_fkey": (
        "**a mais consequente das oito, e a unica que muda um numero na tela.** "
        "Linha com `restaurant_id` nulo continua somando em "
        "`get_available_balance` (o ACUMULADO que o app mostra) e some de "
        "`get_available_balance_for_restaurant` (o que da para GASTAR): vira "
        "saldo que aparece e nao gasta. Nao acontece hoje — apagar o "
        "restaurante esbarra em `orders_restaurant_id_fkey` —, e e o que "
        "precisaria de decisao antes de qualquer limpeza de restaurante"
    ),
}


class Achado:
    def __init__(self, tipo: str, constraint: str, detalhe: str):
        self.tipo = tipo
        self.constraint = constraint
        self.detalhe = detalhe

    def __str__(self) -> str:
        return f"{self.constraint}\n      {self.detalhe}"


class Fk:
    def __init__(self, nome: str, filho: str, pai: str, regra: str, colunas: str):
        self.nome = nome
        self.filho = filho
        self.pai = pai
        self.regra = regra
        self.colunas = colunas

    @property
    def destrutiva(self) -> bool:
        return self.regra in DESTRUTIVAS_REGRAS

    def __str__(self) -> str:
        return f"{self.filho}.{self.colunas} -> {self.pai}"


def _fks(url: str) -> list[Fk]:
    """Toda FK do banco, com a regra de `ON DELETE` que ela declara."""
    engine = create_engine(url)
    with engine.connect() as conexao:
        linhas = conexao.execute(
            text(
                "SELECT conname, conrelid::regclass::text AS filho, "
                "confrelid::regclass::text AS pai, confdeltype, "
                "pg_get_constraintdef(oid) AS definicao "
                "FROM pg_constraint WHERE contype = 'f' ORDER BY 2, 1"
            )
        ).all()

    encontradas = []
    for nome, filho, pai, confdeltype, definicao in linhas:
        # `FOREIGN KEY (a, b) REFERENCES t(x, y) ON DELETE ...`
        colunas = definicao.split("FOREIGN KEY ")[1].split(" REFERENCES")[0].strip("()")
        encontradas.append(Fk(nome, filho, pai, REGRA[confdeltype], colunas))
    return encontradas


def auditar(url: str) -> list[Achado]:
    return auditar_fks(_fks(url))


def auditar_fks(fks: list[Fk], destrutivas: dict[str, str] | None = None) -> list[Achado]:
    """A regra, separada de quem le o banco.

    O `destrutivas` injetavel existe para o teste rapido poder plantar uma FK
    e exigir que ela seja acusada — sem Postgres e sem tocar em `DESTRUTIVAS`.
    Um varredor que so foi rodado contra o schema que ja esta certo nao tem
    como provar que enxerga o schema errado.
    """
    if destrutivas is None:
        destrutivas = DESTRUTIVAS
    por_nome = {fk.nome: fk for fk in fks}
    achados = []

    for fk in fks:
        if not fk.destrutiva or fk.nome in destrutivas:
            continue
        achados.append(
            Achado(
                "destrutiva nao declarada",
                fk.nome,
                f"{fk} e `ON DELETE {fk.regra}` e ninguem escreveu para onde vai "
                f"a linha filha.\n      Se isto apareceu ao mexer numa FK que "
                f"antes BARRAVA, leia duas vezes: a barreira caiu.",
            )
        )

    for nome in sorted(destrutivas):
        fk = por_nome.get(nome)
        if fk is None:
            achados.append(
                Achado(
                    "lista velha",
                    nome,
                    "Declarada em DESTRUTIVAS e nao existe no banco. Tire da "
                    "lista, senao ela vira cemiterio.",
                )
            )
        elif not fk.destrutiva:
            achados.append(
                Achado(
                    "lista velha",
                    nome,
                    f"Declarada como destrutiva e hoje e `ON DELETE {fk.regra}`, "
                    f"que BARRA. Se a mudanca foi de proposito, tire da lista.",
                )
            )

    return achados


def fechamento(fks: list[Fk], raiz: str) -> dict:
    """O que um `DELETE` na raiz levaria junto, anularia, e o que o barra hoje.

    Fecho TRANSITIVO pelas arestas `CASCADE`: apagar `restaurants` apaga
    `categories`, que apaga `products`, que apaga `product_option_groups`. E o
    terceiro degrau que a tabela de contagens nao mostra, e e nele que a
    surpresa mora.

    Uma barreira cujo FILHO ja esta no fecho **nao barra**: a linha que
    dispararia a recusa esta sendo apagada na mesma instrucao. Sem essa
    exclusao o relatorio acusaria bloqueio onde nao ha, e o falso positivo
    aqui e caro do mesmo jeito que o da armadilha 15 — ele pede uma mudanca de
    regra que ninguem precisava fazer.
    """
    apagadas = {raiz}
    fila = [raiz]
    while fila:
        pai = fila.pop()
        for fk in fks:
            if fk.pai == pai and fk.regra == "CASCADE" and fk.filho not in apagadas:
                apagadas.add(fk.filho)
                fila.append(fk.filho)

    anuladas = [fk for fk in fks if fk.pai in apagadas and fk.regra == "SET NULL"]
    barreiras = [
        fk
        for fk in fks
        if fk.pai in apagadas and not fk.destrutiva and fk.filho not in apagadas
    ]
    return {
        "apagadas": sorted(apagadas - {raiz}),
        "anuladas": sorted(anuladas, key=str),
        "barreiras": sorted(barreiras, key=str),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Confere que toda FK que apaga ou anula sozinha esta declarada."
    )
    parser.add_argument("--url", required=True, help="URL do Postgres (SQLAlchemy)")
    parser.add_argument(
        "--fechamento",
        action="store_true",
        help="mostra o que um DELETE em cada raiz levaria junto, e o que o barra",
    )
    args = parser.parse_args()

    fks = _fks(args.url)
    achados = auditar(args.url)
    contagem = {regra: sum(1 for fk in fks if fk.regra == regra) for regra in sorted(REGRA.values())}
    resumo = "  ".join(f"{regra}: {n}" for regra, n in contagem.items() if n)

    print("=" * 78)
    print(f"{len(achados)} achado(s)  |  {len(fks)} FKs no banco  |  {resumo}")
    print("=" * 78)
    print()
    if not achados:
        print("  Nenhum: toda FK que apaga ou anula sozinha tem dono escrito.")
    for achado in achados:
        print(f"  [{achado.tipo}] {achado}")
        print()

    if args.fechamento:
        for raiz in RAIZES:
            resultado = fechamento(fks, raiz)
            print()
            print(f"## DELETE em `{raiz}`")
            print()
            print(f"  apaga junto ({len(resultado['apagadas'])}):")
            for tabela in resultado["apagadas"]:
                print(f"      {tabela}")
            print(f"  anula coluna ({len(resultado['anuladas'])}):")
            for fk in resultado["anuladas"]:
                print(f"      {fk}")
            print(f"  BARRA hoje ({len(resultado['barreiras'])}):")
            for fk in resultado["barreiras"]:
                print(f"      {fk}")

    print()
    print("`NO ACTION` errado da erro na hora. `CASCADE` errado da sucesso, e o")
    print("dia em que doer e o dia em que ja apagou. E por isso que este portao")
    print("cobra as destrutivas, e nao as que barram.")
    return 1 if achados else 0


if __name__ == "__main__":
    raise SystemExit(main())
