# Пакет узлов проекта: ComfyUI ищет здесь NODE_CLASS_MAPPINGS — «имя узла → класс».
# На сервере узлы получат имена <проект>.<имя>: learn.Pixelate, learn.StylePrompt.
from .pixelate import Pixelate
from .prompt import StylePrompt
from .tint import Tint

NODE_CLASS_MAPPINGS = {"Pixelate": Pixelate, "StylePrompt": StylePrompt, "Tint": Tint}
# Как узлы называются в интерфейсе; к названию сервер допишет « (learn)»
NODE_DISPLAY_NAME_MAPPINGS = {"Pixelate": "Pixelate", "StylePrompt": "Style Prompt", "Tint": "Tint"}
