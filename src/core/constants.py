# Espelha o CHECK de `admin_users.role` (revisao 20260814_0020). Papel que
# entre aqui e nao no banco derruba o INSERT do script de criacao de usuario;
# no banco e nao aqui, o script nem oferece a opcao.
#
# `print_agent` e papel de MAQUINA: e o usuario do agente de impressao, cuja
# senha fica em texto puro no config.ini da maquina do balcao. Ele nao tem
# tela no painel e nao pertence a uma pessoa.
ADMIN_USER_ROLES = ("owner", "manager", "attendant", "print_agent")

# Os papeis que uma PESSOA pode ter. E `ADMIN_USER_ROLES` menos a conta de
# maquina, e existe para o cadastro de usuario do painel nao oferecer "agente
# de impressao" como se fosse cargo de gente.
#
# Constante propria e nao um `[:-1]`: a lista de cima espelha o CHECK da
# tabela, esta espelha uma decisao de produto, e um dia elas divergem por
# motivos que nada tem a ver um com o outro.
#
# **Nao confundir com `PESSOAS`, de `dependencies/admin_scope.py`**, que hoje
# tem os mesmos tres valores e responde outra pergunta: aquela e "quais papeis
# esta ROTA aceita", esta e "quais papeis podem ser ATRIBUIDOS a alguem". Uma
# rota nova aberta so a dono e gerente mexeria em `PESSOAS` sem que o cadastro
# de usuario tivesse mudado.
PAPEIS_DE_PESSOA = ("owner", "manager", "attendant")

# A conta de maquina, para quem precisa nomea-la sem repetir a string. Ela
# NASCE SO PELO SCRIPT (`scripts/create_admin_user.py`), e a rota de cadastro
# a recusa no corpo: criar um agente e parte de uma instalacao fisica — alguem
# esta na loja, com a maquina na frente, editando o config.ini. Uma tela que
# crie a conta sem ninguem la cria conta orfa, que o heartbeat nunca preenche.
PAPEL_DE_MAQUINA = "print_agent"

# Quem enxerga o restaurante inteiro. Mora aqui, e nao so em
# `dependencies/admin_scope.py`, porque o repositorio precisa dele para contar
# os donos ativos — e uma camada de repositorio nao importa dependencia de
# rota. `UNRESTRICTED_ROLE` continua existindo la, apontando para este valor:
# dois literais "owner" em arquivos diferentes seriam duas chances de divergir.
PAPEL_DE_DONO = "owner"

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
# in_review    Antifraude do gateway segurou a cobranca para analise. So
#              acontece com CARTAO — pix nao passa por analise. Como
#              `pending`, NAO libera o pedido; existe separado porque as duas
#              esperas pedem conversas opostas com o cliente: `pending` e "o
#              cliente ainda nao pagou" (e com ele), `in_review` e "o gateway
#              esta analisando" (e com ninguem, e pode levar ate 48h uteis).
#              Sem essa separacao o lojista le "aguardando pagamento" nos dois
#              casos e nao sabe qual ligacao fazer.
# paid         Gateway confirmou o recebimento. So a partir daqui um pedido
#              online pode ser aceito.
# failed       Gateway recusou (cartao negado, pix expirado). O cliente pode
#              tentar de novo no mesmo pedido, o que volta para `pending`.
# refunded     Foi pago e depois estornado POR INTEIRO. Nao conta para
#              comissao. Estorno PARCIAL nao chega aqui: ele mantem o
#              pagamento em `paid` e aparece so em `orders.refunded_amount`
#              (ver PaymentService._apply_refunded_amount).
PAYMENT_STATUSES = ("on_delivery", "pending", "in_review", "paid", "failed", "refunded")

# Estados em que ainda existe cobranca VIVA do lado do gateway — dinheiro
# retido, ou uma cobranca que o cliente ainda consegue pagar.
#
# Sao exatamente os estados em que um pedido cancelado tem o que ser feito
# no Mercado Pago, e por isso a lista serve a dois lugares que precisam
# concordar: `PaymentRefundService`, que decide, e
# `OrderRepository.list_orders_awaiting_refund`, que acha o que ficou para
# tras. Duas copias virariam uma varredura que procura um conjunto e um
# service que trata outro.
#
# `on_delivery` fica de fora porque nunca houve cobranca; `failed` porque a
# cobranca ja morreu; `refunded` porque o dinheiro ja voltou.
PAYMENT_STATUSES_WITH_LIVE_CHARGE = ("pending", "in_review", "paid")

