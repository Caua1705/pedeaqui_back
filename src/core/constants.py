ADMIN_USER_ROLES = ("owner", "manager", "attendant")

# Minimo da senha de LOJISTA — 12, contra os 8 do cliente. A diferenca e
# proposital e vem da conta de estrago: a conta de cliente da acesso ao
# proprio historico; a de lojista da acesso a todos os pedidos, a lista de
# clientes com telefone e ao faturamento do restaurante. E ha um agravante
# que o cliente nao tem: essa senha fica em TEXTO PURO no config.ini do
# agente de impressao, na maquina do balcao.
#
# Mora aqui, e nao no schema nem no script, porque quem cria o usuario
# (scripts/create_admin_user.py) e quem o deixa trocar a senha
# (PATCH /admin/auth/password) precisam concordar — dois numeros seriam a
# porta de criar uma senha curta por um caminho que o outro recusa.
MIN_ADMIN_PASSWORD_LENGTH = 12

ORDER_TYPES = ("delivery", "pickup")

ORDER_STATUSES = (
    "pending",
    "accepted",
    "rejected",
    "preparing",
    "ready",
    "out_for_delivery",
    "completed",
    "cancelled",
)

# Formas de pagamento que a plataforma reconhece.
#
# Espelha o CHECK de branch_payment_methods.method_type
# (ck_branch_payment_methods_method_type, ver
# src/models/branch_payment_method_model.py:19-23). Os dois lados tem que
# mudar juntos: se um metodo entrar la e nao aqui, a filial consegue
# oferece-lo no cardapio e o pedido e recusado na criacao.
PAYMENT_METHODS = (
    "pix",
    "credit_card",
    "debit_card",
    "cash",
    "voucher",
    "meal_voucher",
    "other",
)

# Estados de pagamento de um pedido (orders.payment_status).
#
# on_delivery  O pedido nao passa por gateway: o cliente paga na entrega ou
#              na retirada. E um estado FINAL do ponto de vista da API — a
#              conferencia do dinheiro acontece no balcao, nao aqui.
# pending      Pagamento online criado e aguardando confirmacao do gateway.
#              O pedido NAO pode ser aceito nem chegar a cozinha nesse estado.
# paid         Gateway confirmou o recebimento. So a partir daqui um pedido
#              online pode ser aceito.
# failed       Gateway recusou (cartao negado, pix expirado). O cliente pode
#              tentar de novo no mesmo pedido, o que volta para `pending`.
# refunded     Foi pago e depois estornado. Nao conta para comissao.
PAYMENT_STATUSES = ("on_delivery", "pending", "paid", "failed", "refunded")

# Percentual de comissao usado quando o restaurante nao tem
# restaurant_settings.platform_commission_percent preenchido. Existe porque
# restaurant_settings e uma linha opcional: sem esse piso, um restaurante sem
# configuracao geraria pedido com comissao zero e silenciosa.
DEFAULT_PLATFORM_COMMISSION_PERCENT = "10.00"

# Fuso de operacao da plataforma. Horario de funcionamento e recorte de
# periodo do relatorio de comissao sao lidos em horario local, nao em UTC:
# "pedidos de ontem" para o lojista e o dia dele, nao o dia do servidor.
PLATFORM_TIMEZONE = "America/Fortaleza"
