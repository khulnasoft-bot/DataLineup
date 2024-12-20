import typing as t

import json

import asyncstdlib as alib
import pytest

from datalineup_engine.core import Cursor
from datalineup_engine.worker.inventories.chained import ChainedInventory
from datalineup_engine.worker.inventory import Inventory
from datalineup_engine.worker.inventory import Item


async def iterate_with_cursor(
    inventory: Inventory, after: t.Optional[Cursor] = None
) -> t.AsyncIterator[tuple[Item, t.Optional[Cursor]]]:
    async for item in inventory.run(after=after):
        async with item:
            pass
        yield item, inventory.cursor


@pytest.mark.asyncio
async def test_chained_inventory() -> None:
    inventory = ChainedInventory.from_options(
        {
            "inventories": [
                {
                    "name": "a",
                    "type": "StaticInventory",
                    "options": {"items": [{"a": 1}, {"a": 2}, {"a": 3}]},
                },
                {
                    "name": "b",
                    "type": "StaticInventory",
                    "options": {"items": [{"b": "1"}, {"b": "2"}, {"b": "3"}]},
                },
                {
                    "name": "c",
                    "type": "StaticInventory",
                    "options": {"items": [{"c": "1"}, {"c": "2"}, {"c": "3"}]},
                },
            ],
            "batch_size": 10,
        },
        services=None,
    )
    batch = await alib.list(iterate_with_cursor(inventory))
    assert [(json.loads(i.id), json.loads(c or ""), i.args) for i, c in batch] == [
        ({"a": "0"}, {"a": '{"v": 1, "a": "0"}'}, {"a": {"a": 1}}),
        ({"a": "1"}, {"a": '{"v": 1, "a": "1"}'}, {"a": {"a": 2}}),
        ({"a": "2"}, {"a": '{"v": 1, "a": "2"}'}, {"a": {"a": 3}}),
        ({"b": "0"}, {"b": '{"v": 1, "a": "0"}'}, {"b": {"b": "1"}}),
        ({"b": "1"}, {"b": '{"v": 1, "a": "1"}'}, {"b": {"b": "2"}}),
        ({"b": "2"}, {"b": '{"v": 1, "a": "2"}'}, {"b": {"b": "3"}}),
        ({"c": "0"}, {"c": '{"v": 1, "a": "0"}'}, {"c": {"c": "1"}}),
        ({"c": "1"}, {"c": '{"v": 1, "a": "1"}'}, {"c": {"c": "2"}}),
        ({"c": "2"}, {"c": '{"v": 1, "a": "2"}'}, {"c": {"c": "3"}}),
    ]

    batch = await alib.list(iterate_with_cursor(inventory, after=Cursor('{"b": "1"}')))
    assert [(json.loads(i.id), json.loads(c or ""), i.args) for i, c in batch] == [
        ({"b": "2"}, {"b": '{"v": 1, "a": "2"}'}, {"b": {"b": "3"}}),
        ({"c": "0"}, {"c": '{"v": 1, "a": "0"}'}, {"c": {"c": "1"}}),
        ({"c": "1"}, {"c": '{"v": 1, "a": "1"}'}, {"c": {"c": "2"}}),
        ({"c": "2"}, {"c": '{"v": 1, "a": "2"}'}, {"c": {"c": "3"}}),
    ]
    assert not await alib.list(inventory.iterate(after=Cursor('{"c": "2"}')))