# O que o cliente aponta quando a nota e baixa. ESPELHA o CHECK
# `ck_order_reviews_problem_tag` e muda JUNTO com ele — e o mesmo par de
# listas da armadilha 15 (PAYMENT_METHODS x o CHECK de
# branch_payment_methods), e erra do mesmo jeito: etiqueta que exista so aqui
# passa pela validacao do schema e morre no INSERT; etiqueta que exista so no
# banco nunca chega a ser oferecida.
#
# Lista FECHADA e nao texto livre porque o painel a AGREGA: "7 de 12 notas
# baixas desta semana foram atraso" e a frase que faz o lojista consertar
# alguma coisa. Texto livre nao soma.
#
# `outro` existe para a etiqueta que falta nao virar nota baixa sem motivo —
# e a frequencia dele e o sinal de que a lista precisa crescer.
REVIEW_PROBLEM_TAGS = (
    "atrasou",
    "veio_errado",
    "veio_frio",
    "faltou_item",
    "qualidade",
    "outro",
)

# Percentual de comissao usado quando o restaurante nao tem
# restaurant_settings.platform_commission_percent preenchido. Existe porque
# restaurant_settings e uma linha opcional: sem esse piso, um restaurante sem
# configuracao geraria pedido com comissao zero e silenciosa.
DEFAULT_PLATFORM_COMMISSION_PERCENT = "10.00"

# Fuso de operacao da plataforma. Horario de funcionamento e recorte de
# periodo do relatorio de comissao sao lidos em horario local, nao em UTC:
# "pedidos de ontem" para o lojista e o dia dele, nao o dia do servidor.
PLATFORM_TIMEZONE = "America/Fortaleza"


# Os provedores de identidade que o app do cliente aceita. ESPELHA o CHECK
# `ck_customer_social_identities_provider` (revisao 20260904_0049) e muda
# JUNTO com ele — armadilha 15: provedor que exista so aqui e recusado no
# INSERT; so no banco, nunca chega a ser oferecido.
#
# Um valor so, e o CHECK existe justamente por isso: a segunda linha (Apple)
# e que vai precisar das duas listas concordando, e nesse dia ninguem vai
# lembrar que ha duas.
SOCIAL_AUTH_PROVIDERS = ("google",)

# O provedor, para quem precisa nomea-lo sem repetir a string.
SOCIAL_PROVIDER_GOOGLE = "google"


# Os avisos de pedido que saem pelo WhatsApp. ESPELHA o CHECK
# `ck_whatsapp_messages_kind` (revisao 20260904_0051) e muda JUNTO com ele —
# armadilha 15.
#
# O valor NAO e o nome do template aprovado na Meta, e a diferenca importa: o
# template pode ser renomeado, reaprovado ou trocado por outro idioma sem que
# o aviso deixe de ser "o pedido foi aceito". Quem liga um no outro e
# `WHATSAPP_TEMPLATE_BY_KIND`, no service.
WHATSAPP_MESSAGE_KINDS = (
    "order_accepted",
    "order_out_for_delivery",
    "order_delivered",
)

# O ciclo de vida de uma mensagem enviada. ESPELHA o CHECK
# `ck_whatsapp_messages_status`.
#
# As quatro palavras sao as DA META, e nao nossas: `sent`, `delivered` e
# `read` chegam prontos no `statuses[]` do webhook, e traduzi-los criaria uma
# tabela de-para para manter em duas pontas. `failed` e o unico que tambem
# nascemos escrevendo — quando a chamada nem chegou a ser aceita.
#
# Nao ha estado "enfileirado": a linha nasce depois da resposta da Meta, e uma
# linha antes dela seria um estado que nenhum webhook resolve.
WHATSAPP_MESSAGE_STATUSES = ("sent", "delivered", "read", "failed")

# Quanto dura a janela de atendimento depois de uma mensagem do cliente. E um
# numero DELES, nao nosso: dentro dela cabe texto livre, fora dela so template
# aprovado. Constante e nao variavel de ambiente justamente por isso —
# configuravel, ela vira um numero nosso que diverge do deles em silencio.
WHATSAPP_CUSTOMER_WINDOW_HOURS = 24
