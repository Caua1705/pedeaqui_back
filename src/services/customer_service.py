import re
import unicodedata
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models.customer_model import Customer, CustomerAddress
from src.models.order_item_model import OrderItem
from src.models.order_model import Order
from src.repositories.customer_repository import CustomerRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.auth_schema import MessageResponse
from src.schemas.customer_schema import (
    ChangeCustomerPasswordRequest,
    CreateCustomerAddressRequest,
    CurrentCustomerResponse,
    CustomerAddressResponse,
    CustomerDataExportResponse,
    CustomerOrderHistoryItem,
    IgnoredImportedAddress,
    ImportCustomerAddressesRequest,
    ImportCustomerAddressesResponse,
    UpdateCurrentCustomerRequest,
    UpdateCustomerAddressRequest,
)
from src.schemas.order_schema import OrderItemResponse
from src.services.cashback_service import CashbackService
from src.utils.money import money_to_float, quantize_money
from src.utils.security import PasswordTooLongError, hash_password, utcnow, verify_password


# Teto de linhas de cashback no pacote de exportacao. Alto e fixo em vez de
# paginado: exportacao pela metade nao serve para o que ela existe, e o
# cliente com mais transacoes hoje tem duas casas.
MAX_EXPORT_CASHBACK_ROWS = 1000


