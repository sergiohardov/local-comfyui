# Стиль — в промпте, а негатив только подчищает (урок 1): узел собирает оба текста по выбранному стилю
STYLES = {
    "none": ("", ""),
    "flat vector": ("flat vector illustration, bold outlines, simple shapes, flat colors", "photo, realistic, 3d render, gradient"),
    "pixel art": ("pixel art, 16-bit game sprite, limited palette, crisp pixels", "photo, realistic, 3d render, blurry, smooth shading"),
    "cartoon": ("cartoon, cel shading, bold outlines, vibrant colors", "photo, realistic, 3d render"),
    "watercolor": ("watercolor painting, soft edges, paper texture", "photo, realistic, 3d render, sharp lines"),
    "photo": ("photo, realistic, detailed, natural light", "cartoon, illustration, drawing, painting"),
    "isometric": ("isometric view, game asset, clean lines", "photo, realistic, perspective"),
}
COMMON_NEGATIVE = "blurry, text, watermark"


class StylePrompt:
    """Промпт и негатив из описания и стиля из списка."""

    # Список строк вместо типа — выпадающий список (COMBO)
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "subject": ("STRING", {"multiline": True, "default": "a black bmw car"}),
                "style": (list(STYLES), {"default": "flat vector"}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("positive", "negative")  # подписи выходов
    FUNCTION = "run"
    CATEGORY = "learn"

    def run(self, subject, style):
        positive, negative = STYLES[style]
        join = lambda *parts: ", ".join(p for p in parts if p)
        return (join(subject.strip(), positive), join(negative, COMMON_NEGATIVE))

