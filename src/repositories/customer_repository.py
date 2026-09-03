import uuid

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.models.customer_model import (
    AccountDeletionCode,
    Customer,
    CustomerAddress,
    EmailVerificationCode,
    PasswordResetCode,
)


#: As tabelas de codigo de seis digitos, que a retencao e a exclusao de conta
#: varrem JUNTAS. Lista em UM lugar so, e nao repetida nos dois `for`: uma
#: tabela nova de codigo entra aqui e passa a ser apagada nos dois caminhos —
#: e ela guarda o e-mail em TEXTO PURO, entao ficar de fora de qualquer um dos
#: dois deixa dado pessoal legivel num canto do mesmo banco.
TABELAS_DE_CODIGO = (EmailVerificationCode, PasswordResetCode, AccountDeletionCode)


class CustomerRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_phone(self, phone: str) -> Customer | None:
        stmt = select(Customer).where(Customer.phone == phone)
        return self.db.scalar(stmt)

    def get_by_id(self, customer_id: uuid.UUID) -> Customer | None:
        stmt = select(Customer).where(Customer.id == customer_id)
        return self.db.scalar(stmt)

    def lock_customer(self, customer_id: uuid.UUID) -> Customer | None:
        stmt = select(Customer).where(Customer.id == customer_id).with_for_update()
        return self.db.scalar(stmt)

    def get_by_email(self, email: str) -> Customer | None:
        stmt = select(Customer).where(Customer.email == email)
        return self.db.scalar(stmt)

    def get_by_email_or_phone(self, email: str | None, phone: str | None) -> Customer | None:
        if email:
            return self.get_by_email(email)
        if phone:
            return self.get_by_phone(phone)
        return None

    def create(self, **values) -> Customer:
        customer = Customer(**values)
        self.db.add(customer)
        self.db.flush()
        return customer

    def update(self, customer: Customer, **values) -> Customer:
        for field, value in values.items():
            setattr(customer, field, value)
        self.db.flush()
        return customer

    def delete_codes_created_before(self, cutoff: datetime) -> int:
        """Apaga os codigos velhos das tres tabelas. Devolve quantos sairam.

        Corta por `created_at`, e nao por `expires_at`: a linha continua
        servindo ao teto de reenvios e ao token de reset DEPOIS de o codigo
        vencer. Quem sabe ate quando e `auth_service.codes_retention_cutoff`,
        e e de la que o `cutoff` tem que vir — o repositorio so consulta.
        """
        apagados = 0
        for modelo in TABELAS_DE_CODIGO:
            resultado = self.db.execute(
                delete(modelo).where(modelo.created_at < cutoff)
            )
            apagados += resultado.rowcount or 0
        return apagados

    def create_email_code(self, **values) -> EmailVerificationCode:
        code = EmailVerificationCode(**values)
        self.db.add(code)
        self.db.flush()
        return code

    def latest_unused_email_code(self, email: str) -> EmailVerificationCode | None:
        stmt = (
            select(EmailVerificationCode)
            .where(EmailVerificationCode.email == email, EmailVerificationCode.used_at.is_(None))
            .order_by(EmailVerificationCode.created_at.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def count_email_codes_since(self, email: str, since: datetime) -> int:
        # COUNT no banco. Antes eram todas as linhas trazidas para a memoria
        # so para chamar len() nelas — no caminho de um cliente insistindo em
        # reenviar, e a cada tentativa.
        stmt = select(func.count()).select_from(EmailVerificationCode).where(
            EmailVerificationCode.email == email,
            EmailVerificationCode.created_at >= since,
        )
        return self.db.scalar(stmt) or 0

    def create_deletion_code(self, **values) -> AccountDeletionCode:
        code = AccountDeletionCode(**values)
        self.db.add(code)
        self.db.flush()
        return code

    def latest_unused_deletion_code(self, email: str) -> AccountDeletionCode | None:
        """O ultimo codigo de EXCLUSAO nao usado deste e-mail.

        Nao existe versao desta consulta que enxergue as outras duas tabelas, e
        e essa a defesa: um codigo pedido para verificar o e-mail ou para ligar
        a conta do Google nao tem como apagar a conta, e o contrario tambem
        nao. Com uma tabela so e uma coluna `purpose`, a defesa seria um
        `WHERE` que alguem esquece.
        """
        stmt = (
            select(AccountDeletionCode)
            .where(
                AccountDeletionCode.email == email,
                AccountDeletionCode.used_at.is_(None),
            )
            .order_by(AccountDeletionCode.created_at.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def count_deletion_codes_since(self, email: str, since: datetime) -> int:
        stmt = select(func.count()).select_from(AccountDeletionCode).where(
            AccountDeletionCode.email == email,
            AccountDeletionCode.created_at >= since,
        )
        return self.db.scalar(stmt) or 0

    def create_password_reset_code(self, **values) -> PasswordResetCode:
        code = PasswordResetCode(**values)
        self.db.add(code)
        self.db.flush()
        return code

    def latest_unused_password_reset_code(self, email: str) -> PasswordResetCode | None:
        stmt = (
            select(PasswordResetCode)
            .where(PasswordResetCode.email == email, PasswordResetCode.used_at.is_(None))
            .order_by(PasswordResetCode.created_at.desc())
            .limit(1)
        )
        return self.db.scalar(stmt)

    def get_password_reset_by_token_hash(self, token_hash: str) -> PasswordResetCode | None:
        stmt = select(PasswordResetCode).where(PasswordResetCode.reset_token_hash == token_hash)
        return self.db.scalar(stmt)

    def count_password_reset_codes_since(self, email: str, since: datetime) -> int:
        stmt = select(func.count()).select_from(PasswordResetCode).where(
            PasswordResetCode.email == email,
            PasswordResetCode.created_at >= since,
        )
        return self.db.scalar(stmt) or 0

    def invalidate_unused_password_reset_codes(self, customer_id: uuid.UUID) -> None:
        agora = datetime.now(timezone.utc)
        for code in self._unused_password_reset_codes(customer_id):
            code.used_at = agora
            self.db.add(code)
        self.db.flush()

    def _unused_password_reset_codes(self, customer_id: uuid.UUID) -> list[PasswordResetCode]:
        stmt = select(PasswordResetCode).where(
            PasswordResetCode.customer_id == customer_id,
            PasswordResetCode.used_at.is_(None),
        )
        return list(self.db.scalars(stmt).all())

    def list_addresses(self, customer_id: uuid.UUID) -> list[CustomerAddress]:
        stmt = (
            select(CustomerAddress)
            .where(CustomerAddress.customer_id == customer_id)
            .order_by(CustomerAddress.is_default.desc(), CustomerAddress.created_at.desc())
        )
        return list(self.db.scalars(stmt).all())

    def get_address(self, customer_id: uuid.UUID, address_id: uuid.UUID) -> CustomerAddress | None:
        stmt = select(CustomerAddress).where(
            CustomerAddress.id == address_id,
            CustomerAddress.customer_id == customer_id,
        )
        return self.db.scalar(stmt)

    def create_address(self, **values) -> CustomerAddress:
        address = CustomerAddress(**values)
        self.db.add(address)
        self.db.flush()
        return address

    def unset_default_addresses(self, customer_id: uuid.UUID) -> None:
        # A consulta ja filtra por is_default: antes, ela trazia TODOS os
        # enderecos (ordenados, ainda por cima) para descartar quase todos num
        # `if` logo em seguida.
        for address in self._default_addresses(customer_id):
            address.is_default = False
            self.db.add(address)
        self.db.flush()

    def _default_addresses(self, customer_id: uuid.UUID) -> list[CustomerAddress]:
        stmt = select(CustomerAddress).where(
            CustomerAddress.customer_id == customer_id,
            CustomerAddress.is_default.is_(True),
        )
        return list(self.db.scalars(stmt).all())

    def delete_addresses_of(self, customer_id: uuid.UUID) -> int:
        """Apaga os enderecos salvos da pessoa. Devolve quantos sairam.

        `orders.customer_address_id` tem `ON DELETE SET NULL`, entao o DELETE
        NAO falha e nao leva pedido junto: ele so solta o vinculo. O endereco
        entregue continua no pedido, no snapshot dele — e e por isso que
        apagar esta tabela nao basta para tirar o endereco do banco.
        """
        resultado = self.db.execute(
            delete(CustomerAddress).where(CustomerAddress.customer_id == customer_id)
        )
        return resultado.rowcount or 0

    def delete_codes_of(self, customer_id: uuid.UUID) -> int:
        """Apaga os codigos de e-mail, de recuperacao e de exclusao da pessoa.

        As TRES tabelas guardam o e-mail em TEXTO PURO, numa copia fora de
        `customers`. Sem este passo, anonimizar o cliente deixaria o endereco
        dele legivel em outro lugar do mesmo banco.

        A de exclusao entrou na revisao 0050 e e a que fecha o circulo: a
        propria linha que autorizou a exclusao guarda o e-mail de quem pediu.
        """
        apagados = 0
        for modelo in TABELAS_DE_CODIGO:
            resultado = self.db.execute(
                delete(modelo).where(modelo.customer_id == customer_id)
            )
            apagados += resultado.rowcount or 0
        return apagados
