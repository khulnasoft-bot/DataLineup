import contextlib
import logging
from collections.abc import Generator
from collections.abc import Iterable
from collections.abc import Iterator

from pydantic.v1 import ValidationError

from datalineup_engine.core import PipelineOutput
from datalineup_engine.core import PipelineResults
from datalineup_engine.core import ResourceUsed
from datalineup_engine.core import TopicMessage
from datalineup_engine.core.pipeline import PipelineEvent
from datalineup_engine.core.pipeline import PipelineResultTypes
from datalineup_engine.utils.hooks import ContextHook
from datalineup_engine.utils.hooks import EventHook
from datalineup_engine.utils.traceback_data import TracebackData
from datalineup_engine.worker.context import message_context
from datalineup_engine.worker.context import pipeline_context
from datalineup_engine.worker.pipeline_message import PipelineMessage

PipelineHook = ContextHook[PipelineMessage, PipelineResults]


class PipelineBootstrap:
    def __init__(self, initialized_hook: EventHook["PipelineBootstrap"]):
        self.pipeline_hook: PipelineHook = ContextHook(
            error_handler=self.pipeline_hook_failed
        )
        initialized_hook.emit(self)

        self.logger = logging.getLogger("datalineup.bootstrap")

    def bootstrap_pipeline(self, message: PipelineMessage) -> PipelineResults:
        message.set_meta_arg(meta_type=TopicMessage, value=message.message)
        with pipeline_context(message.info), message_context(message.message):
            return self.pipeline_hook.emit(self.run_pipeline)(message)

    def run_pipeline(self, message: PipelineMessage) -> PipelineResults:
        try:
            execute_result = message.execute()
        except ValidationError:
            self.logger.error(
                "Failed to deserialize message",
                extra={"data": {"message_args": message.message.args}},
            )
            raise

        # Ensure result is an iterator.
        results: Iterator
        if execute_result is None:
            results = iter([])
        elif isinstance(execute_result, Iterable):
            results = iter(execute_result)
        elif not isinstance(execute_result, Iterator):
            if isinstance(execute_result, PipelineResultTypes):
                results = iter([execute_result])
            else:
                self.logger.error("Invalid result type: %s", execute_result.__class__)
                results = iter([])
        else:
            results = execute_result

        # Convert result into a list of PipelineOutput.
        outputs: list[PipelineOutput] = []
        resources: list[ResourceUsed] = []
        events: list[PipelineEvent] = []
        for result in results:
            if isinstance(result, PipelineOutput):
                outputs.append(result)
            elif isinstance(result, TopicMessage):
                outputs.append(PipelineOutput(channel="default", message=result))
            elif isinstance(result, ResourceUsed):
                resources.append(result)
            elif isinstance(result, PipelineEvent):
                events.append(result)
            else:
                self.logger.error("Invalid result type: %s", result.__class__)

        return PipelineResults(outputs=outputs, resources=resources, events=events)

    def pipeline_hook_failed(self, exception: Exception) -> None:
        self.logger.error("Error while handling pipeline hook", exc_info=exception)


class RemoteException(Exception):
    def __init__(self, tb: TracebackData):
        super().__init__(tb)
        self.remote_traceback = tb

    @classmethod
    def from_exception(cls, exception: Exception) -> "RemoteException":
        tb = TracebackData.from_exception(exception)
        return cls(tb)

    def __str__(self) -> str:
        return (
            self.remote_traceback.format_exception_only()
            + "\nRemoteException "
            + "".join(self.remote_traceback.format())
        )

    def __repr__(self) -> str:
        stype = f"RemoteException[{self.remote_traceback.exc_type}]"
        if self.remote_traceback.exc_str.startswith("("):
            return stype + self.remote_traceback.exc_str
        return stype + f"({self.remote_traceback.exc_str})"


@contextlib.contextmanager
def wrap_remote_exception() -> Generator[None, None, None]:
    try:
        yield
    except Exception as e:
        raise RemoteException.from_exception(e) from None
