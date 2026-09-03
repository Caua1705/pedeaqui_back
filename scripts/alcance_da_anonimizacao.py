"""Toda tabela que pende de `customers` esta no alcance da exclusao de conta.

A armadilha 38 diz o que acontece com a tabela que **nao** pende de
`customers`: ela so e apagada pela retencao, e por isso nasce com prazo. Este
varredor cobra a outra metade — a tabela que **pende** e que a exclusao de
conta precisa alcancar.

## O caso concreto que criou este arquivo

Em 04/09/2026 a frente do "entrar com Google" acrescenta
`customer_social_identities`: `customer_id` mais o `sub`, que e o
identificador estavel da pessoa dentro do Google. Sem um passo novo no
`CustomerAnonymizationService`, a conta anonimizada continuaria guardando um
**ponteiro para o cadastro dela num sistema de terceiro** — que e, palavra por
palavra, o motivo pelo qual `_delete_saved_cards` apaga o perfil de pagamento
junto do cartao.

E o segundo estrago era pior que o resto: com a identidade sobrevivendo, quem
excluisse a conta e entrasse com o Google de novo cairia no caso "sub
conhecido" e seria logado numa conta `is_active=False`. **403 para sempre, sem
como se recadastrar pelo Google.** Nenhum teste da suite falharia.

## Por que registro explicito, e nao "procure quem apaga"

Mesmo motivo do `espelhos_de_enum.py`, e ele vale ainda mais aqui: o service
nao fala com tabela nenhuma, ele chama repositorio. Casar `delete(Modelo)`
espalhado em nove repositorios com o passo do service que o invoca daria um
varredor que erra — e varredor que erra a classificacao e pior que varredor
que reporta tudo.

Quem classifica e a pessoa, uma vez, e a resposta fica escrita em `ALCANCE`.
Cada tabela diz uma de tres coisas:

    Alcancada(passo)      o metodo do service que a alcanca. Ele tem que EXISTIR
    Fora(motivo)          por que a linha sobrevive a exclusao, por decisao
    Indireta(via, passo)  nao tem `customer_id`: pende da pessoa por outra
                          tabela, e e por ela que o passo a acha

`Indireta` nao e categoria de conveniencia — ela e o que a primeira execucao
deste varredor ensinou. `order_reviews` guarda o COMENTARIO que a pessoa
escreveu e nao tem `customer_id`: o `UPDATE` chega nela por
`orders.customer_id`. `customer_saved_cards` chega por
`customer_payment_profiles`. As duas guardam dado de pessoa, as duas sao
alcancadas, e nenhuma das duas apareceria numa varredura que so olhasse a
coluna. O criterio de ENTRADA continua sendo `customer_id`, porque a
alternativa (fechar as FKs a partir de `customers`) arrasta `order_items` e
`products` e devolve o schema inteiro.

Quatro coisas viram vermelho, e as quatro sao a mesma pergunta feita de
angulos diferentes:

1. **tabela com `customer_id` fora de `ALCANCE`** — e a tabela nova que
   ninguem ligou na exclusao. O caso acima;
2. **passo declarado que nao existe mais no service** — a renomeacao que
   levou o passo embora e deixou a linha da lista dizendo que ele cuida;
3. **entrada direta cuja tabela nao tem mais `customer_id`** — para a lista
   nao virar cemiterio (a mesma regra do `ESPERADOS` do
   `estado_entre_workers.py`);
4. **`Indireta` cujo `via` deixou de pender da pessoa** — o caminho pelo qual
   o passo achava a linha sumiu, e o passo continua escrito como se nao.

## Onde ele nao chega

Ele le `Base.metadata`, entao **coluna criada a mao no Supabase nao aparece**
— e a armadilha 33, e o que a fecha e a regra de so mudar schema por revisao.
E ele nao le o `Redis`: dado pessoal em cache tem varredor proprio
(`dados_pessoais_em_chave.py`), e a armadilha 56 e sobre justamente a
exclusao de conta nao alcancar o que esta la.

    python scripts/alcance_da_anonimizacao.py --tudo

Somente leitura: le `Base.metadata` e o fonte do service. Nao escreve nada.
"""

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# `import src.models` e o que popula o `Base.metadata`: o `__init__` importa
# os models um a um, e model que nao esteja la fica fora do metadata (foi o
# que aconteceu com `delivery_estimates`, e esta anotado la).
import src.models  # noqa: E402, F401
from src.db.base import Base  # noqa: E402


SERVICE_PATH = Path("src/services/customer_anonymization_service.py")

#: A coluna que faz a linha pender da pessoa.
COLUNA_DA_PESSOA = "customer_id"


