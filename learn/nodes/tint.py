import torch

# Цвета в долях 0..1, как и сама картинка: (красный, зелёный, синий)
COLORS = {
    "sepia": (0.44, 0.26, 0.08),
    "red": (1.0, 0.0, 0.0),
    "orange": (1.0, 0.5, 0.0),
    "yellow": (1.0, 0.9, 0.0),
    "green": (0.0, 0.8, 0.2),
    "blue": (0.0, 0.3, 1.0),
    "purple": (0.6, 0.1, 0.9),
}


class Tint:
    """Окрасить картинку в цвет: смешать её с заливкой этого цвета."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                # Список строк вместо типа — выпадающий список
                "color": (list(COLORS), {"default": "sepia"}),
                # FLOAT — дробное число; step — шаг стрелок в виджете
                "strength": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.05, "tooltip": "0 — без изменений, 1 — сплошная заливка"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "learn"

    def run(self, image, color, strength):
        # image — тензор [картинки, высота, ширина, 3]; последняя ось — цвет пикселя.
        # Тензор из трёх чисел torch сам «растянет» на каждый пиксель каждой картинки
        fill = torch.tensor(COLORS[color], dtype=image.dtype, device=image.device)
        tinted = image * (1 - strength) + fill * strength
        return (tinted,)
