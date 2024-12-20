import typing as t

import dataclasses
import time
from collections.abc import AsyncGenerator
from weakref import WeakSet

from opentelemetry.metrics import CallbackOptions
from opentelemetry.metrics import Observation
from opentelemetry.metrics import get_meter

from datalineup_engine.core import PipelineResults
from datalineup_engine.worker.executors.executable import ExecutableMessage
from datalineup_engine.worker.services.hooks import MessagePublished
from datalineup_engine.worker.services.hooks import ResultsProcessed

from . import MinimalService


class PipelineName(t.NamedTuple):
    labels: t.FrozenSet[tuple[str, str]]
    executor: str
    name: str


Nanoseconds: t.TypeAlias = int
PipelineMessages: t.TypeAlias = dict[PipelineName, WeakSet[ExecutableMessage]]

PollingMessage = t.NewType("PollingMessage", ExecutableMessage)
SchedulingMessage = t.NewType("SchedulingMessage", ExecutableMessage)
SubmittingMessage = t.NewType("SubmittingMessage", ExecutableMessage)
ExecutingMessage = t.NewType("ExecutingMessage", ExecutableMessage)
ProcessingMessage = t.NewType("ProcessingMessage", ExecutableMessage)
PublishingMessage = t.NewType("PublishingMessage", ExecutableMessage)

T = t.TypeVar("T", bound=ExecutableMessage)
U = t.TypeVar("U", bound=ExecutableMessage)


class PipelineUsage(t.NamedTuple):
    executor: str
    labels: dict[str, str]
    name: str
    usage: float


@dataclasses.dataclass
class PipelineState:
    messages: WeakSet[ExecutableMessage] = dataclasses.field(default_factory=WeakSet)
    # The default_factory needs to use lambda otherwise it won't get overriden
    # in tests by freezegun.
    last_flushed_at: Nanoseconds = dataclasses.field(
        default_factory=lambda: time.perf_counter_ns()
    )
    last_collected_at: Nanoseconds = dataclasses.field(
        default_factory=lambda: time.perf_counter_ns()
    )
    usage: Nanoseconds = 0

    def flush(self, *, now: Nanoseconds) -> None:
        now = time.perf_counter_ns()
        since_flush = now - self.last_flushed_at
        self.usage += since_flush * len(self.messages)
        self.last_flushed_at = now

    def add(self, xmsg: ExecutableMessage) -> None:
        self.flush(now=time.perf_counter_ns())
        self.messages.add(xmsg)

    def pop(self, xmsg: ExecutableMessage) -> None:
        if xmsg in self.messages:
            self.flush(now=time.perf_counter_ns())
            self.messages.discard(xmsg)

    def collect(self, *, now: Nanoseconds) -> float:
        self.flush(now=now)
        usage = self.usage / (now - self.last_collected_at)
        self.last_collected_at = now
        self.usage = 0
        return usage


@dataclasses.dataclass
class StageState(t.Generic[T, U]):
    pipelines: dict[PipelineName, PipelineState] = dataclasses.field(
        default_factory=dict
    )

    @staticmethod
    def _name(xmsg: ExecutableMessage) -> PipelineName:
        return PipelineName(
            executor=xmsg.queue.definition.executor or "default",
            labels=frozenset(xmsg.queue.definition.labels.items()),
            name=xmsg.message.info.name,
        )

    def push(self, xmsg: T) -> None:
        pipeline_state = self.pipelines.setdefault(self._name(xmsg), PipelineState())
        pipeline_state.add(xmsg)

    def pop(self, xmsg: ExecutableMessage) -> U:
        if pipeline_state := self.pipelines.get(self._name(xmsg)):
            pipeline_state.pop(xmsg)

        return t.cast(U, xmsg)

    def collect(self, *, now: Nanoseconds) -> t.Iterator[PipelineUsage]:
        for k, state in self.pipelines.items():
            yield PipelineUsage(
                labels=dict(k.labels),
                executor=k.executor,
                name=k.name,
                usage=state.collect(now=now),
            )


