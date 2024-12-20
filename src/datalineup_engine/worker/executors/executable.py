import typing as t

import asyncio
import contextlib
import dataclasses
from collections.abc import AsyncGenerator
from collections.abc import Iterator
from functools import cached_property

import asyncstdlib as alib
from asyncstdlib import nullcontext

from datalineup_engine.core import ResourceUsed
from datalineup_engine.core import TopicMessage
from datalineup_engine.core.api import QueueItem
from datalineup_engine.utils import flatten
from datalineup_engine.utils import iterators
from datalineup_engine.utils.config import LazyConfig
from datalineup_engine.utils.log import getLogger
from datalineup_engine.worker.context import job_context
from datalineup_engine.worker.context import message_context
from datalineup_engine.worker.executors.parkers import Parkers
from datalineup_engine.worker.pipeline_message import PipelineMessage
from datalineup_engine.worker.resources.manager import ResourceContext
from datalineup_engine.worker.resources.manager import ResourcesContext
from datalineup_engine.worker.services import Services
from datalineup_engine.worker.services.hooks import ItemsBatch
from datalineup_engine.worker.topic import Topic
from datalineup_engine.worker.topic import TopicOutput

JOB_NAMESPACE: t.Final[str] = "job"


class ExecutableMessage:
    def __init__(
        self,
        *,
        queue: "ExecutableQueue",
        message: PipelineMessage,
        parker: Parkers,
        output: dict[str, list[Topic]],
        message_context: t.Optional[t.AsyncContextManager] = None,
    ):
        self.message = message
        self._context = contextlib.AsyncExitStack()
        self._executing_context = contextlib.AsyncExitStack()
        if message_context:
            self._context.push_async_exit(message_context)
        self._parker = parker
        self.output = output
        self.resources: dict[str, ResourceContext] = {}
        self.queue = queue

    @property
    def id(self) -> str:
        return self.message.id

    def park(self) -> None:
        self._parker.park(id(self))

    async def unpark(self) -> None:
        await self._parker.unpark(id(self))

    async def attach_resources(
        self, resources_context: ResourcesContext
    ) -> dict[str, dict[str, object]]:
        self.resources = await self._executing_context.enter_async_context(
            resources_context
        )
        resources_data = {
            k: ({"name": r.resource.name, "state": r.resource.state} | r.resource.data)
            for k, r in self.resources.items()
            if r.resource
        }
        self.message.update_with_resources(resources_data)
        return resources_data

    def update_resources_used(self, resources_used: list[ResourceUsed]) -> None:
        if not self.resources:
            return

        for resource_used in resources_used:
            if resource_used.release_at is not None:
                self.resources[resource_used.type].release_later(
                    resource_used.release_at
                )
            if resource_used.state is not None:
                self.resources[resource_used.type].update_state(resource_used.state)

    @cached_property
    def config(self) -> LazyConfig:
        return self.queue.config.load_object(self.message.message.config)

    def __str__(self) -> str:
        return str(self.message.message.id)

    @contextlib.contextmanager
    def datalineup_context(self) -> t.Iterator[None]:
        with job_context(self.queue.definition), message_context(self.message.message):
            yield


