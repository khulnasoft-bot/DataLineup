from typing import Optional

import asyncio

from datalineup_engine.config import default_config_with_env
from datalineup_engine.core import Cursor
from datalineup_engine.utils.options import asdict
from datalineup_engine.worker import work_factory
from datalineup_engine.worker.services.manager import ServicesManager
from datalineup_engine.worker_manager.config.static_definitions import StaticDefinitions

from .config.inventory_test import InventoryTest
from .diff import get_diff


def run_datalineup_inventory(
    *,
    static_definitions: StaticDefinitions,
    inventory_name: str,
    limit: Optional[int] = None,
    after: Optional[Cursor] = None,
) -> list[dict]:
    inventory_item = static_definitions.inventories[inventory_name]
    inventory = work_factory.build_inventory(
        inventory_item=inventory_item,
        services=ServicesManager(
            config=default_config_with_env(),
        ).services,
    )
    items: list[dict] = []

    async def run_inventory() -> None:
        count = 0
        async for item in inventory.iterate(after=after):
            async with item:
                items.append(asdict(item.as_topic_message()))
                count = count + 1
                if limit and count >= limit:
                    break

    asyncio.run(run_inventory())

    return items


def run_datalineup_inventory_test(
    *,
    static_definitions: StaticDefinitions,
    inventory_test: InventoryTest,
) -> None:
    items: list[dict] = run_datalineup_inventory(
        static_definitions=static_definitions,
        inventory_name=inventory_test.spec.selector.inventory,
        limit=inventory_test.spec.limit,
        after=inventory_test.spec.after,
    )

    expected_items: list[dict] = [
        asdict(item.as_topic_message()) for item in inventory_test.spec.items
    ]

    if items != expected_items:
        diff: str = get_diff(
            expected=expected_items,
            got=items,
        )
        raise AssertionError(
            f"Inventory items do not match the expected items:\n{diff}"
        )
