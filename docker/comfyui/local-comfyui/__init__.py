# The tool inside ComfyUI: our nodes for project graphs (nodes.py), typical workflows in Browse Templates
# (example_workflows/, in the API format in workflows/), node packages of projects sent over the API
# (project_nodes.py) and the tool API for other machines: status, the LoRA library, training, logs (api.py).
from . import api  # noqa: F401  registers the /tool/... routes
from . import project_nodes
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: F401

project_nodes.register_all()
