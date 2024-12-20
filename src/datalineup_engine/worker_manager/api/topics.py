from flask import Blueprint

from datalineup_engine.core.api import TopicsResponse
from datalineup_engine.utils.flask import Json
from datalineup_engine.utils.flask import jsonify
from datalineup_engine.worker_manager.app import current_app

bp = Blueprint("topics", __name__, url_prefix="/api/topics")


@bp.route("", methods=("GET",))
def get_job_definitions() -> Json[TopicsResponse]:
    topics = list(current_app.datalineup.static_definitions.topics.values())
    return jsonify(TopicsResponse(items=topics))
