# Sprite from a generated picture and its foreground mask (BiRefNet, from the graph): the figure on a square
# transparent canvas, with one scale for every sprite so the proportions set by the pose reach the game unchanged,
# the lowest point of the silhouette on the bottom edge (feet down: the sprite's pivot is at the feet) and the body
# axis of the pose in the middle, so a sword held out does not push the body aside. Optionally hard edges and the
# project's palette.
import re

from PIL import Image

DEFAULTS = {"size": 512, "padding": 0, "edges": "soft", "palette": None, "model": "birefnet.safetensors"}
EDGES = ("soft", "hard")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Mask values below ~10% are haze around the figure, not the figure
SILHOUETTE = 26


def check(post, models):
    if not isinstance(post, dict) or set(post) - set(DEFAULTS):
        return None, [f"post: объект с полями {', '.join(DEFAULTS)}, все необязательные"]
    post = {**DEFAULTS, **post}
    errors = []
    size = post["size"]
    if not isinstance(size, int) or isinstance(size, bool) or not 64 <= size <= 4096:
        errors.append("post.size: сторона квадратного холста, целое от 64 до 4096")
    elif not isinstance(post["padding"], int) or isinstance(post["padding"], bool) \
            or not 0 <= post["padding"] < size // 2:
        errors.append(f"post.padding: отступ снизу в пикселях холста, целое от 0 до {size // 2 - 1}")
    if post["edges"] not in EDGES:
        errors.append(f"post.edges: {' или '.join(EDGES)}")
    palette = post["palette"]
    if palette is not None and not (isinstance(palette, list) and 1 <= len(palette) <= 256
                                    and all(isinstance(c, str) and HEX.match(c) for c in palette)):
        errors.append("post.palette: список от 1 до 256 цветов вида \"#a1b2c3\"")
    if post["model"] not in models:
        errors.append(f"post.model: нет модели удаления фона {post['model']!r}, есть: {', '.join(models) or 'ничего'}")
    return post, errors


def body_axis(points):
    if points and "right_hip" in points and "left_hip" in points:
        return (points["right_hip"][0] + points["left_hip"][0]) / 2
    if points and "neck" in points:
        return points["neck"][0]
    return None


def make(image, mask, post, points=None):
    image, mask = image.convert("RGB"), mask.convert("L").resize(image.size)
    size, padding = post["size"], post["padding"]
    bbox = mask.point(lambda v: 255 if v >= SILHOUETTE else 0).getbbox()
    if not bbox:
        return Image.new("RGBA", (size, size), (0, 0, 0, 0)), ["на картинке не нашлось фигуры: маска пустая"]

    # One scale for all sprites: the canvas over the height of the generation, not over each silhouette
    scale = size / image.height
    rgba = image.convert("RGBA")
    rgba.putalpha(mask)
    # Premultiplied while resizing, so the soft edge does not pick up the background's colour
    scaled = rgba.convert("RGBa").resize((round(image.width * scale), round(image.height * scale)),
                                         Image.Resampling.LANCZOS).convert("RGBA")
    alpha = scaled.getchannel("A")
    if post["edges"] == "hard":
        alpha = alpha.point(lambda v: 255 if v >= 128 else 0)
    rgb = scaled.convert("RGB")
    if post["palette"]:
        # Last, after resizing, so no in-between colours appear; no dithering, flat fills stay flat
        colors = [int(c[i:i + 2], 16) for c in post["palette"] for i in (1, 3, 5)]
        reference = Image.new("P", (1, 1))
        reference.putpalette(colors + colors[:3] * (256 - len(post["palette"])))
        rgb = rgb.quantize(palette=reference, dither=Image.Dither.NONE).convert("RGB")
    sprite = rgb.convert("RGBA")
    sprite.putalpha(alpha)

    axis = body_axis(points)
    center = axis if axis is not None else (bbox[0] + bbox[2]) / 2
    left, top = round(size / 2 - center * scale), round(size - padding - bbox[3] * scale)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(sprite, (left, top))

    warnings = []
    placed = (left + bbox[0] * scale, top + bbox[1] * scale, left + bbox[2] * scale)
    for side, overflow in (("слева", -placed[0]), ("сверху", -placed[1]), ("справа", placed[2] - size)):
        if overflow >= 1:
            warnings.append(f"фигура не поместилась в холст {size}×{size}: обрезано {side} {round(overflow)} px")
    return canvas, warnings
