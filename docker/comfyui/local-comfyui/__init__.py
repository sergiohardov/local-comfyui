# The tool inside ComfyUI: our nodes for project graphs (nodes.py), typical workflows in Browse Templates
# (example_workflows/) and the tool API for other machines: status, the LoRA library and training (api.py).
from . import api  # noqa: F401  registers the /tool/... routes
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: F401
