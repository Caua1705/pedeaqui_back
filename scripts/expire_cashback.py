"""Vence o saldo de cashback parado — o relogio e o ULTIMO PEDIDO.

A validade conta a partir do ultimo pedido daquele cliente naquele
restaurante, e nao da data do credito. E o que dispensa expiracao por lote:
nao ha FIFO, nao ha credito partido ao meio, ha **um relogio por (cliente,
restaurante)** que todo pedido reinicia — e quando ele vence, o saldo inteiro
daquele par vira zero de uma vez.

Desenho completo em `docs/cashback.md`, secao 3.

## POR QUE UM ARQUIVO PROPRIO, e nao mais uma tabela no cleanup

`cleanup_idempotency_keys.py` tem um contrato explicito no docstring:
**apagar linha vencida nao afeta correcao.** Passado o TTL a chave ja nao
protege nada, e o pior caso de um bug la e espaco em disco.

**Aqui um bug apaga DINHEIRO DE CLIENTE**, e nao ha desfazer. Arquivo
proprio, `--dry-run` proprio, log proprio — e nenhuma chance de uma mudanca
no expurgo de linha vencida arrastar isto junto.

## O que a execucao faz, por par vencido

1. trava a linha do cliente (`SELECT ... FOR UPDATE`), como o resgate faz;
2. **confere o vencimento de novo, ja travado** — a pessoa pode ter pedido
   entre a leitura da lista e agora, e pedido empurra a validade;
3. marca as linhas `available` daquele par como `expired`, tirando-as da
   soma sem reescrever valor nenhum;
4. grava UMA linha `type="expired"` com o total negativo, para o extrato nao
   mostrar creditos que somam R$ 40 ao lado de um saldo zero.

**Commit por cliente**, e nao um no fim: interrompido no meio (deploy,
container reiniciado), o que ja venceu esta gravado e a proxima execucao
continua de onde parou — e o lock de ninguem fica de pe pela duracao inteira
da varredura, o que travaria o checkout dessas pessoas.

## O que NAO vence

- **Quem nunca pediu naquele restaurante.** Sem pedido nao ha de quando
  contar. So chega nesse estado o ajuste manual por SQL.
- **Restaurante sem regra propria, ou com a regra desligada.** Loja que sai
  da campanha CONGELA o saldo em vez de vence-lo, e a tela concorda: ela
  passa a responder `expires_at: null`. Vencer o que a loja ja prometeu
  porque ela desligou a campanha seria a plataforma apagando dinheiro que
  nao e dela.
- **O prazo da FILIAL**, que nao existe para este fim: o saldo e do
  restaurante inteiro, entao o prazo sai da regra de `branch_id IS NULL`.

Uso:

    python scripts/expire_cashback.py
    python scripts/expire_cashback.py --dry-run

No container:

    docker exec pedeaqui-api python scripts/expire_cashback.py
"""

import argparse
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy.orm import Session

from src.db.session import SessionLocal
from src.models.cashback_rule_model import CashbackRule
from src.repositories.cashback_repository import CashbackRepository
from src.repositories.cashback_rule_repository import CashbackRuleRepository
from src.repositories.order_repository import OrderRepository
from src.services.cashback_rule import expires_at_from_last_order, resolve_cashback_terms
from src.services.cashback_service import CashbackService
from src.services.order_state_machine import KITCHEN_ORDER_STATUSES
from src.utils.money import ZERO
from src.utils.security import utcnow


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vence o saldo de cashback parado desde o ultimo pedido."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Apenas lista o que venceria, sem escrever nada.",
    )
    args = parser.parse_args()

    momento = utcnow()
    with SessionLocal() as db:
        regras = CashbackRuleRepository(db).list_enabled_restaurant_rules()
        if not regras:
            print("[cashback] nenhum restaurante com campanha ligada; nada a fazer.")
            return 0

        if args.dry_run:
            return _simular(db, regras, momento)
        return _expirar(db, regras, momento)


def _expirar(db: Session, regras: list[CashbackRule], momento: datetime) -> int:
    total = ZERO
    pessoas = 0
    for regra in regras:
        for customer_id, _, _ in _vencidos(db, regra, momento):
            valor = CashbackService(db).expire_balance(
                customer_id, regra.restaurant_id, momento
            )
            # Commit inclusive quando o valor volta zero: o `FOR UPDATE` da
            # conferencia so e liberado no fim da transacao, e segurar o
            # lock de quem NAO venceu travaria o checkout dessa pessoa.
            db.commit()
            if valor <= ZERO:
                continue
            total += valor
            pessoas += 1
            # Uma linha por saldo apagado, e e de proposito: e o rastro de
            # dinheiro de cliente que deixou de existir. Sem ela, "o saldo
            # sumiu" nao tem como ser respondido.
            print(
                f"[cashback] saldo expirado customer_id={customer_id} "
                f"restaurant_id={regra.restaurant_id} valor={valor}"
            )
    print(f"[cashback] {pessoas} saldo(s) expirado(s), somando {total}.")
    return 0


def _simular(db: Session, regras: list[CashbackRule], momento: datetime) -> int:
    total = ZERO
    pessoas = 0
    for regra in regras:
        for customer_id, saldo, vencimento in _vencidos(db, regra, momento):
            total += saldo
            pessoas += 1
            print(
                f"[dry-run] customer_id={customer_id} "
                f"restaurant_id={regra.restaurant_id} saldo={saldo} "
                f"venceu_em={vencimento.isoformat()}"
            )
    print(f"[dry-run] {pessoas} saldo(s) venceriam, somando {total}.")
    return 0


def _vencidos(
    db: Session,
    regra: CashbackRule,
    momento: datetime,
) -> list[tuple[uuid.UUID, Decimal, datetime]]:
    """Quem tem saldo vencido naquele restaurante, com quanto e desde quando.

    Duas consultas por restaurante — os saldos e o ultimo pedido de cada um —
    e o cruzamento em Python. Uma consulta por cliente seria um N+1 sobre
    toda a base de quem tem saldo.

    **A lista e um palpite, e quem decide e `expire_balance`.** Ela e lida
    sem lock, entao um pedido feito no meio da varredura ja a desatualiza; a
    conferencia definitiva acontece la, com o cliente travado. Aqui o que
    importa e nao carregar quem obviamente nao venceu.
    """
    terms = resolve_cashback_terms(None, regra)
    saldos = CashbackRepository(db).list_available_balances_by_customer(regra.restaurant_id)
    ultimo_pedido = OrderRepository(db).last_order_at_by_customer(
        regra.restaurant_id, KITCHEN_ORDER_STATUSES
    )

    vencidos = []
    for customer_id, saldo in saldos:
        vencimento = expires_at_from_last_order(ultimo_pedido.get(customer_id), terms)
        if vencimento is None:
            continue
        if vencimento > momento:
            continue
        vencidos.append((customer_id, saldo, vencimento))
    return vencidos


if __name__ == "__main__":
    raise SystemExit(main())
