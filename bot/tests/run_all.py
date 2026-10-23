"""Запуск всех тестов:  python tests/run_all.py  (на временных БД, боевые данные не затрагиваются)"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_abuse  # noqa: E402
import test_mailing_gift  # noqa: E402
import test_ops  # noqa: E402
import test_menu  # noqa: E402
import test_payments  # noqa: E402
import test_promo  # noqa: E402
import test_referrals  # noqa: E402
import test_retention  # noqa: E402
import test_revoke  # noqa: E402
import test_stage1  # noqa: E402
import test_tariffs  # noqa: E402
import test_trial_guard  # noqa: E402
import test_whitelist  # noqa: E402
import test_wiring  # noqa: E402


async def main():
    for module in (test_payments, test_stage1, test_promo, test_revoke, test_whitelist, test_retention, test_abuse, test_referrals, test_tariffs, test_trial_guard, test_menu, test_mailing_gift, test_ops, test_wiring):
        print(f"== {module.__name__}")
        await module.main()
    print("\nВсе тесты пройдены")


if __name__ == "__main__":
    asyncio.run(main())