@dataclasses.dataclass
class StagesState:
    polling: StageState[ExecutableMessage, PollingMessage] = dataclasses.field(
        default_factory=StageState
    )
    scheduling: StageState[PollingMessage, SchedulingMessage] = dataclasses.field(
        default_factory=StageState
    )
    submitting: StageState[SchedulingMessage, SubmittingMessage] = dataclasses.field(
        default_factory=StageState
    )
    executing: StageState[SubmittingMessage, ExecutingMessage] = dataclasses.field(
        default_factory=StageState
    )
    processing_results: StageState[ExecutableMessage, ProcessingMessage] = (
        dataclasses.field(default_factory=StageState)
    )
    publishing: StageState[ProcessingMessage, PublishingMessage] = dataclasses.field(
        default_factory=StageState
    )
    waiting_publish: StageState[PublishingMessage, ExecutableMessage] = (
        dataclasses.field(default_factory=StageState)
    )


class UsageMetrics(MinimalService):
    name = "usage_metrics"

    async def open(self) -> None:
        self.meter = get_meter("datalineup.metrics")
        self.message_counter = self.meter.create_observable_counter(
            name="datalineup.pipeline.usage",
            unit="s",
            description="""
            Track the sum of message's time spent at different stage.
            """,
            callbacks=[self.collect_usage_metrics],
        )

        self.services.hooks.message_polled.register(self.on_message_polled)
        self.services.hooks.message_scheduled.register(self.on_message_scheduled)
        self.services.hooks.message_submitted.register(self.on_message_submitted)
        self.services.hooks.message_executed.register(self.on_message_executed)
        self.services.hooks.results_processed.register(self.on_results_processed)
        self.services.hooks.message_published.register(self.on_message_published)
        self.services.hooks.output_blocked.register(self.on_output_blocked)

        self.stages_state = StagesState()

    def collect_usage_metrics(
        self, options: CallbackOptions
    ) -> t.Iterable[Observation]:
        now = time.perf_counter_ns()
        for stage_name in (
            "polling",
            "scheduling",
            "submitting",
            "executing",
            "processing_results",
            "publishing",
            "waiting_publish",
        ):
            stage: StageState = getattr(self.stages_state, stage_name)
            for pipeline in stage.collect(now=now):
                yield Observation(
                    pipeline.usage,
                    {
                        "executor": pipeline.executor,
                        "pipeline": pipeline.name,
                        "state": stage_name,
                    }
                    | {f"datalineup.job.labels.{k}": v for k, v in pipeline.labels.items()},
                )

    async def on_message_polled(self, xmsg: ExecutableMessage) -> None:
        self.stages_state.polling.push(xmsg)

    async def on_message_scheduled(self, xmsg: ExecutableMessage) -> None:
        self.stages_state.scheduling.push(self.stages_state.polling.pop(xmsg))

    async def on_message_submitted(self, xmsg: ExecutableMessage) -> None:
        self.stages_state.submitting.push(self.stages_state.scheduling.pop(xmsg))

    async def on_message_executed(
        self, xmsg: ExecutableMessage
    ) -> AsyncGenerator[None, PipelineResults]:
        self.stages_state.executing.push(self.stages_state.submitting.pop(xmsg))
        try:
            yield
        finally:
            self.stages_state.executing.pop(xmsg)

    async def on_results_processed(
        self,
        results: ResultsProcessed,
    ) -> AsyncGenerator[None, None]:
        self.stages_state.processing_results.push(results.xmsg)
        try:
            yield
        finally:
            self.stages_state.processing_results.pop(results.xmsg)

    async def on_message_published(
        self, output: MessagePublished
    ) -> AsyncGenerator[None, None]:
        self.stages_state.publishing.push(
            self.stages_state.processing_results.pop(output.xmsg)
        )
        try:
            yield
        finally:
            self.stages_state.publishing.pop(output.xmsg)
            self.stages_state.processing_results.push(output.xmsg)

    async def on_output_blocked(
        self, output: MessagePublished
    ) -> AsyncGenerator[None, None]:
        self.stages_state.waiting_publish.push(
            self.stages_state.publishing.pop(output.xmsg)
        )
        try:
            yield
        finally:
            self.stages_state.waiting_publish.pop(output.xmsg)
