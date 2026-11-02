import numpy as np
import torch
from PIL import Image


class Pixelate:
    """Пиксель-арт из картинки: уменьшить блоками, сократить палитру, увеличить обратно без сглаживания."""

    # Входы узла: тип и настройки виджета. IMAGE — провод, INT — числовое поле
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "pixel_size": ("INT", {"default": 8, "min": 2, "max": 64, "tooltip": "размер одного «пикселя» в настоящих пикселях"}),
                "colors": ("INT", {"default": 16, "min": 0, "max": 256, "tooltip": "сколько цветов оставить; 0 — палитру не трогать"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)  # выходы узла
    FUNCTION = "run"  # какой метод вызвать
    CATEGORY = "learn"  # раздел в поиске и меню узлов

    # Аргументы — по именам входов. IMAGE — тензор torch [картинки, высота, ширина, 3], числа 0..1
    def run(self, image, pixel_size, colors):
        frames = []
        for frame in image:
            pil = Image.fromarray((frame.cpu().numpy() * 255).round().astype(np.uint8))
            small = pil.resize((max(1, pil.width // pixel_size), max(1, pil.height // pixel_size)), Image.BOX)
            if colors:
                small = small.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).convert("RGB")
            big = small.resize((small.width * pixel_size, small.height * pixel_size), Image.NEAREST)
            frames.append(torch.from_numpy(np.asarray(big).astype(np.float32) / 255))
        return (torch.stack(frames),)  # всегда кортеж: по элементу на каждый выход
