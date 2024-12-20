import typing as t

import asyncio
from collections.abc import Awaitable
from datetime import datetime
from datetime import timedelta

import asyncstdlib as alib
import pytest
from aiormq.exceptions import AMQPConnectionError

from datalineup_engine.config import Config
from datalineup_engine.core import MessageId
from datalineup_engine.core import TopicMessage
from datalineup_engine.utils import utcnow
from datalineup_engine.worker.services.manager import ServicesManager
from datalineup_engine.worker.services.rabbitmq import RabbitMQService
from datalineup_engine.worker.topic import TopicClosedError
from datalineup_engine.worker.topics import RabbitMQTopic
from datalineup_engine.worker.topics.rabbitmq import Exchange
from datalineup_engine.worker.topics.rabbitmq import RabbitMQSerializer
from tests.utils.tcp_proxy import TcpProxy
from tests.worker.topics.conftest import RabbitMQTopicMaker


async def unwrap(context: t.AsyncContextManager[TopicMessage]) -> TopicMessage:
    async with context as message:
        return message


@pytest.mark.asyncio
async def test_rabbitmq_topic_simple(
    rabbitmq_topic_maker: RabbitMQTopicMaker,
) -> None:
    topic = await rabbitmq_topic_maker(RabbitMQTopic)

    messages = [
        TopicMessage(id=MessageId("0"), args={"n": 1}),
        TopicMessage(id=MessageId("1"), args={"n": 2}),
    ]

    for message in messages:
        await topic.publish(message, wait=True)

    async with alib.scoped_iter(topic.run()) as topic_iter:
        items = []
        async for context in alib.islice(topic_iter, 2):
            async with context as message:
                items.append(message)
        assert items == messages

    await topic.close()


@pytest.mark.asyncio
async def test_rabbitmq_topic_pickle(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(
        RabbitMQTopic, serializer=RabbitMQSerializer.PICKLE
    )

    messages = [
        TopicMessage(id=MessageId("0"), args={"n": b"1", "time": utcnow()}),
        TopicMessage(id=MessageId("1"), args={"n": b"2", "time": utcnow()}),
    ]

    for message in messages:
        await topic.publish(message, wait=True)

    async with alib.scoped_iter(topic.run()) as topic_iter:
        items = []
        async for context in alib.islice(topic_iter, 2):
            async with context as message:
                items.append(message)
        assert items == messages

    await topic.close()


@pytest.mark.asyncio
async def test_bounded_rabbitmq_topic_max_length(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(RabbitMQTopic, max_length=2, prefetch_count=2)
    topic.RETRY_PUBLISH_DELAY = timedelta(seconds=0.1)

    message = TopicMessage(id=MessageId("0"), args={"n": 1})

    assert await topic.publish(message, wait=False)
    assert await topic.publish(message, wait=True)
    assert not await topic.publish(message, wait=False)
    publish_task = asyncio.create_task(topic.publish(message, wait=True))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(publish_task), 0.5)

    async with alib.scoped_iter(topic.run()) as topic_iter:
        assert await unwrap(await alib.anext(topic_iter)) == message
        assert await publish_task

        # We can still publish two more message, because at that point 2
        # messages are waiting on the consumer buffer, so the queue is empty.
        assert await topic.publish(message, wait=True)
        assert await topic.publish(message, wait=True)

        # However one more and we fill the queue again.
        publish_task = asyncio.create_task(topic.publish(message, wait=True))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(publish_task), 0.5)

        assert await unwrap(await alib.anext(topic_iter)) == message
        assert await unwrap(await alib.anext(topic_iter)) == message
        assert await publish_task

    await topic.close()


@pytest.mark.asyncio
async def test_rabbitmq_topic_channel_closed(
    services_manager_maker: t.Callable[[Config], t.Awaitable[ServicesManager]],
    config: Config,
    tcp_proxy: t.Callable[[int, int], Awaitable[TcpProxy]],
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]],
    rabbitmq_service_loader: t.Callable[..., Awaitable[RabbitMQService]],
) -> None:
    proxy = await tcp_proxy(15672, 5672)
    config = config.load_object(
        {
            "rabbitmq": {
                "urls": {"proxy": "amqp://127.0.0.1:15672/"},
                "reconnect_interval": 1,
            }
        }
    )
    services_manager = await services_manager_maker(config)

    topic = await rabbitmq_topic_maker(
        RabbitMQTopic,
        services_manager=services_manager,
        durable=True,
        auto_delete=False,
        connection_name="proxy",
    )

    reader = await rabbitmq_topic_maker(
        RabbitMQTopic,
        services_manager=services_manager,
        durable=True,
        auto_delete=False,
        connection_name="proxy",
    )

    message = TopicMessage(id=MessageId("0"), args={"n": 1})

    async with alib.scoped_iter(reader.run()) as topic_iter:
        assert await topic.publish(message, wait=False)
        assert await topic.publish(message, wait=False)

        assert await unwrap(await alib.anext(topic_iter)) == message
        assert await unwrap(await alib.anext(topic_iter)) == message

        await proxy.disconnect()
        with pytest.raises(AMQPConnectionError):
            assert await topic.publish(message, wait=False)
        assert await topic.publish(message, wait=True)

        assert await unwrap(await alib.anext(topic_iter)) == message

    # Rabbitmq service must be closed before the tcp proxy othwerwise a
    # connection leak.
    await services_manager.close()


