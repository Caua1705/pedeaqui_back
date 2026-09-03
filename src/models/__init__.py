from src.models.admin_error_report_model import AdminErrorReport
from src.models.admin_user_model import AdminUser
from src.models.ai_feedback_model import AIFeedback
from src.models.ai_product_embedding_model import AIProductEmbedding
from src.models.ai_usage_event_model import AIUsageEvent
from src.models.ai_voice_session_model import AIVoiceSession
from src.models.branch_model import Branch
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_delivery_time_band_model import BranchDeliveryTimeBand
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.models.cashback_rule_model import CashbackRule, CashbackRuleWeekday
from src.models.cashback_transaction_model import CashbackTransaction
from src.models.category_model import Category
from src.models.coupon_claim_model import CouponClaim
from src.models.coupon_model import CouponTemplate, RestaurantCoupon
from src.models.coupon_redemption_model import CouponRedemption
from src.models.courier_model import Courier, CourierAssignment
from src.models.customer_model import (
    AccountDeletionCode,
    Customer,
    CustomerAddress,
    EmailVerificationCode,
    PasswordResetCode,
)
from src.models.customer_saved_card_model import CustomerPaymentProfile, CustomerSavedCard
from src.models.customer_social_identity_model import CustomerSocialIdentity
# `delivery_estimates` faltava aqui, e o efeito nao era um import quebrado:
# `alembic/env.py` monta o `target_metadata` a partir de `import src.models`,
# entao a tabela simplesmente NAO EXISTIA para o autogenerate — que propoe
# `DROP TABLE` em tudo que nao acha no ORM (armadilha 24). Ela sobrevivia
# porque o app a importa por outro caminho (o repositorio), e porque a regra
# manda ler o arquivo gerado antes de aplicar. Modelo novo entra NESTA lista.
from src.models.delivery_estimate_model import DeliveryEstimate
from src.models.idempotency_key_model import IdempotencyKey
from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.models.order_review_model import OrderReview
from src.models.order_status_history_model import OrderStatusHistory
from src.models.print_agent_model import PrintAgent, PrintAgentCommand, PrintAgentPrinter
from src.models.printing_sector_model import PrintingSector
from src.models.product_model import Product
from src.models.product_option_model import ProductOption, ProductOptionGroup
from src.models.restaurant_banner_model import RestaurantBanner
from src.models.restaurant_model import Restaurant
from src.models.restaurant_payment_credential_model import RestaurantPaymentCredential
from src.models.restaurant_setting_model import RestaurantSetting

__all__ = [
    "AccountDeletionCode",
    "AdminErrorReport",
    "AdminUser",
    "AIFeedback",
    "AIProductEmbedding",
    "AIUsageEvent",
    "AIVoiceSession",
    "Branch",
    "BranchBusinessHour",
    "BranchDeliveryTimeBand",
    "BranchPaymentMethod",
    "CashbackRule",
    "CashbackRuleWeekday",
    "CashbackTransaction",
    "Category",
    "CouponClaim",
    "CouponTemplate",
    "CouponRedemption",
    "Courier",
    "CourierAssignment",
    "Customer",
    "CustomerAddress",
    "CustomerPaymentProfile",
    "CustomerSavedCard",
    "CustomerSocialIdentity",
    "DeliveryEstimate",
    "EmailVerificationCode",
    "IdempotencyKey",
    "PasswordResetCode",
    "Order",
    "OrderItem",
    "OrderItemOption",
    "OrderReview",
    "OrderStatusHistory",
    "PrintAgent",
    "PrintAgentCommand",
    "PrintAgentPrinter",
    "PrintingSector",
    "Product",
    "ProductOption",
    "ProductOptionGroup",
    "Restaurant",
    "RestaurantCoupon",
    "RestaurantBanner",
    "RestaurantPaymentCredential",
    "RestaurantSetting",
]
