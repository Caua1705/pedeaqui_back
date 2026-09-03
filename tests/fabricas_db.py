"""Linhas mínimas para a suíte `db`.

Cada função grava **uma** linha com o mínimo que o NOT NULL exige e devolve o
objeto. Sem builder, sem herança, sem `**kwargs` genérico: quem lê um teste
daqui precisa saber o que existe no banco naquele ponto sem abrir outro
arquivo, e a repetição paga isso (regra 7 do "Como escrever código aqui").

Os valores default são propositalmente sem graça. Quando um teste depende de um
valor, ele passa esse valor — o que aparece na chamada é o que importa para
aquele teste.

`flush` e não `commit`: a fixture `db` reverte tudo no fim, e o commit não
acrescenta nada aqui.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.category_model import Category
from src.models.customer_model import Customer, CustomerAddress
from src.models.order_model import Order
from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.models.restaurant_model import Restaurant
from src.models.restaurant_setting_model import RestaurantSetting
from src.utils.security import hash_tracking_token


# Largura da janela de relatório usada pelos testes. Trinta dias porque o teto
# do relatório é 92 (`MAX_REPORT_DAYS`) e porque não há teste que crie pedido
# com mais de um dia de idade sem passar `created_at` explícito.
DIAS_DA_JANELA_DE_RELATORIO = 30


def _sufixo() -> str:
    """Sufixo único para os campos com UNIQUE (slug, e-mail, telefone, CPF).

    Os testes rodam dentro de uma transação revertida, mas a mesma transação
    pode criar dois restaurantes — e aí o UNIQUE de `slug` bate.
    """
    return uuid.uuid4().hex[:12]


def periodo_de_relatorio() -> str:
    """Querystring de período que contém o que as fábricas acabaram de criar.

    **Derivada da data de execução, nunca literal.** `criar_pedido` deixa o
    `created_at` no `server_default` (`now()`), então um período escrito à mão
    envelhece: no dia em que a data de hoje passa do `end_date`, o pedido cai
    fora da janela e o relatório volta vazio.

    Isso já aconteceu, e o modo de falhar foi diferente em cada arquivo:

    - `test_auditoria_papeis.py` quebrou alto — ele afirma que o faturamento
      soma as duas filiais, e a soma virou zero;
    - `test_auditoria_isolamento_e2e.py` teria passado **em silêncio**, e é o
      pior dos dois. Ele afirma que a listagem NÃO contém dado do outro
      restaurante; com o relatório vazio a afirmação continua verdadeira e
      deixa de provar qualquer coisa.

    O fim é `hoje + 1` de propósito. O relatório recorta o período no fuso da
    operação (America/Fortaleza) e o `date.today()` sai do fuso da máquina que
    roda o teste: às 21h de Fortaleza o UTC já é o dia seguinte, e um `end_date`
    igual a hoje deixaria de fora um pedido criado nessa janela.
    """
    hoje = date.today()
    inicio = hoje - timedelta(days=DIAS_DA_JANELA_DE_RELATORIO)
    fim = hoje + timedelta(days=1)
    return f"start_date={inicio.isoformat()}&end_date={fim.isoformat()}"


def criar_restaurante(db: Session, nome: str = "Restaurante de Teste") -> Restaurant:
    restaurante = Restaurant(name=nome, slug=f"restaurante-{_sufixo()}", is_active=True)
    db.add(restaurante)
    db.flush()
    return restaurante


def criar_filial(db: Session, restaurante: Restaurant, nome: str = "Matriz") -> Branch:
    filial = Branch(
        restaurant_id=restaurante.id,
        name=nome,
        slug=f"filial-{_sufixo()}",
        address="Rua de Teste, 100",
        neighborhood="Centro",
        city="Fortaleza",
        state="CE",
        is_active=True,
    )
    db.add(filial)
    db.flush()
    return filial


def criar_configuracoes(
    db: Session,
    restaurante: Restaurant,
    voice_enabled: bool = False,
) -> RestaurantSetting:
    """A linha de `restaurant_settings` do restaurante.

    `voice_enabled` tem default False igual ao da coluna: quem quiser voz num
    teste pede voz, e o teste que não pedir descreve o estado em que todo
    restaurante nasce.
    """
    configuracoes = RestaurantSetting(
        restaurant_id=restaurante.id,
        voice_enabled=voice_enabled,
    )
    db.add(configuracoes)
    db.flush()
    return configuracoes


def filial_padrao(db: Session, restaurante: Restaurant) -> Branch:
    """A filial do restaurante, criando "Matriz" se ele ainda nao tiver uma.

    Existe porque cardapio pende de filial desde a revisao 20260820_0026 e a
    esmagadora maioria dos testes nao fala de filial nenhuma — eles falam de
    restaurante, categoria e produto. Sem isto, os 105 usos de
    `criar_categoria`/`criar_produto` teriam que ganhar um parametro para
    provar o que ja provavam.

    Nao e magica de conveniencia: e o estado que a migracao GARANTE. Ela
    recusa aplicar se existir cardapio em restaurante sem filial, entao um
    restaurante com categoria e sem loja nao e um caso que o banco de
    producao possa ter.

    Quem testa DUAS lojas passa a filial explicitamente.
    """
    existente = (
        db.query(Branch)
        .filter(Branch.restaurant_id == restaurante.id)
        .order_by(Branch.is_main.desc().nulls_last(), Branch.name.asc())
        .first()
    )
    if existente is not None:
        return existente
    return criar_filial(db, restaurante)


def criar_categoria(
    db: Session,
    restaurante: Restaurant,
    nome: str = "Categoria",
    filial: Branch | None = None,
) -> Category:
    categoria = Category(
        restaurant_id=restaurante.id,
        branch_id=(filial or filial_padrao(db, restaurante)).id,
        name=nome,
        slug=f"categoria-{_sufixo()}",
        is_active=True,
    )
    db.add(categoria)
    db.flush()
    return categoria


def criar_produto(
    db: Session,
    restaurante: Restaurant,
    categoria: Category,
    nome: str = "Produto",
    preco: Decimal = Decimal("10.00"),
    is_active: bool = True,
    is_available: bool = True,
    sort_order: int = 0,
    code: str | None = None,
    catalog_key: str | None = None,
    serves_people: int | None = None,
) -> Product:
    """Um produto. A FILIAL sai da categoria, como em `create_product`.

    Nao ha parametro de filial aqui de proposito: a FK composta
    `(branch_id, category_id)` recusaria um produto que discordasse da
    categoria dele, e reproduzir essa possibilidade na fabrica so criaria um
    jeito de escrever teste que o banco nao aceita.
    """
    produto = Product(
        restaurant_id=restaurante.id,
        branch_id=categoria.branch_id,
        category_id=categoria.id,
        catalog_key=catalog_key,
        name=nome,
        slug=f"produto-{_sufixo()}",
        price=preco,
        is_active=is_active,
        is_available=is_available,
        sort_order=sort_order,
        serves_people=serves_people,
        code=code,
    )
    db.add(produto)
    db.flush()
    return produto


def criar_grupo_de_opcoes(
    db: Session,
    produto: Product,
    nome: str = "Grupo",
    is_required: bool = False,
    is_active: bool = True,
    sort_order: int = 0,
) -> ProductOptionGroup:
    grupo = ProductOptionGroup(
        product_id=produto.id,
        name=nome,
        sort_order=sort_order,
        min_select=1 if is_required else 0,
        max_select=1,
        is_required=is_required,
        is_active=is_active,
    )
    db.add(grupo)
    db.flush()
    return grupo


def criar_opcao(
    db: Session,
    grupo: ProductOptionGroup,
    nome: str = "Opcao",
    is_active: bool = True,
    additional_price: Decimal = Decimal("0.00"),
    sort_order: int = 0,
) -> ProductOption:
    opcao = ProductOption(
        option_group_id=grupo.id,
        name=nome,
        sort_order=sort_order,
        additional_price=additional_price,
        is_active=is_active,
    )
    db.add(opcao)
    db.flush()
    return opcao


def criar_cliente(
    db: Session,
    nome: str = "Cliente de Teste",
    email: str | None = None,
    phone: str | None = None,
    cpf: str | None = None,
    is_active: bool = True,
) -> Customer:
    sufixo = _sufixo()
    cliente = Customer(
        name=nome,
        email=email or f"cliente-{sufixo}@exemplo.com",
        phone=phone or f"85{sufixo[:9]}",
        cpf=cpf or sufixo[:11],
        password_hash="nao-e-hash-de-verdade",
        birth_date=date(1990, 1, 1),
        is_active=is_active,
    )
    db.add(cliente)
    db.flush()
    return cliente


def criar_endereco(
    db: Session,
    cliente: Customer,
    street: str = "Rua de Teste",
    is_default: bool = False,
    created_at: datetime | None = None,
) -> CustomerAddress:
    endereco = CustomerAddress(
        customer_id=cliente.id,
        street=street,
        number="100",
        neighborhood="Centro",
        is_default=is_default,
    )
    if created_at is not None:
        endereco.created_at = created_at
    db.add(endereco)
    db.flush()
    return endereco


def criar_pedido(
    db: Session,
    restaurante: Restaurant,
    filial: Branch,
    cliente: Customer | None = None,
    status: str = "pending",
    payment_status: str = "on_delivery",
    total: Decimal = Decimal("50.00"),
    created_at: datetime | None = None,
    customer_name_snapshot: str = "Cliente de Teste",
    customer_phone_snapshot: str = "85999999999",
    order_type: str = "delivery",
    order_number: int | None = None,
    tracking_token: str | None = None,
) -> Order:
    pedido = Order(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        customer_id=cliente.id if cliente else None,
        # O banco só guarda o hash. Um teste que precise do token em claro
        # (para chamar a rota de acompanhamento) passa `tracking_token`.
        tracking_token_hash=hash_tracking_token(tracking_token or f"token-{uuid.uuid4().hex}"),
        customer_name_snapshot=customer_name_snapshot,
        customer_phone_snapshot=customer_phone_snapshot,
        order_type=order_type,
        status=status,
        payment_status=payment_status,
        total=total,
    )
    # `created_at` tem server_default e é justamente o que os filtros de
    # período leem, então os testes de janela precisam poder escolhê-lo.
    if created_at is not None:
        pedido.created_at = created_at
    # `order_number` normalmente vem da sequence, e o valor dela depende de
    # tudo o que já rodou (é global e imune a rollback). Teste que precisa de
    # um número CONHECIDO — a busca do painel — passa o dele.
    if order_number is not None:
        pedido.order_number = order_number
    db.add(pedido)
    db.flush()
    return pedido


def instante(ano: int, mes: int, dia: int, hora: int = 12, minuto: int = 0) -> datetime:
    """Timestamp com fuso, porque as colunas são TIMESTAMPTZ.

    Comparar coluna com fuso contra `datetime` ingênuo levanta erro no
    psycopg — e o erro aparece longe de onde o valor foi montado.
    """
    return datetime(ano, mes, dia, hora, minuto, tzinfo=timezone.utc)