@pytest.mark.asyncio
async def test_closed_rabbitmq_topic(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(RabbitMQTopic)
    await topic.close()
    with pytest.raises(TopicClosedError):
        await topic.publish(TopicMessage(id=MessageId("0"), args={"n": 0}), wait=True)


@pytest.mark.asyncio
async def test_rabbitmq_topic_serialization_error(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(RabbitMQTopic)
    with pytest.raises(TypeError):
        await topic.publish(
            TopicMessage(id=MessageId("0"), args={"n": datetime.now()}), wait=True
        )


@pytest.mark.asyncio
async def test_rabbitmq_topic_expiring_message(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(RabbitMQTopic)

    message = TopicMessage(id=MessageId("0"), args={"n": "1"}, expire_after=None)
    messages = [
        TopicMessage(
            id=MessageId("1"), args={"n": "2"}, expire_after=timedelta(seconds=0)
        ),
        message,
    ]

    for m in messages:
        await topic.publish(m, wait=True)

    async with alib.scoped_iter(topic.run()) as topic_iter:
        assert await unwrap(await alib.anext(topic_iter)) == message

    await topic.close()


@pytest.mark.asyncio
async def test_retry(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(
        RabbitMQTopic, serializer=RabbitMQSerializer.PICKLE, max_retry=1
    )

    messages = [
        TopicMessage(id=MessageId("0"), args={"n": b"1", "time": utcnow()}),
    ]

    for message in messages:
        await topic.publish(message, wait=True)

    async with alib.scoped_iter(topic.run()) as topic_iter:
        # we try 2 time to execute the first message.
        # Max_retry is at 1, so we will give up after two attemps.
        context = await alib.anext(topic_iter)
        with pytest.raises(ValueError):
            async with context as message:
                raise ValueError("Exception")

        context = await alib.anext(topic_iter)
        with pytest.raises(ValueError):
            async with context as message:
                raise ValueError("Exception")

        await topic.publish(
            TopicMessage(id=MessageId("1"), args={"n": b"1", "time": utcnow()}),
            wait=True,
        )

        context = await alib.anext(topic_iter)
        async with context as message:
            assert message.id == "1"

    await topic.close()


@pytest.mark.asyncio
async def test_dead_letter_exchanges(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(
        RabbitMQTopic,
        serializer=RabbitMQSerializer.PICKLE,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": "dlx_queue",
        },
    )
    dlx_topic = await rabbitmq_topic_maker(
        RabbitMQTopic,
        serializer=RabbitMQSerializer.PICKLE,
        queue_name="dlx_queue",
    )

    await dlx_topic.ensure_queue()

    messages = [
        TopicMessage(id=MessageId("0"), args={"n": b"1", "time": utcnow()}),
    ]

    for message in messages:
        await topic.publish(message, wait=True)

    # We make the message fail
    async with alib.scoped_iter(topic.run()) as topic_iter:
        context = await alib.anext(topic_iter)
        with pytest.raises(ValueError):
            async with context as message:
                raise ValueError("Exception")

    # We iter the dlx_topic, ensure the failed message
    async with alib.scoped_iter(dlx_topic.run()) as dlx_topic_iter:
        context = await alib.anext(dlx_topic_iter)
        async with context as message:
            assert message.id == "0"

    await topic.close()
    await dlx_topic.close()


@pytest.mark.asyncio
async def test_create_topic_with_exchange(
    rabbitmq_topic_maker: t.Callable[..., Awaitable[RabbitMQTopic]]
) -> None:
    topic = await rabbitmq_topic_maker(
        RabbitMQTopic,
        serializer=RabbitMQSerializer.PICKLE,
        exchange=Exchange(name="test"),
    )

    await topic.ensure_queue()
