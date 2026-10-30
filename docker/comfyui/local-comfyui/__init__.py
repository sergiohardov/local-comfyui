# The tool inside ComfyUI: our nodes for project graphs (nodes.py), typical workflows in Workflow → Browse Templates
# (example_workflows/) and the HTTP API for projects on other machines (api.py).
from . import api  # noqa: F401  registers the /tool/... routes
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: F401