@dataclass(frozen=True)
class Alcancada:
    """A tabela e alcancada por este passo do service."""

    passo: str


@dataclass(frozen=True)
class Fora:
    """A linha sobrevive a exclusao, por decisao. O motivo fica escrito."""

    motivo: str


@dataclass(frozen=True)
class Indireta:
    """Sem `customer_id`: pende da pessoa por `via`, e o passo a acha por ela."""

    via: str
    passo: str


# tabela -> Alcancada(passo do service) | Fora(motivo)
#
# Toda tabela com `customer_id` entra aqui. Uma que apareca e nao esteja na
# lista e achado: e a tabela nova que ninguem ligou na exclusao de conta.
ALCANCE: dict[str, Alcancada | Fora | Indireta] = {
    # --- o que a exclusao alcanca -------------------------------------------
    "customers": Alcancada("_anonymize_customer"),
    "orders": Alcancada("_anonymize_orders"),
    "customer_addresses": Alcancada("_delete_addresses"),
    "customer_payment_profiles": Alcancada("_delete_saved_cards"),
    "delivery_estimates": Alcancada("_delete_delivery_estimates"),
    "email_verification_codes": Alcancada("_delete_verification_codes"),
    "password_reset_codes": Alcancada("_delete_verification_codes"),
    "account_deletion_codes": Alcancada("_delete_verification_codes"),
    "customer_social_identities": Alcancada("_delete_social_identities"),
    # --- o que a exclusao alcanca por outro caminho -------------------------
    "order_reviews": Indireta("orders", "_clear_review_comments"),
    "customer_saved_cards": Indireta("customer_payment_profiles", "_delete_saved_cards"),
    # --- o que sobrevive, e por que -----------------------------------------
    "cashback_transactions": Fora(
        "O extrato e financeiro, e nao dado de identificacao — a decisao esta "
        "em docs/lgpd-fase2-exclusao-de-conta.md. O efeito pratico e que o "
        "saldo fica inalcancavel, e e por isso que `_log_forfeited_cashback` "
        "existe: o rastro do dinheiro que a pessoa perdeu"
    ),
    "coupon_redemptions": Fora(
        "`customer_id` e NOT NULL e e o que faz 'uma vez por cliente' valer. "
        "Apagar a redencao transformaria excluir a conta numa forma de "
        "reciclar desconto de uso unico — e o service inteiro nao e um DELETE "
        "por causa desta tabela (ver o cabecalho dele)"
    ),
    "coupon_claims": Fora(
        "Mesmo motivo da redencao, um andar acima: o UNIQUE "
        "(coupon_id, customer_id) e o que faz resgatar duas vezes ser igual a "
        "resgatar uma. A linha nao guarda nada da pessoa alem do id, que "
        "depois da anonimizacao e um pseudonimo"
    ),
    "ai_voice_sessions": Fora(
        "Contagem de token e duracao de sessao emitida — medicao de custo, "
        "sem conteudo nenhum: o audio vai do navegador direto para a OpenAI e "
        "o backend nunca o ve (armadilha 43). Sobra o id, que e pseudonimo"
    ),
}


@dataclass(frozen=True)
class Achado:
    tabela: str
    problema: str


def tabelas_que_pendem_da_pessoa() -> list[str]:
    """As tabelas do ORM com uma coluna `customer_id`, mais `customers`.

    `customers` entra porque ela e o alvo principal, e sair da lista faria o
    varredor calar sobre o passo mais importante de todos.
    """
    tabelas = {
        nome
        for nome, tabela in Base.metadata.tables.items()
        if COLUNA_DA_PESSOA in tabela.columns
    }
    tabelas.add("customers")
    return sorted(tabelas)


