# Post-processing of a picture with its foreground mask (BiRefNet, from the graph), by type:
# - sprite: the figure on a square transparent canvas, with one scale for every sprite so the proportions set by the
#   pose reach the game unchanged, the lowest point of the silhouette on the bottom edge (feet down: the sprite's
#   pivot is at the feet) and the body axis of the pose in the middle, so a sword held out does not push the body aside;
# - frame: an icon frame generated from a layout the tool draws itself, so the window is where the tool put it; the
#   outside goes transparent by the mask and the window by its rectangle, since a model cannot be trusted to leave
#   the middle empty and BiRefNet takes a filled middle for part of the frame;
# - icon: an item fitted into the window of a frame, on a window colour, under the frame, the frame tinted if asked;
# - parts: the character cut into parts on the bones of its pose (parts.py); it takes a second generation, so the
#   API runs it in stages.
# All types can end with hard edges and the project's palette.
import base64
import binascii
import io
import re

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from . import parts as cutting

TYPES = ("sprite", "frame", "icon", "parts")
COMMON = {"edges": "soft", "palette": None, "model": "birefnet.safetensors"}
DEFAULTS = {
    "sprite": {"size": 512, "padding": 0},
    # window in pixels of the generation; by default a centred square of half its side
    "frame": {"size": 128, "window": None, "denoise": 0.9},
    # window in pixels of the frame; without a frame the whole canvas
    "icon": {"size": 128, "frame": None, "window": None, "margin": 6, "background": None, "tint": None},
    "parts": {"size": 512},
}
EDGES = ("soft", "hard")
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Mask values below ~10% are haze around the figure, not the figure
SILHOUETTE = 26


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def decode_png(text):
    # A picture inside JSON: base64, with or without the "data:image/png;base64," prefix
    if not isinstance(text, str):
        return None
    try:
        picture = Image.open(io.BytesIO(base64.b64decode(text.split(",", 1)[-1], validate=True)))
        picture.load()
        return picture
    except (binascii.Error, UnidentifiedImageError, OSError, ValueError):
        return None


def check_window(window, width, height, where):
    if not (isinstance(window, list) and len(window) == 4 and all(is_int(v) for v in window)
            and window[2] > 0 and window[3] > 0 and window[0] >= 0 and window[1] >= 0
            and window[0] + window[2] <= width and window[1] + window[3] <= height):
        return [f"{where}.window: [x, y, ширина, высота] в пикселях внутри {width}×{height}"]
    return []


