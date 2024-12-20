import logging
from datetime import datetime
from datetime import timedelta

from datalineup_engine.core.api import ComponentDefinition
from datalineup_engine.core.api import LockInput
from datalineup_engine.core.api import LockResponse
from datalineup_engine.core.api import ResourceItem
from datalineup_engine.core.api import ResourcesProviderItem
from datalineup_engine.models.queue import Queue
from datalineup_engine.stores import jobs_store
from datalineup_engine.stores import queues_store
from datalineup_engine.utils.sqlalchemy import AnySyncSession
from datalineup_engine.worker_manager.config.static_definitions import StaticDefinitions


def lock_jobs(
    lock_input: LockInput,
    *,
    max_assigned_items: int,
    static_definitions: StaticDefinitions,
    session: AnySyncSession,
) -> LockResponse:
    logger = logging.getLogger(f"{__name__}.lock_jobs")
    # Note:
    # - Leftover items remain unassigned.
    assignation_expiration_cutoff: datetime = datetime.now() - timedelta(minutes=15)

    assigned_items: list[Queue] = []

    # Obtains items that were already assigned.
    assigned_items.extend(
        queues_store.get_assigned_queues(
            session=session,
            worker_id=lock_input.worker_id,
            selector=lock_input.selector,
            assigned_after=assignation_expiration_cutoff,
        )
    )

    # Unassign extra items.
    for unassigned_item in assigned_items[max_assigned_items:]:
        unassigned_item.assigned_at = None
        unassigned_item.assigned_to = None

    assigned_items = assigned_items[:max_assigned_items]

    # Obtain new queues
    if len(assigned_items) < max_assigned_items:
        assigned_items.extend(
            queues_store.get_unassigned_queues(
                session=session,
                assigned_before=assignation_expiration_cutoff,
                # We don't set a limit when specifying executors
                limit=(
                    max_assigned_items - len(assigned_items)
                    if not lock_input.executors
                    else None
                ),
                selector=lock_input.selector,
            )
        )
    # Join definitions and filtered out by executors
    for item in assigned_items.copy():
        try:
            item.join_definitions(static_definitions)
            if (
                lock_input.executors
                and item.queue_item.executor not in lock_input.executors
            ):
                assigned_items.remove(item)
        except Exception as e:
            if item.job:
                jobs_store.set_failed(item.job.name, session=session, error=repr(e))
            assigned_items.remove(item)

    static_definitions = static_definitions
    resources: dict[str, ResourceItem] = {}
    resources_providers: dict[str, ResourcesProviderItem] = {}
    executors: dict[str, ComponentDefinition] = {}
    # Copy list since the iteration could drop items from assigned_items.
    for item in assigned_items.copy():
        # Collect resource for assigned work
        item_resources: dict[str, ResourceItem] = {}
        item_resources_providers: dict[str, ResourcesProviderItem] = {}
        missing_resource = False
        for resource_type in item.queue_item.pipeline.info.resources.values():
            pipeline_resources = static_definitions.resources_by_type.get(resource_type)

            if not pipeline_resources:
                logger.error(
                    "Skipping queue item, resource missing: item=%s, " "resource=%s",
                    item.name,
                    resource_type,
                )
                # Do not update assign the object in the database.
                assigned_items.remove(item)
                missing_resource = True
                break

            # assert to make mypy happy.
            assert item_resources is not None  # noqa: S101
            for resource in pipeline_resources:
                if isinstance(resource, ResourceItem):
                    item_resources[resource.name] = resource
                elif isinstance(resource, ResourcesProviderItem):
                    item_resources_providers[resource.name] = resource

        # A resource was missing, we can't schedule this item.
        if missing_resource:
            continue

        resources.update(item_resources)
        resources_providers.update(item_resources_providers)

        # Collect executor for assigned work
        executor_name = item.queue_item.executor
        executor = static_definitions.executors.get(executor_name)
        if not executor:
            logger.error(
                "Skipping queue item, executor missing: item=%s, " "executor=%s",
                item.name,
                executor_name,
            )
            # Do not update assign the object in the database.
            assigned_items.remove(item)
            continue

        executors.setdefault(executor.name, executor)
    # Refresh assignments
    new_assigned_at = datetime.now()
    for assigned_item in assigned_items:
        assigned_item.assigned_at = new_assigned_at
        assigned_item.assigned_to = lock_input.worker_id

    queue_items = []
    for item in assigned_items:
        queue_items.append(item.queue_item)

    return LockResponse(
        items=queue_items,
        resources=list(sorted(resources.values(), key=lambda r: r.name)),
        resources_providers=list(
            sorted(resources_providers.values(), key=lambda r: r.name)
        ),
        executors=list(sorted(executors.values(), key=lambda e: e.name)),
    )
