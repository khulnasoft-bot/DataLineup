from datalineup_engine.core.topic import TopicMessage
from datalineup_engine.worker.topic import Topic


class NullTopic(Topic):
    """A topic that does nothing."""

    async def publish(self, message: TopicMessage, wait: bool) -> bool:
        return True
