# The tool inside ComfyUI: typical workflows in Workflow → Browse Templates (example_workflows/)
# and the HTTP API for projects on other machines (api.py). No nodes of its own.
from . import api  # noqa: F401  registers the /tool/... routes

NODE_CLASS_MAPPINGS = {}
