# The tool inside ComfyUI: common workflows in Browse Templates (example_workflows/; in the API format in
# api_workflows/ — a folder named workflows/ ComfyUI would list as templates too), node packages of projects sent
# over the API (project_nodes.py) and the tool API for other machines: status, the LoRA library, training, logs
# (api.py). It has no nodes of its own: what a project needs, it sends.
from . import api  # noqa: F401  registers the /tool/... routes
from . import project_nodes

NODE_CLASS_MAPPINGS = {}

project_nodes.register_all()
