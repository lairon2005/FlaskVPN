from db import async_session_maker
from database.repositories.user import UserRepository
from database.repositories.tariff import TariffRepository
from database.repositories.promo_code import PromoCodeRepository
from database.repositories.channel import ChannelRepository
from database.repositories.stats import StatsRepository
from database.repositories.payment import PaymentRepository
from database.repositories.payment_method import PaymentMethodRepository
from database.repositories.lifecycle import LifecycleRepository
from database.repositories.settings import SettingsRepository
from database.repositories.manager import ManagerRepository, ManagerClientRepository
from database.repositories.manager_operation import ManagerOperationRepository
from database.repositories.temp_key import TempKeyRepository
from database.repositories.client_access import ClientAccessRepository
from database.repositories.partner import PartnerRepository

user_repo = UserRepository(async_session_maker)
tariff_repo = TariffRepository(async_session_maker)
promo_repo = PromoCodeRepository(async_session_maker)
channel_repo = ChannelRepository(async_session_maker)
stats_repo = StatsRepository(async_session_maker)
payment_repo = PaymentRepository(async_session_maker)
payment_method_repo = PaymentMethodRepository(async_session_maker)
lifecycle_repo = LifecycleRepository(async_session_maker)
settings_repo = SettingsRepository(async_session_maker)
manager_repo = ManagerRepository(async_session_maker)
manager_client_repo = ManagerClientRepository(async_session_maker)
manager_op_repo = ManagerOperationRepository(async_session_maker)
temp_key_repo = TempKeyRepository(async_session_maker)
client_access_repo = ClientAccessRepository(async_session_maker)
partner_repo = PartnerRepository(async_session_maker)