class ExecutableQueue:
    def __init__(
        self,
        *,
        definition: QueueItem,
        topic: Topic,
        output: dict[str, list[Topic]],
        services: Services,
    ):
        self.logger = getLogger(__name__, self)

        self.definition = definition
        self.name: str = definition.name
        self.pipeline = definition.pipeline
        self.executor = definition.executor

        self.topic = topic
        self.output = output
        self.services = services

        self.parkers = Parkers()
        self.concurrency_sem: t.AsyncContextManager = nullcontext()
        if self.options.max_concurrency:
            self.concurrency_sem = asyncio.Semaphore(self.options.max_concurrency)

        self.iterable = self.run()

        self.is_closed = False
        self.pending_messages_count = 0
        self.done = asyncio.Event()

    @dataclasses.dataclass
    class Options:
        batching_enabled: bool = False
        buffer_flush_after: t.Optional[float] = 5
        buffer_size: t.Optional[int] = 10
        max_concurrency: t.Optional[int] = None

    async def run(self) -> AsyncGenerator[ExecutableMessage, None]:
        await self.open()

        try:
            async for context, message in self._make_iterator():
                with message_context(message):
                    pipeline_message = PipelineMessage(
                        info=self.pipeline.info,
                        message=message.extend(self.pipeline.args),
                    )
                    executable_message = ExecutableMessage(
                        queue=self,
                        parker=self.parkers,
                        message=pipeline_message,
                        message_context=context,
                        output=self.output,
                    )
                    await self.services.s.hooks.message_polled.emit(executable_message)
                    await self.parkers.wait()
                with self.pending_context() as pending_context:
                    executable_message._context.enter_context(pending_context())
                    yield executable_message
        finally:
            with job_context(self.definition):
                await self.close()

    async def close(self) -> None:
        if self.is_closed:
            return
        self.is_closed = True

        # TODO: don't clean topics here once topics are shared between jobs.
        await self.topic.close()

        # Wait until all in-fligth messages are done before closing outputs.
        await self.wait_for_done()
        for topics in self.output.values():
            for topic in topics:
                await topic.close()

    async def open(self) -> None:
        await self.topic.open()
        for output in flatten(self.output.values()):
            await output.open()

    async def wait_for_done(self) -> None:
        if self.pending_messages_count:
            await self.done.wait()

    @contextlib.contextmanager
    def pending_context(self) -> Iterator[t.Callable[[], t.ContextManager]]:
        # Yield a new contextmanager to be attached to the message.
        # This allow tracking when all message from this job have been processed
        # so we can properly clean the jobs resources afterward. We need to yield
        # the context from a context so that if the job's yield has an exception
        # in the case of cancellation we won't to mark the pending message as not
        # being pending anymore.
        self.pending_messages_count += 1
        processed = False

        @contextlib.contextmanager
        def message_context() -> Iterator[None]:
            nonlocal processed

            try:
                yield
            finally:
                if not processed:
                    self.message_processed()
                processed = True

        try:
            yield message_context
        except BaseException:
            if not processed:
                self.message_processed()
            processed = True
            raise

    def message_processed(self) -> None:
        self.pending_messages_count -= 1
        if self.is_closed and self.pending_messages_count == 0:
            self.done.set()

    @property
    def options(self) -> Options:
        return self.config.cast_namespace(JOB_NAMESPACE, self.Options)

    @cached_property
    def config(self) -> LazyConfig:
        return LazyConfig(
            [
                self.services.s.config.r,
                self.definition.config,
            ]
        )

    def __repr__(self) -> str:
        return f"ExecutableQueue(name={self.name})"

    def _make_iterator(self) -> t.AsyncIterator[tuple[TopicOutput, TopicMessage]]:
        iterator = iterators.async_enter(
            alib.map(self.concurrency_context, self.topic.run()),
            error=self.log_message_error,
        )
        # If we enable cursors states, we are going to decorate the iterator
        # to buffer multiple messages together and load their states.
        if self.options.batching_enabled:
            buffered_iterator = iterators.async_buffered(
                iterator,
                flush_after=self.options.buffer_flush_after,
                buffer_size=self.options.buffer_size,
            )
            emit_batches = self._emit_batches(buffered_iterator)
            iterator = iterators.async_flatten(emit_batches)

        return iterators.contextualize(iterator, context=self.job_context)

    @contextlib.asynccontextmanager
    async def concurrency_context(
        self, message_context: TopicOutput
    ) -> t.AsyncIterator[TopicMessage]:
        async with self.concurrency_sem, message_context as message:
            yield message

    @contextlib.asynccontextmanager
    async def job_context(self) -> t.AsyncIterator[None]:
        with job_context(self.definition):
            yield

    async def log_message_error(self, error: Exception) -> None:
        self.logger.error("Failed to process message", exc_info=error)

    async def _emit_batches(
        self, iterator: t.AsyncIterator[list[tuple[TopicOutput, TopicMessage]]]
    ) -> t.AsyncIterator[list[tuple[TopicOutput, TopicMessage]]]:
        async for items in iterator:
            await self.services.s.hooks.items_batched.emit(
                ItemsBatch(items=[i for _, i in items], job=self.definition)
            )
            yield items
