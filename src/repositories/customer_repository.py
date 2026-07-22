import uuid

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.customer_model import Customer, CustomerAddress, EmailVerificationCode, PasswordResetCode


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

    def get_by_cpf(self, cpf: str) -> Customer | None:
        stmt = select(Customer).where(Customer.cpf == cpf)
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
        stmt = select(EmailVerificationCode).where(
            EmailVerificationCode.email == email,
            EmailVerificationCode.created_at >= since,
        )
        return len(self.db.scalars(stmt).all())

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
        stmt = select(PasswordResetCode).where(
            PasswordResetCode.email == email,
            PasswordResetCode.created_at >= since,
        )
        return len(self.db.scalars(stmt).all())

    def invalidate_unused_password_reset_codes(self, customer_id: uuid.UUID) -> None:
        for code in self.db.scalars(
            select(PasswordResetCode).where(
                PasswordResetCode.customer_id == customer_id,
                PasswordResetCode.used_at.is_(None),
            )
        ).all():
            code.used_at = datetime.now(code.expires_at.tzinfo)
            self.db.add(code)
        self.db.flush()

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
        for address in self.list_addresses(customer_id):
            if address.is_default:
                address.is_default = False
                self.db.add(address)
        self.db.flush()
