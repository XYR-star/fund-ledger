from datetime import datetime
from zoneinfo import ZoneInfo


BUSINESS_TIMEZONE_NAME = "Asia/Shanghai"
BUSINESS_TIMEZONE = ZoneInfo(BUSINESS_TIMEZONE_NAME)


def now_shanghai_naive() -> datetime:
    return datetime.now(BUSINESS_TIMEZONE).replace(tzinfo=None)