def default_window(width, height):
    side = min(width, height) // 2
    return [(width - side) // 2, (height - side) // 2, side, side]


def check(post, models, width, height, points=None):
    # width and height are those of the picture the post-processing gets: the generation or the uploaded picture;
    # points are those of its pose, if any
    if not isinstance(post, dict):
        return None, ["post: объект с полем type и настройками этого типа"]
    kind = post.get("type", "sprite")
    if kind not in TYPES:
        return None, [f"post.type: {' / '.join(TYPES)}"]
    allowed = {"type"} | set(COMMON) | set(DEFAULTS[kind])
    if set(post) - allowed:
        return None, [f"post ({kind}): лишние поля {', '.join(sorted(set(post) - allowed))}; "
                      f"есть {', '.join(sorted(allowed - {'type'}))}"]
    post = {"type": kind, **COMMON, **DEFAULTS[kind], **post}
    errors = []

    if not is_int(post["size"]) or not 16 <= post["size"] <= 4096:
        errors.append("post.size: сторона квадратного холста, целое от 16 до 4096")
    if post["edges"] not in EDGES:
        errors.append(f"post.edges: {' или '.join(EDGES)}")
    palette = post["palette"]
    if palette is not None and not (isinstance(palette, list) and 1 <= len(palette) <= 256
                                    and all(isinstance(c, str) and HEX.match(c) for c in palette)):
        errors.append("post.palette: список от 1 до 256 цветов вида \"#a1b2c3\"")
    if post["model"] not in models:
        errors.append(f"post.model: нет модели удаления фона {post['model']!r}, есть: {', '.join(models) or 'ничего'}")

    if kind == "sprite":
        if is_int(post["size"]) and not (is_int(post["padding"]) and 0 <= post["padding"] < post["size"] // 2):
            errors.append(f"post.padding: отступ снизу в пикселях холста, целое от 0 до {post['size'] // 2 - 1}")
    elif kind == "parts":
        if not isinstance(points, dict):
            errors.append("post (parts): нужна поза — части режутся по её костям")
        elif lacking := cutting.missing(points):
            errors.append(f"pose.points: для нарезки на части нужны ещё {', '.join(lacking)}")
    elif kind == "frame":
        post["window"] = post["window"] or default_window(width, height)
        errors += check_window(post["window"], width, height, "post")
        if not isinstance(post["denoise"], (int, float)) or isinstance(post["denoise"], bool) \
                or not 0.3 <= post["denoise"] <= 1:
            errors.append("post.denoise: от 0.3 до 1 — насколько модель уходит от нарисованной раскладки")
    elif kind == "icon":
        if post["frame"] is not None:
            frame = decode_png(post["frame"])
            if frame is None:
                errors.append("post.frame: PNG рамки в base64")
            elif post["window"] is None:
                errors.append("post.window: окно рамки, его возвращает генерация рамки")
            else:
                errors += check_window(post["window"], frame.width, frame.height, "post")
                post["frame"] = frame.convert("RGBA")
        elif post["window"] is not None and is_int(post["size"]):
            errors += check_window(post["window"], post["size"], post["size"], "post")
        if not is_int(post["margin"]) or post["margin"] < 0:
            errors.append("post.margin: отступ предмета от края окна, целое от 0")
        for key in ("background", "tint"):
            if post[key] is not None and not (isinstance(post[key], str) and HEX.match(post[key])):
                errors.append(f"post.{key}: цвет вида \"#a1b2c3\"")
    return post, errors


def public(post):
    # What a reply shows of the settings: a frame picture is reported by its size, not sent back
    shown = dict(post)
    if isinstance(shown.get("frame"), Image.Image):
        shown["frame"] = f"PNG {shown['frame'].width}×{shown['frame'].height}"
    return shown


def frame_layout(width, height, window):
    # The layout a frame is generated from: a plain ring around the window with a bevel line, on a dark field.
    # Neutral grey, so the prompt and the LoRA decide the colours
    x, y, w, h = window
    ring = max(24, min(w, h) // 3)
    layout = Image.new("RGB", (width, height), (40, 40, 44))
    pen = ImageDraw.Draw(layout)
    pen.rectangle((x - ring, y - ring, x + w + ring - 1, y + h + ring - 1), fill=(128, 128, 128))
    inset = ring // 2
    pen.rectangle((x - inset, y - inset, x + w + inset - 1, y + h + inset - 1), outline=(88, 88, 88),
                  width=max(2, ring // 20))
    pen.rectangle((x, y, x + w - 1, y + h - 1), fill=(30, 30, 34))
    return layout


def resized(rgba, size):
    # Premultiplied while resizing, so a soft edge does not pick up the colour behind it
    return rgba.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")


def finish(picture, post):
    # Last, after all resizing, so no in-between colours appear; no dithering, flat fills stay flat
    alpha = picture.getchannel("A")
    if post["edges"] == "hard":
        alpha = alpha.point(lambda v: 255 if v >= 128 else 0)
    rgb = picture.convert("RGB")
    if post["palette"]:
        colors = [int(c[i:i + 2], 16) for c in post["palette"] for i in (1, 3, 5)]
        reference = Image.new("P", (1, 1))
        reference.putpalette(colors + colors[:3] * (256 - len(post["palette"])))
        rgb = rgb.quantize(palette=reference, dither=Image.Dither.NONE).convert("RGB")
    result = rgb.convert("RGBA")
    result.putalpha(alpha)
    return result


def body_axis(points):
    if points and "right_hip" in points and "left_hip" in points:
        return (points["right_hip"][0] + points["left_hip"][0]) / 2
    if points and "neck" in points:
        return points["neck"][0]
    return None


def make(image, mask, post, points=None):
    # Returns the result picture, extra fields for the reply and warnings
    image, mask = image.convert("RGB"), mask.convert("L").resize(image.size)
    return {"sprite": make_sprite, "frame": make_frame, "icon": make_icon}[post["type"]](image, mask, post, points)


def make_sprite(image, mask, post, points):
    size, padding = post["size"], post["padding"]
    bbox = mask.point(lambda v: 255 if v >= SILHOUETTE else 0).getbbox()
    if not bbox:
        return Image.new("RGBA", (size, size), (0, 0, 0, 0)), {}, ["на картинке не нашлось фигуры: маска пустая"]

    # One scale for all sprites: the canvas over the height of the generation, not over each silhouette
    scale = size / image.height
    rgba = image.convert("RGBA")
    rgba.putalpha(mask)
    sprite = finish(resized(rgba, (round(image.width * scale), round(image.height * scale))), post)

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
    return canvas, {}, warnings


def make_frame(image, mask, post, points):
    x, y, w, h = post["window"]
    alpha = mask.copy()
    ImageDraw.Draw(alpha).rectangle((x, y, x + w - 1, y + h - 1), fill=0)
    rgba = image.convert("RGBA")
    rgba.putalpha(alpha)
    bbox = alpha.point(lambda v: 255 if v >= SILHOUETTE else 0).getbbox()
    if not bbox:
        return Image.new("RGBA", (post["size"],) * 2, (0, 0, 0, 0)), {}, ["рамки не нашлось: маска пустая"]
    # The frame square around the window's centre, as large as the frame reaches, then scaled to the icon size
    cx, cy = x + w / 2, y + h / 2
    half = max(cx - bbox[0], bbox[2] - cx, cy - bbox[1], bbox[3] - cy)
    box = (round(cx - half), round(cy - half), round(cx + half), round(cy + half))
    size = post["size"]
    scale = size / (box[2] - box[0])
    frame = finish(resized(rgba.crop(box), (size, size)), post)
    window = [round((x - box[0]) * scale), round((y - box[1]) * scale), round(w * scale), round(h * scale)]
    return frame, {"window": window}, []


def make_icon(image, mask, post, points):
    frame = post["frame"]
    if frame is not None and post["tint"]:
        # Recoloured by brightness: dark stays dark, light stays light, the middle takes the tint
        alpha = frame.getchannel("A")
        frame = ImageOps.colorize(frame.convert("L"), black="#000000", mid=post["tint"], white="#ffffff").convert("RGBA")
        frame.putalpha(alpha)
    width, height = frame.size if frame is not None else (post["size"], post["size"])
    x, y, w, h = post["window"] or [0, 0, width, height]
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if post["background"]:
        ImageDraw.Draw(canvas).rectangle((x, y, x + w - 1, y + h - 1), fill=post["background"])

    warnings = []
    bbox = mask.point(lambda v: 255 if v >= SILHOUETTE else 0).getbbox()
    if not bbox:
        warnings.append("на картинке не нашлось предмета: маска пустая")
    else:
        # Each item fills the window the same way, a dagger as much as a two-handed sword
        room_w, room_h = max(1, w - 2 * post["margin"]), max(1, h - 2 * post["margin"])
        rgba = image.convert("RGBA")
        rgba.putalpha(mask)
        item = rgba.crop(bbox)
        scale = min(room_w / item.width, room_h / item.height)
        item = resized(item, (max(1, round(item.width * scale)), max(1, round(item.height * scale))))
        canvas.alpha_composite(item, (x + (w - item.width) // 2, y + (h - item.height) // 2))
    if frame is not None:
        canvas.alpha_composite(frame)
    return finish(canvas, post), {}, warnings
