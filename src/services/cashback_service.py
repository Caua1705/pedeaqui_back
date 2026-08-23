"""Saldo de cashback: o que se le, o que se credita e o que se resgata.

## O razao e assinado, e o saldo e a SOMA

Nao ha lote, nao ha FIFO e nao ha linha partida ao meio. Credito entra
positivo, resgate entra negativo, e o saldo e `SUM(amount)` das linhas
`available` daquele restaurante. E o que faz o resgate parcial custar uma
linha nova em vez de bookkeeping de lote.

O `status` serve para TIRAR linha da soma de uma vez: e o que a expiracao vai
usar, virando `available` em `expired` no par (cliente, restaurante) inteiro.

## Toda escrita tem chave de idempotencia, e a chave e o pedido

`cashback:earn:{order_id}`, `cashback:redeem:{order_id}`,
`cashback:refund:{order_id}`. O indice unico parcial
(`ux_cashback_transactions_idempotency_key`) e a garantia final; a consulta
antes do INSERT e o que transforma "ja foi feito" em resultado normal, e nao
em erro de integridade — mesmo desenho de `CouponService.create_redemption`.

Isso importa porque o painel reenvia: clique duplo, retry de rede, replay de
`Idempotency-Key`. Creditar duas vezes o mesmo pedido e dinheiro criado do
nada.

## Nada aqui commita

Quem commita e o service que chama (`OrderService` no resgate,
`AdminOrderService` no credito e no estorno), e de proposito: a linha do
razao tem que entrar na MESMA transacao do pedido que a originou. Ou as duas
coisas acontecem, ou nenhuma.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.cashback_transaction_model import CashbackTransaction
from src.models.customer_model import Customer
from src.repositories.branch_repository import BranchRepository
from src.repositories.cashback_repository import CashbackRepository
from src.repositories.cashback_rule_repository import CashbackRuleRepository
from src.repositories.customer_repository import CustomerRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.cashback_schema import (
    CashbackBalanceResponse,
    CashbackTransactionResponse,
    CashbackTransactionsResponse,
    RestaurantCashbackBalance,
)
from src.services.cashback_rule import (
    CashbackTerms,
    expires_at_from_last_order,
    resolve_cashback_terms,
)
from src.services.order_state_machine import KITCHEN_ORDER_STATUSES
from src.utils.money import ZERO, money_to_float, quantize_money, to_decimal


logger = logging.getLogger("uvicorn.error")


class CashbackService:
    DESCRIPTIONS = {
        "earned": "Cashback recebido",
        "redeemed": "Cashback utilizado",
        "expired": "Cashback expirado",
        "cancelled": "Cashback cancelado",
        "adjustment": "Ajuste de cashback",
    }

    def __init__(self, db: Session):
        self.db = db
        self.cashback_repository = CashbackRepository(db)
        self.cashback_rule_repository = CashbackRuleRepository(db)
        self.branch_repository = BranchRepository(db)
        self.order_repository = OrderRepository(db)
        self.customer_repository = CustomerRepository(db)

    def get_balance(self, customer: Customer) -> CashbackBalanceResponse:
        """O acumulado e a quebra por restaurante.

        **`balance` continua sendo a SOMA de todos os restaurantes**, e ele
        nao e gastavel em lugar nenhum: o saldo pertence a quem o concedeu.
        O que da para usar numa loja esta em `by_restaurant[]`, e a tela
        mostra essa lista — publicar "R$ 40" com R$ 5 gastaveis naquela loja
        e criar a reclamacao com as proprias maos.

        O total fica porque tira-lo quebraria o app que ja consome esta rota,
        e porque ele continua respondendo uma pergunta legitima: quanto a
        pessoa acumulou — que e exatamente o que ela PERDE ao excluir a
        conta.
        """
        balance = self.cashback_repository.get_available_balance(customer.id)
        return CashbackBalanceResponse(
            balance=money_to_float(balance),
            by_restaurant=self._balances_by_restaurant(customer),
        )

    def _balances_by_restaurant(
        self,
        customer: Customer,
    ) -> list[RestaurantCashbackBalance]:
        """Uma linha por restaurante em que ainda ha saldo, com a validade.

        Tres consultas de tamanho fixo, nunca uma por restaurante: os saldos,
        o ultimo pedido de cada loja e as regras. O N+1 aqui apareceria
        justamente na tela que o cliente abre para olhar o dinheiro dele.

        **`min_redeem_balance` NAO entra nesta lista, e a ausencia e
        deliberada.** Ele e termo do resgate, e o resgate acontece numa
        FILIAL, que pode ter regra propria — publica-lo sob uma chave por
        restaurante seria mostrar um piso que nao vale na loja em que a
        pessoa esta pedindo. A validade nao tem esse problema: ela e
        propriedade do saldo, e o saldo e do restaurante.
        """
        saldos = self.cashback_repository.list_available_balances_by_restaurant(
            customer.id
        )
        if not saldos:
            return []

        ultimo_pedido = self.order_repository.last_order_at_by_restaurant(
            customer.id, KITCHEN_ORDER_STATUSES
        )
        regras = self.cashback_rule_repository.list_restaurant_rules(
            [restaurante.id for restaurante, _ in saldos]
        )

        linhas = []
        for restaurante, saldo in saldos:
            terms = resolve_cashback_terms(None, regras.get(restaurante.id))
            linhas.append(
                RestaurantCashbackBalance(
                    restaurant_id=restaurante.id,
                    restaurant_name=restaurante.name,
                    restaurant_slug=restaurante.slug,
                    balance=money_to_float(saldo),
                    expires_at=expires_at_from_last_order(
                        ultimo_pedido.get(restaurante.id), terms
                    ),
                )
            )
        return linhas

    def list_transactions(
        self,
        customer: Customer,
        limit: int,
        offset: int,
    ) -> CashbackTransactionsResponse:
        balance = self.cashback_repository.get_available_balance(customer.id)
        rows = self.cashback_repository.list_transactions(customer.id, limit, offset)
        transactions = [
            CashbackTransactionResponse(
                id=transaction.id,
                type=transaction.type,
                amount=money_to_float(transaction.amount),
                status=transaction.status,
                description=self.DESCRIPTIONS[transaction.type],
                restaurant_name=restaurant_name,
                order_id=transaction.order_id,
                expires_at=transaction.expires_at,
                created_at=transaction.created_at,
            )
            for transaction, restaurant_name in rows
        ]
        return CashbackTransactionsResponse(
            balance=money_to_float(balance),
            transactions=transactions,
        )

    # -----------------------------------------------------------------
    # Resgate — acontece na CRIACAO do pedido
    # -----------------------------------------------------------------

    def amount_to_redeem(
        self,
        *,
        customer: Customer,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
        momento: datetime,
        teto: Decimal,
    ) -> Decimal:
        """Quanto do saldo entra neste pedido.

        `teto` e o subtotal ja descontado o cupom: o cashback nunca desconta
        taxa de entrega nem taxa de servico, e nunca leva o total abaixo de
        zero.

        O valor sai daqui e nao do corpo do pedido. Nenhum preco vem do
        cliente — e um `Decimal` enviado pelo app teria que ser conferido
        refazendo esta conta inteira, o que e a propria conta.

        **Quem chama ja tem que ter travado a linha do cliente.** O saldo
        lido sem lock e o saldo de um instante que pode nao existir mais no
        commit, e dois pedidos simultaneos gastariam o mesmo dinheiro.
        """
        terms = self._terms(restaurant_id, branch_id, momento)
        if not terms.enabled:
            return ZERO
        if teto <= ZERO:
            return ZERO

        balance = self.cashback_repository.get_available_balance_for_restaurant(
            customer.id, restaurant_id
        )
        # O minimo e do SALDO, e nao do resgate: com R$ 3 acumulados e minimo
        # de R$ 5 nao ha resgate nenhum, nem parcial.
        if balance < terms.min_redeem_balance:
            return ZERO
        return quantize_money(min(balance, teto))

    def register_redemption(self, order, amount: Decimal) -> CashbackTransaction | None:
        """A linha NEGATIVA do resgate, na transacao do pedido."""
        if amount <= ZERO:
            return None
        return self._register(
            customer_id=order.customer_id,
            restaurant_id=order.restaurant_id,
            order_id=order.id,
            tipo="redeemed",
            amount=-quantize_money(amount),
            idempotency_key=f"cashback:redeem:{order.id}",
            metadata={"order_number": str(order.order_number)},
        )

    # -----------------------------------------------------------------
    # Credito — acontece na CONCLUSAO do pedido
    # -----------------------------------------------------------------

    def credit_for_order(self, order) -> Decimal:
        """Credita o cashback de um pedido concluido.

        **Na conclusao, e nao no pago nem no aceite.** `paid` acontece na
        criacao do pedido online e a loja ainda pode recusar; de `accepted`
        ainda se cancela. `completed` e terminal — depois dele o pedido nao
        volta, e o estorno deixa de ser o caminho comum.

        **A base e `commission_base_amount`, ja congelada no pedido.** Uma
        base, dois usos, impossivel divergirem: taxa de entrega e taxa de
        servico ficam de fora sem precisar dizer de novo, e pedido pago com
        cashback gera sobre o liquido.

        **O percentual e o do dia do PEDIDO**, nao o de hoje: o pedido de
        terca (10%) concluido na quarta (3%) tem que pagar os 10% que a tela
        do checkout prometeu.

        Devolve o valor creditado, ou zero — e zero e resultado normal aqui:
        pedido de convidado, loja sem campanha e forma de pagamento que nao
        gera passam por este caminho todo dia.
        """
        if order.customer_id is None:
            return ZERO
        chave = f"cashback:earn:{order.id}"
        if self.cashback_repository.get_by_idempotency_key(chave) is not None:
            return ZERO

        terms = self._terms(order.restaurant_id, order.branch_id, order.created_at)
        if not terms.enabled:
            return ZERO
        if not self._payment_method_earns(order):
            return ZERO

        base = to_decimal(order.commission_base_amount)
        amount = quantize_money(base * terms.percent / Decimal("100"))
        if amount <= ZERO:
            return ZERO

        self._register(
            customer_id=order.customer_id,
            restaurant_id=order.restaurant_id,
            order_id=order.id,
            tipo="earned",
            amount=amount,
            idempotency_key=chave,
            # O percentual e a base ficam gravados na linha pelo mesmo motivo
            # de `commission_percent` estar no pedido: sem eles o valor nao e
            # conferivel depois, e a regra pode ter mudado no meio.
            metadata={"percent": str(terms.percent), "base": str(base)},
        )
        return amount

    def refund_redemption(self, order) -> Decimal:
        """Devolve o cashback resgatado num pedido cancelado ou recusado.

        Espelha `CouponService.reverse_for_order`, e existe pelo mesmo
        motivo: sem ela o cliente cancela e PERDE o saldo, que e o pior
        chamado possivel.

        Linha nova, nunca UPDATE do resgate: o razao e historico, e o extrato
        do cliente precisa mostrar a devolucao.
        """
        # O pedido ja diz se resgatou alguma coisa, e a quase totalidade nao
        # resgatou: sem esta saida, todo cancelamento pagaria uma consulta ao
        # razao para descobrir que nao havia nada a devolver.
        if to_decimal(order.cashback_redeemed_amount) <= ZERO:
            return ZERO

        redemption = self.cashback_repository.get_by_idempotency_key(
            f"cashback:redeem:{order.id}"
        )
        if redemption is None:
            return ZERO
        chave = f"cashback:refund:{order.id}"
        if self.cashback_repository.get_by_idempotency_key(chave) is not None:
            return ZERO

        # A linha do resgate e negativa; a devolucao e o mesmo valor com o
        # sinal trocado.
        amount = -to_decimal(redemption.amount)
        self._register(
            customer_id=redemption.customer_id,
            restaurant_id=redemption.restaurant_id,
            order_id=order.id,
            tipo="cancelled",
            amount=amount,
            idempotency_key=chave,
            metadata={"motivo": "pedido cancelado"},
        )
        return amount

    # -----------------------------------------------------------------
    # Expiracao — o relogio e o ULTIMO PEDIDO
    # -----------------------------------------------------------------

    def expire_balance(
        self,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        momento: datetime,
    ) -> Decimal:
        """Vence o saldo daquele par, se ele estiver vencido. Nao commita.

        **Confere o vencimento DE NOVO, ja com o cliente travado**, e nao
        confia na lista que a varredura montou. Entre ler a lista e chegar
        aqui a pessoa pode ter feito um pedido — e o pedido empurra a
        validade para a frente. Sem esta segunda leitura, quem pediu no
        segundo errado perde o saldo inteiro, e nao ha desfazer.

        **O lock e o mesmo do resgate, e tem que ser.** Sem ele a varredura
        marca as linhas como `expired` no instante em que o checkout ja leu
        o saldo e vai gravar o resgate: o pedido sairia descontando um
        dinheiro que deixou de existir. Cliente primeiro, como em todo
        caminho — aqui nao ha segundo lock, entao nao ha ciclo possivel.

        Devolve o valor vencido, ou zero. Zero e resultado normal: a maior
        parte das linhas da lista some aqui quando a varredura demora.
        """
        self.customer_repository.lock_customer(customer_id)

        vencimento = self.expires_at_for(customer_id, restaurant_id)
        if vencimento is None:
            return ZERO
        if vencimento > momento:
            return ZERO

        saldo = self.cashback_repository.get_available_balance_for_restaurant(
            customer_id, restaurant_id
        )
        if saldo <= ZERO:
            return ZERO

        linhas = self.cashback_repository.mark_available_as_expired(
            customer_id, restaurant_id
        )
        self._register_expiry(customer_id, restaurant_id, saldo, vencimento, linhas)
        return saldo

    def expires_at_for(
        self,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> datetime | None:
        """Quando o saldo daquele par vence — a MESMA conta que a tela faz.

        A tela e a varredura chamam esta funcao, e nao duas contas parecidas.
        Duas discordariam no dia em que uma delas fosse ajustada, e a
        divergencia seria saldo apagado antes da data que o app mostrou.
        """
        ultimo_pedido = self.order_repository.last_order_at_by_restaurant(
            customer_id, KITCHEN_ORDER_STATUSES
        )
        regra = self.cashback_rule_repository.list_restaurant_rules([restaurant_id])
        terms = resolve_cashback_terms(None, regra.get(restaurant_id))
        return expires_at_from_last_order(ultimo_pedido.get(restaurant_id), terms)

    def _register_expiry(
        self,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        saldo: Decimal,
        vencimento: datetime,
        linhas: int,
    ) -> CashbackTransaction:
        """UMA linha negativa, para o extrato ficar legivel.

        Sem ela o extrato mostraria creditos que somam R$ 40 e um saldo de
        zero, sem nada dizendo para onde o dinheiro foi.

        **`status="expired"`, e nao `available`.** Esta linha e historico, e
        nao parcela do saldo: com `available` ela seria a UNICA linha na soma
        e o saldo do cliente ficaria NEGATIVO — uma divida que ele
        descobriria no proximo pedido.

        **Sem `idempotency_key`, ao contrario das outras tres escritas.** Ali
        a chave e o pedido, e o mesmo pedido pode voltar. Aqui o evento nao
        tem chave natural que se repita: o que impede a segunda execucao de
        gravar de novo e nao haver mais saldo `available` para vencer, e a
        varredura seguinte simplesmente nao encontra o par. Uma chave
        derivada da data do vencimento faria pior — saldo lancado a mao
        depois (o `adjustment`, que so entra por SQL) ficaria preso em
        `available` para sempre, sem erro em lugar nenhum.
        """
        return self.cashback_repository.create(
            CashbackTransaction(
                customer_id=customer_id,
                restaurant_id=restaurant_id,
                order_id=None,
                type="expired",
                amount=-quantize_money(saldo),
                status="expired",
                idempotency_key=None,
                metadata_={"vencimento": vencimento.isoformat(), "linhas": linhas},
            )
        )

    # -----------------------------------------------------------------

    def _terms(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
        momento: datetime,
    ) -> CashbackTerms:
        da_filial, do_restaurante = self.cashback_rule_repository.get_rules_for_branch(
            restaurant_id, branch_id
        )
        return resolve_cashback_terms(da_filial, do_restaurante, momento)

    def _payment_method_earns(self, order) -> bool:
        """A forma de pagamento daquele pedido gera cashback?

        A resposta mora em `branch_payment_methods.earns_cashback`, que ja e
        a tabela que manda em forma de pagamento por filial — uma lista
        propria aqui seria a terceira do projeto (armadilha 15).

        **Sem linha correspondente, NAO gera, e a falta e logada.** A
        configuracao e quem diz o que gera; ausencia de configuracao nao e
        permissao para gastar o dinheiro do lojista. Acontece quando a filial
        remove a forma de pagamento depois do pedido — raro, e o log e o que
        torna diagnosticavel um cashback que parou de sair.
        """
        methods = self.branch_repository.list_enabled_payment_methods(order.branch_id)
        correspondentes = [
            method
            for method in methods
            if method.method_type == order.payment_method
            and method.payment_flow == order.payment_flow
        ]
        if not correspondentes:
            logger.info(
                "[Cashback] forma de pagamento sem linha na filial, nao gera "
                "order_id=%s payment_method=%s payment_flow=%s",
                order.id,
                order.payment_method,
                order.payment_flow,
            )
            return False
        # Varias linhas para o mesmo metodo existem por causa da bandeira
        # (credito Visa e credito Master sao duas): basta uma que gere.
        return any(method.earns_cashback for method in correspondentes)

    def _register(
        self,
        *,
        customer_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        order_id: uuid.UUID,
        tipo: str,
        amount: Decimal,
        idempotency_key: str,
        metadata: dict,
    ) -> CashbackTransaction:
        """Grava a linha. Nao commita — ver o docstring do modulo.

        `expires_at` fica NULO de proposito: a validade e do SALDO e conta a
        partir do ultimo pedido, nao da linha. Preenche-la aqui criaria uma
        segunda resposta para "quando isto vence", e as duas discordariam no
        primeiro pedido novo do cliente.
        """
        return self.cashback_repository.create(
            CashbackTransaction(
                customer_id=customer_id,
                restaurant_id=restaurant_id,
                order_id=order_id,
                type=tipo,
                amount=amount,
                status="available",
                idempotency_key=idempotency_key,
                metadata_=metadata,
            )
        )
