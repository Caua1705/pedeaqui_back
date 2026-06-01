from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.customer_model import Customer


class CustomerRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_phone(self, phone: str) -> Customer | None:
        stmt = select(Customer).where(Customer.phone == phone)
        return self.db.scalar(stmt)

    def create(self, name: str, phone: str) -> Customer:
        customer = Customer(name=name, phone=phone)
        self.db.add(customer)
        self.db.flush()
        return customer
