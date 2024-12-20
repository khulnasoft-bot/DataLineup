TIMEOUT = 1200
TIMEOUT_DELAY = 60
EXECUTE_FUNC_NAME = "remote_execute"

healthcheck_interval = 10


def worker_healthcheck_key(job_id: str) -> str:
    return f"datalineup:arq:{job_id}:whealthcheck"


def executor_healthcheck_key(job_id: str) -> str:
    return f"datalineup:arq:{job_id}:ehealthcheck"
