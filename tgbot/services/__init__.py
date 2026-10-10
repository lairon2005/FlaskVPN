from database import (
    user_repo, tariff_repo, promo_repo, channel_repo, stats_repo,
    payment_repo, payment_method_repo, lifecycle_repo, settings_repo,
    manager_repo, manager_client_repo, manager_op_repo, temp_key_repo, client_access_repo,
    partner_repo,
)
from loader import remnawave_client, bot, config

from .subscription_service import SubscriptionService
from .referral_service import ReferralService
from .promo_code_service import PromoCodeService
from .user_service import UserService
from .profile_service import ProfileService
from .device_service import DeviceService
from .device_slot_service import DeviceSlotService
from .traffic_service import TrafficService
from .key_service import KeyService
from .admin_stats_service import AdminStatsService
from .payment_service import PaymentService
from .payment_method_service import PaymentMethodService
from .support_service import SupportService
from .manager_service import ManagerService
from .manager_notifier import ManagerNotifier
from .partner_service import PartnerService
from .partner_notifier import PartnerNotifier
from .payment import create_payment

# traffic_service создаётся первым: базовую квоту новых пользователей
# subscription_service спрашивает у него.
traffic_service = TrafficService(user_repo, settings_repo, remnawave_client)
subscription_service = SubscriptionService(
    user_repo, remnawave_client, base_traffic_gb=traffic_service.base_gb
)
referral_service = ReferralService(user_repo, subscription_service, partner_repo=partner_repo)
promo_service = PromoCodeService(promo_repo, user_repo)
user_service = UserService(user_repo, stats_repo)
profile_service = ProfileService(user_repo, remnawave_client)
device_service = DeviceService(user_repo, remnawave_client)
device_slot_service = DeviceSlotService(user_repo, settings_repo, remnawave_client)
key_service = KeyService(user_repo, remnawave_client)
admin_stats_service = AdminStatsService(stats_repo, remnawave_client, payment_repo, lifecycle_repo,
                                         partner_repo=partner_repo)
# payment_method_service создаётся ДО payment_service — тот принимает его зависимостью
payment_method_service = PaymentMethodService(payment_method_repo)
payment_service = PaymentService(
    subscription_service, referral_service, user_repo, tariff_repo, payment_repo,
    payment_method_service, device_slot_service, promo_service,
    traffic_service=traffic_service, settings_repo=settings_repo,
)
support_service = SupportService(user_repo)

# Менеджеры офлайн-продаж. Нотификатор подключается после создания сервиса:
# сервис отдаёт готовые тексты, а доставка в Telegram — его забота.
manager_service = ManagerService(
    manager_repo=manager_repo, client_repo=manager_client_repo, op_repo=manager_op_repo,
    temp_repo=temp_key_repo, access_repo=client_access_repo, user_repo=user_repo,
    tariff_repo=tariff_repo, payment_service=payment_service, subscription_service=subscription_service,
    traffic_service=traffic_service, device_slot_service=device_slot_service,
    remnawave=remnawave_client, settings_repo=settings_repo, create_payment=create_payment,
    config=config,
)
manager_service.notifier = ManagerNotifier(bot, config)

# Партнёры (денежная рефералка). Конвейер оплат зовёт сервис для начислений и сторно,
# а сервис платит с баланса через тот же конвейер — поэтому подключение после создания.
partner_service = PartnerService(
    partner_repo, user_repo, settings_repo, tariff_repo=tariff_repo, payment_service=payment_service,
)
partner_service.notifier = PartnerNotifier(bot, config)
payment_service.partner_service = partner_service
