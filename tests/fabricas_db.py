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
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from src.models.branch_model import Branch
from src.models.category_model import Category
from src.models.customer_model import Customer, CustomerAddress
from src.models.order_model import Order
from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.models.restaurant_model import Restaurant


def _sufixo() -> str:
    """Sufixo único para os campos com UNIQUE (slug, e-mail, telefone, CPF).

    Os testes rodam dentro de uma transação revertida, mas a mesma transação
    pode criar dois restaurantes — e aí o UNIQUE de `slug` bate.
    """
    return uuid.uuid4().hex[:12]


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


def criar_categoria(db: Session, restaurante: Restaurant, nome: str = "Categoria") -> Category:
    categoria = Category(
        restaurant_id=restaurante.id,
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
) -> Product:
    produto = Product(
        restaurant_id=restaurante.id,
        category_id=categoria.id,
        name=nome,
        slug=f"produto-{_sufixo()}",
        price=preco,
        is_active=is_active,
        is_available=is_available,
        sort_order=sort_order,
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
) -> ProductOptionGroup:
    grupo = ProductOptionGroup(
        product_id=produto.id,
        name=nome,
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
) -> ProductOption:
    opcao = ProductOption(
        option_group_id=grupo.id,
        name=nome,
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
) -> Order:
    pedido = Order(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        customer_id=cliente.id if cliente else None,
        tracking_token=f"token-{uuid.uuid4().hex}",
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