class CustomerService:
    def __init__(self, db: Session):
        self.db = db
        self.customer_repository = CustomerRepository(db)
        self.order_repository = OrderRepository(db)

    def get_me(self, customer: Customer) -> CurrentCustomerResponse:
        return CurrentCustomerResponse(
            id=customer.id,
            name=customer.name,
            email=customer.email,
            phone=customer.phone,
            birth_date=customer.birth_date,
            email_verified=customer.email_verified_at is not None,
            marketing_opt_in=customer.marketing_opt_in,
        )

    def export_me(self, customer: Customer) -> CustomerDataExportResponse:
        """Tudo que a plataforma guarda sobre este cliente, num pacote so.

        Direito de acesso e portabilidade (LGPD, Art. 18, II e V). E montagem
        do que as rotas de perfil, pedidos, enderecos e cashback ja devolvem
        separadas — de proposito: exportacao que monta a propria consulta vira
        um segundo lugar onde o escopo do cliente pode ser esquecido, e o
        escopo aqui e a unica coisa que separa "meus dados" de "a base".

        O teto do cashback e alto e fixo em vez de paginado porque exportacao
        pela metade nao serve para o que ela existe. Cliente que passe disso
        e um caso a tratar quando existir; hoje o maior tem duas casas.
        """
        return CustomerDataExportResponse(
            exported_at=utcnow(),
            profile=self.get_me(customer),
            addresses=[
                CustomerAddressResponse.model_validate(address)
                for address in self.list_addresses(customer)
            ],
            orders=self.list_orders(customer),
            cashback=CashbackService(self.db).list_transactions(
                customer, limit=MAX_EXPORT_CASHBACK_ROWS, offset=0
            ),
        )

    def update_me(
        self,
        customer: Customer,
        payload: UpdateCurrentCustomerRequest,
    ) -> CurrentCustomerResponse:
        email_owner = self.customer_repository.get_by_email(payload.email)
        if email_owner and email_owner.id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="E-mail ja esta em uso",
            )

        phone_owner = self.customer_repository.get_by_phone(payload.phone)
        if phone_owner and phone_owner.id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Telefone ja esta em uso",
            )

        alteracoes = {
            "name": payload.name,
            "email": payload.email,
            "phone": payload.phone,
            "birth_date": payload.birth_date,
        }
        # Ausente e "nao mexi no consentimento"; `false` e "revoguei". Sem
        # esta distincao, quem editasse so o telefone revogaria o proprio
        # opt-in sem ter pedido.
        if payload.marketing_opt_in is not None:
            alteracoes["marketing_opt_in"] = payload.marketing_opt_in

        try:
            self.customer_repository.update(customer, **alteracoes)
            self.db.commit()
            self.db.refresh(customer)
        except IntegrityError as exc:
            self.db.rollback()
            email_owner = self.customer_repository.get_by_email(payload.email)
            if email_owner and email_owner.id != customer.id:
                detail = "E-mail ja esta em uso"
            else:
                detail = "Telefone ja esta em uso"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=detail,
            ) from exc
        except Exception:
            self.db.rollback()
            raise

        return self.get_me(customer)

    def change_password(
        self,
        customer: Customer,
        payload: ChangeCustomerPasswordRequest,
    ) -> MessageResponse:
        if not verify_password(payload.current_password, customer.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Senha atual incorreta",
            )
        if payload.new_password != payload.confirm_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="As senhas não conferem",
            )
        if len(payload.new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A nova senha deve ter pelo menos 8 caracteres",
            )
        try:
            password_hash = hash_password(payload.new_password)
        except PasswordTooLongError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A nova senha é muito longa",
            ) from exc

        try:
            customer.password_hash = password_hash
            # Invalida os tokens ja emitidos. O cliente que trocou a senha
            # neste aparelho tambem cai e precisa logar de novo — e o preco
            # de nao manter uma lista de sessoes ativas, e vale a pena: a
            # troca de senha e a unica ferramenta que o cliente tem para
            # expulsar quem entrou na conta dele.
            customer.password_changed_at = utcnow()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return MessageResponse(message="Senha alterada com sucesso")

    def list_orders(self, customer: Customer) -> list[CustomerOrderHistoryItem]:
        rows = self.order_repository.list_orders_by_customer(customer.id)
        return [
            self._order_history_item(order, restaurant_name, branch_name)
            for order, restaurant_name, branch_name in rows
        ]

    @classmethod
    def _order_history_item(
        cls,
        order: Order,
        restaurant_name: str,
        branch_name: str,
    ) -> CustomerOrderHistoryItem:
        return CustomerOrderHistoryItem(
            id=order.id,
            order_number=order.order_number,
            restaurant_name=restaurant_name,
            branch_name=branch_name,
            status=order.status,
            order_type=order.order_type,
            subtotal=money_to_float(order.subtotal),
            delivery_fee=money_to_float(order.delivery_fee),
            service_fee=money_to_float(order.service_fee),
            coupon_code=order.coupon_code_snapshot,
            # `money_to_float` e nao `quantize_money`: a resposta e o unico
            # lugar onde dinheiro vira float, e os quatro campos vizinhos ja
            # faziam assim.
            coupon_discount_amount=money_to_float(order.coupon_discount_amount),
            cashback_redeemed_amount=money_to_float(order.cashback_redeemed_amount),
            discount_total=money_to_float(order.discount_total),
            total=money_to_float(order.total),
            created_at=order.created_at,
            items=[cls._order_item_response(item) for item in order.items],
        )

    @staticmethod
    def _order_item_response(item: OrderItem) -> OrderItemResponse:
        # `unit_price_snapshot` JA vem com os adicionais somados (armadilha 2).
        # Nao some order_item_options aqui: o adicional entraria em dobro e o
        # total do pedido pararia de bater com a soma das linhas.
        return OrderItemResponse(
            id=item.id,
            product_id=item.product_id,
            product_code_snapshot=item.product_code_snapshot,
            product_name_snapshot=item.product_name_snapshot,
            product_description_snapshot=item.product_description_snapshot,
            unit_price_snapshot=money_to_float(item.unit_price_snapshot),
            quantity=item.quantity,
            observation=item.observation,
            total=money_to_float(item.total),
            created_at=item.created_at,
        )

    def list_addresses(self, customer: Customer) -> list[CustomerAddress]:
        return self.customer_repository.list_addresses(customer.id)

    def create_address(self, customer: Customer, payload: CreateCustomerAddressRequest) -> CustomerAddress:
        try:
            self.customer_repository.lock_customer(customer.id)
            existing_addresses = self.customer_repository.list_addresses(customer.id)
            is_default = payload.is_default or not existing_addresses
            if is_default:
                self.customer_repository.unset_default_addresses(customer.id)
            address = self.customer_repository.create_address(
                customer_id=customer.id,
                **payload.model_dump(exclude={"is_default"}),
                is_default=is_default,
            )
            self.db.commit()
            self.db.refresh(address)
            return address
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Nao foi possivel salvar o endereco",
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    def update_address(self, customer: Customer, address_id: UUID, payload: UpdateCustomerAddressRequest) -> CustomerAddress:
        self.customer_repository.lock_customer(customer.id)
        address = self._get_owned_address(customer, address_id)
        values = payload.model_dump(exclude_unset=True)
        for required_field in ("street", "number", "neighborhood"):
            if required_field in values and not values[required_field]:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Endereco incompleto")

        try:
            if values.pop("is_default", None):
                self.customer_repository.unset_default_addresses(customer.id)
                address.is_default = True
            for key, value in values.items():
                setattr(address, key, value)
            self.db.add(address)
            self.db.commit()
            self.db.refresh(address)
            return address
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Nao foi possivel atualizar o endereco",
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    def delete_address(self, customer: Customer, address_id: UUID) -> None:
        self.customer_repository.lock_customer(customer.id)
        address = self._get_owned_address(customer, address_id)
        was_default = address.is_default
        try:
            self.db.delete(address)
            self.db.flush()
            if was_default:
                remaining_addresses = self.customer_repository.list_addresses(customer.id)
                if remaining_addresses:
                    remaining_addresses[0].is_default = True
                    self.db.add(remaining_addresses[0])
            self.db.commit()
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Nao foi possivel remover o endereco",
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    def set_default_address(self, customer: Customer, address_id: UUID) -> CustomerAddress:
        self.customer_repository.lock_customer(customer.id)
        address = self._get_owned_address(customer, address_id)
        try:
            self.customer_repository.unset_default_addresses(customer.id)
            address.is_default = True
            self.db.add(address)
            self.db.commit()
            self.db.refresh(address)
            return address
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Nao foi possivel definir o endereco padrao",
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    def import_addresses(
        self,
        customer: Customer,
        payload: ImportCustomerAddressesRequest,
    ) -> ImportCustomerAddressesResponse:
        created: list[CustomerAddress] = []
        existing: list[CustomerAddress] = []
        ignored: list[IgnoredImportedAddress] = []

        try:
            self.customer_repository.lock_customer(customer.id)
            saved_addresses = self.customer_repository.list_addresses(customer.id)
            by_client_reference = {
                address.client_reference: address
                for address in saved_addresses
                if address.client_reference
            }
            by_fingerprint = {
                self._address_fingerprint(address): address
                for address in saved_addresses
            }
            request_keys: set[tuple[str, str]] = set()

            for imported in payload.addresses:
                fingerprint = self._address_fingerprint(imported)
                request_key = (
                    ("client_reference", imported.client_reference)
                    if imported.client_reference
                    else ("fingerprint", fingerprint)
                )
                if request_key in request_keys:
                    ignored.append(
                        IgnoredImportedAddress(
                            client_reference=imported.client_reference,
                            reason="duplicate_in_request",
                        )
                    )
                    continue
                request_keys.add(request_key)

                matched = (
                    by_client_reference.get(imported.client_reference)
                    if imported.client_reference
                    else by_fingerprint.get(fingerprint)
                )
                if matched is not None:
                    if imported.is_default and not matched.is_default:
                        self.customer_repository.unset_default_addresses(customer.id)
                        matched.is_default = True
                        self.db.add(matched)
                    existing.append(matched)
                    continue

                is_default = imported.is_default or not saved_addresses
                if is_default:
                    self.customer_repository.unset_default_addresses(customer.id)
                address = self.customer_repository.create_address(
                    customer_id=customer.id,
                    **imported.model_dump(exclude={"is_default"}),
                    is_default=is_default,
                )
                saved_addresses.append(address)
                if address.client_reference:
                    by_client_reference[address.client_reference] = address
                by_fingerprint[fingerprint] = address
                created.append(address)

            if saved_addresses and not any(address.is_default for address in saved_addresses):
                saved_addresses[0].is_default = True
                self.db.add(saved_addresses[0])

            self.db.commit()
            for address in created:
                self.db.refresh(address)
            for address in existing:
                self.db.refresh(address)
            return ImportCustomerAddressesResponse(
                created=created,
                existing=existing,
                ignored=ignored,
            )
        except IntegrityError as exc:
            self.db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Conflito ao importar enderecos",
            ) from exc
        except Exception:
            self.db.rollback()
            raise

    def _get_owned_address(self, customer: Customer, address_id: UUID) -> CustomerAddress:
        address = self.customer_repository.get_address(customer.id, address_id)
        if not address:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endereco nao encontrado")
        return address

    @classmethod
    def _address_fingerprint(cls, address) -> str:
        fields = (
            "street",
            "number",
            "neighborhood",
            "city",
            "state",
            "zipcode",
        )
        return "|".join(cls._normalize_address_value(getattr(address, field, None)) for field in fields)

    @staticmethod
    def _normalize_address_value(value: object | None) -> str:
        if value is None:
            return ""
        normalized = unicodedata.normalize("NFKD", str(value).strip().lower())
        normalized = "".join(
            character for character in normalized if not unicodedata.combining(character)
        )
        return re.sub(r"\s+", " ", normalized)
