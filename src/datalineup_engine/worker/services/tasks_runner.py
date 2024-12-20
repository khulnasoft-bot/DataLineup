import typing as t

import asyncio

from datalineup_engine.utils.asyncutils import TasksGroupRunner

from . import MinimalService


class TasksRunnerService(MinimalService):
    name = "tasks_runner"

    async def open(self) -> None:
        self.runner = TasksGroupRunner(name="tasks-runner-service")
        self.runner.start()

    async def close(self) -> None:
        await self.runner.close()

    def create_task(self, coro: t.Coroutine, *, name: str) -> asyncio.Task:
        return self.runner.create_task(coro, name=name)