def passos_do_service() -> set[str]:
    """Os metodos que o `CustomerAnonymizationService` tem hoje.

    Sai do AST e nao de `dir()` da classe para o varredor nao depender de
    importar o service — que arrasta o gateway de pagamento e o `settings`
    inteiro atras.
    """
    fonte = (ROOT_DIR / SERVICE_PATH).read_text(encoding="utf-8")
    arvore = ast.parse(fonte)
    for no in ast.walk(arvore):
        if isinstance(no, ast.ClassDef) and no.name == "CustomerAnonymizationService":
            return {
                filho.name
                for filho in no.body
                if isinstance(filho, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def varrer(
    diretas: set[str] | None = None,
    passos: set[str] | None = None,
    alcance: dict[str, Alcancada | Fora | Indireta] | None = None,
) -> list[Achado]:
    """Os quatro vermelhos. Lista vazia e o resultado esperado.

    Os tres argumentos existem para o teste poder plantar uma tabela e exigir
    que ela seja acusada. Sem eles, o modo de falha e o de sempre: o varredor
    para de enxergar e o portao fica verde por VACUIDADE, que e
    indistinguivel de verde por acerto.
    """
    achados: list[Achado] = []
    diretas = set(tabelas_que_pendem_da_pessoa()) if diretas is None else diretas
    passos = passos_do_service() if passos is None else passos
    alcance_declarado = ALCANCE if alcance is None else alcance

    for tabela in sorted(diretas):
        if tabela not in alcance_declarado:
            achados.append(
                Achado(
                    tabela,
                    f"tem `{COLUNA_DA_PESSOA}` e nao esta em ALCANCE. A exclusao "
                    "de conta nao a alcanca, ou alcanca e ninguem escreveu qual "
                    "passo faz isso",
                )
            )

    for tabela, entrada in alcance_declarado.items():
        achados.extend(_achados_da_entrada(tabela, entrada, diretas, passos))

    return achados


def _achados_da_entrada(
    tabela: str,
    alcance: Alcancada | Fora | Indireta,
    diretas: set[str],
    passos: set[str],
) -> list[Achado]:
    """O que ha de errado nesta entrada da lista. Lista vazia quando ela bate."""
    if isinstance(alcance, Indireta):
        return _achados_da_indireta(tabela, alcance, diretas, passos)

    if tabela not in diretas:
        return [
            Achado(
                tabela,
                f"esta em ALCANCE como direta e nao tem mais `{COLUNA_DA_PESSOA}` "
                "no ORM. Entrada de cemiterio: tire-a da lista, ou declare-a "
                "como Indireta dizendo por onde ela pende da pessoa",
            )
        ]
    if isinstance(alcance, Alcancada) and alcance.passo not in passos:
        return [_achado_de_passo_ausente(tabela, alcance.passo)]
    return []


def _achados_da_indireta(
    tabela: str,
    alcance: Indireta,
    diretas: set[str],
    passos: set[str],
) -> list[Achado]:
    achados: list[Achado] = []
    if alcance.via not in diretas:
        achados.append(
            Achado(
                tabela,
                f"pende da pessoa por `{alcance.via}`, que nao tem mais "
                f"`{COLUNA_DA_PESSOA}`. O caminho que `{alcance.passo}` usa para "
                "achar a linha deixou de existir",
            )
        )
    if alcance.passo not in passos:
        achados.append(_achado_de_passo_ausente(tabela, alcance.passo))
    return achados


def _achado_de_passo_ausente(tabela: str, passo: str) -> Achado:
    return Achado(
        tabela,
        f"declara o passo `{passo}`, que nao existe mais em "
        "CustomerAnonymizationService",
    )


def _imprimir_entrada(tabela: str, alcance: Alcancada | Fora | Indireta | None) -> None:
    if isinstance(alcance, Alcancada):
        print(f"  [alcancada] {tabela}")
        print(f"              {alcance.passo}")
        return
    if isinstance(alcance, Indireta):
        print(f"  [indireta]  {tabela}")
        print(f"              {alcance.passo}, por {alcance.via}")
        return
    if isinstance(alcance, Fora):
        print(f"  [fora]      {tabela}")
        print(f"              {alcance.motivo}")
        return
    print(f"  [?]         {tabela}   <-- NAO DECLARADA")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Cobra que toda tabela com customer_id esteja no alcance da "
            "exclusao de conta (somente leitura)."
        )
    )
    parser.add_argument(
        "--tudo",
        action="store_true",
        help="Lista tambem a classificacao de cada tabela declarada.",
    )
    args = parser.parse_args()

    diretas = tabelas_que_pendem_da_pessoa()
    achados = varrer()

    print("=" * 76)
    print(
        f"{len(diretas)} tabela(s) com {COLUNA_DA_PESSOA}  |  "
        f"{len(ALCANCE)} declarada(s)  |  {len(achados)} achado(s)"
    )
    print("=" * 76)

    if args.tudo:
        for tabela in sorted(set(diretas) | set(ALCANCE)):
            print()
            _imprimir_entrada(tabela, ALCANCE.get(tabela))

    for achado in achados:
        print()
        print(f"  !! {achado.tabela}")
        print(f"     {achado.problema}")

    if not achados:
        print()
        print("Toda tabela com customer_id esta declarada, e todo passo declarado")
        print("existe no service.")
        return 0

    print()
    print("O numero certo aqui e ZERO, e zero que cresce nao e divida herdada —")
    print("e uma tabela com dado de pessoa que a exclusao de conta nao alcanca.")
    print("Ligue o passo no CustomerAnonymizationService, ou escreva em ALCANCE")
    print("por que a linha sobrevive.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
