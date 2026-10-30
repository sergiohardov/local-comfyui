# Frames and icons from a picture and its foreground mask (Remove Background), for the nodes Cut Frame and Icon:
# - frame: an icon frame generated from a layout drawn here (Frame Layout), so the window is where it was put; the
#   outside goes transparent by the mask and the window by its rectangle, since a model cannot be trusted to leave
#   the middle empty and BiRefNet takes a filled middle for part of the frame;
# - icon: an item fitted into the window of a frame, on a window colour, under the frame, the frame tinted if asked.
import re

from PIL import Image, ImageDraw, ImageOps

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
# Mask values below ~10% are haze around the figure, not the figure
SILHOUETTE = 26


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)



def check_window(window, width, height, where):
    if not (isinstance(window, list) and len(window) == 4 and all(is_int(v) for v in window)
            and window[2] > 0 and window[3] > 0 and window[0] >= 0 and window[1] >= 0
            and window[0] + window[2] <= width and window[1] + window[3] <= height):
        return [f"{where}.window: [x, y, ширина, высота] в пикселях внутри {width}×{height}"]
    return []


def default_window(width, height):
    side = min(width, height) // 2
    return [(width - side) // 2, (height - side) // 2, side, side]




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


def make(image, mask, post):
    # A frame or an icon; returns the picture, extra fields for the reply and warnings
    image, mask = image.convert("RGB"), mask.convert("L").resize(image.size)
    return {"frame": make_frame, "icon": make_icon}[post["type"]](image, mask, post)


def make_frame(image, mask, post):
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
    frame = resized(rgba.crop(box), (size, size))
    window = [round((x - box[0]) * scale), round((y - box[1]) * scale), round(w * scale), round(h * scale)]
    return frame, {"window": window}, []


def make_icon(image, mask, post):
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
    return canvas, {}, warnings
