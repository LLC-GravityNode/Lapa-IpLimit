import asyncio
from collections import Counter, defaultdict
from typing import Dict, List, Set

from telegram_bot.send_message import send_logs
from utils.logs import logger
from utils.panel_api import disable_user
from utils.read_config import read_config
from utils.types import PanelType, UserType

ACTIVE_USERS: dict[str, UserType] | dict = {}

# Новое: глобальный счётчик серий по IP
IP_STREAKS: dict[str, dict[str, int]] = defaultdict(dict)


def _ru_plurals_logs(n: int) -> str:
    """
    Подбирает правильное слово для 'лог' на русском.
    1 -> 'лог', 2..4 -> 'лога', 5..20 -> 'логов', дальше по правилам.
    """
    n_abs = abs(n)
    if 11 <= (n_abs % 100) <= 14:
        return "логов"
    last = n_abs % 10
    if last == 1:
        return "лог"
    if 2 <= last <= 4:
        return "лога"
    return "логов"


async def check_ip_used() -> dict[str, List[str]]:
    """
    Собираем текущие активные IP по каждому пользователю за этот прогон.
    Возвращаем словарь: email -> список активных IP (уникальных).
    """
    all_users_log: dict[str, List[str]] = {}
    for email in list(ACTIVE_USERS.keys()):
        data = ACTIVE_USERS[email]

        # Если data.ip — это список всех срабатываний за период,
        # берём уникальные как «активные» на этот прогон:
        unique_ips = list(dict.fromkeys(data.ip))  # сохраняем порядок появления
        all_users_log[email] = unique_ips

        logger.info("User snapshot: %s -> %s", email, unique_ips)

    # Просто для метрики в логах:
    total_ips = sum(len(ips) for ips in all_users_log.values())
    logger.info("Количество всех активных IP-адресов за прогон: %s", str(total_ips))

    return all_users_log


def _update_ip_streaks(current_active: dict[str, List[str]]) -> None:
    """
    Обновляет глобальные IP_STREAKS на основе текущих активных IP.
    """
    # 1) инкремент для активных
    for user, ips in current_active.items():
        curr_set: Set[str] = set(ips)
        # увеличиваем streak для активных IP
        for ip in curr_set:
            IP_STREAKS[user][ip] = IP_STREAKS[user].get(ip, 0) + 1

        # обнуляем/удаляем IP, которые пропали у пользователя
        stale_ips = [ip for ip in IP_STREAKS[user].keys() if ip not in curr_set]
        for ip in stale_ips:
            del IP_STREAKS[user][ip]

    # 2) пользователи, которые полностью пропали в этом прогоне
    stale_users = [u for u in IP_STREAKS.keys() if u not in current_active]
    for u in stale_users:
        del IP_STREAKS[u]


def _format_streak_messages() -> List[str]:
    """
    Формирует текстовые блоки в нужном тебе формате.
    Сортируем пользователей по убыванию числа активных IP,
    а внутри — по убыванию длины серии.
    """
    # Подготовим удобную структуру
    users_sorted = sorted(
        IP_STREAKS.items(),
        key=lambda kv: len(kv[1]),
        reverse=True,
    )

    blocks: List[str] = []
    for user, ip_map in users_sorted:
        if not ip_map:
            continue
        # сортируем IP внутри по убыванию streak
        ips_sorted = sorted(ip_map.items(), key=lambda kv: kv[1], reverse=True)
        header = f"{user} всего {len(ips_sorted)} активных ip"
        lines = []
        for ip, streak in ips_sorted:
            word = _ru_plurals_logs(streak)
            lines.append(f"- {ip} ({streak} {word} подряд)")
        blocks.append(header + "  \n" + "\n".join(lines))
    return blocks


async def check_users_usage(panel_data: PanelType):
    """
    checks the usage of active users
    + обновляет счётчики серий и отправляет логи в нужном формате
    """
    config_data = await read_config()
    all_users_log = await check_ip_used()

    # обновляем глобальные streak-и
    _update_ip_streaks(all_users_log)

    # отправляем логи по формату
    messages = _format_streak_messages()
    total_ips = sum(len(v) for v in IP_STREAKS.values())
    messages.append(f"---------\nВсего активных IP: <b>{total_ips}</b>")

    # режем на части, если сообщений много
    shorter_messages = ["\n\n".join(messages[i : i + 50]) for i in range(0, len(messages), 50)]
    for chunk in shorter_messages:
        await send_logs(chunk)

    # дальше — твоя логика лимитов/отключений,
    # считаем лимит по КОЛИЧЕСТВУ активных IP у юзера в текущий момент
    except_users = config_data.get("EXCEPT_USERS", [])
    special_limit = config_data.get("SPECIAL_LIMIT", {})
    limit_number = config_data["GENERAL_LIMIT"]

    for user_name, ip_map in IP_STREAKS.items():
        if user_name in except_users:
            continue
        user_limit_number = int(special_limit.get(user_name, limit_number))
        # активные IP сейчас — это ключи в IP_STREAKS[user_name]
        active_ip_count = len(ip_map)
        if active_ip_count > user_limit_number:
            message = (
                f"User {user_name} has {active_ip_count} active ips. {str(set(ip_map.keys()))}"
            )
            logger.warning(message)
            await send_logs(str("<b>Warning: </b>" + message))
            try:
                await disable_user(panel_data, UserType(name=user_name, ip=[]))
            except ValueError as error:
                print(error)

    # ВАЖНО: НЕ чистим IP_STREAKS и НЕ чистим all_users_log здесь.
    # Нам нужна «память» серий на следующий прогон.
    ACTIVE_USERS.clear()  # очищаем снапшот прогонa (как и было)


async def run_check_users_usage(panel_data: PanelType) -> None:
    """run check_ip_used() function and then run check_users_usage()"""
    while True:
        await check_users_usage(panel_data)
        data = await read_config()
        await asyncio.sleep(int(data["CHECK_INTERVAL"]))