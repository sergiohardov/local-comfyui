# Our nodes: operations ComfyUI has no node for, so any project graph can use them, in the UI as well as through
# ComfyUI's own API. They wrap the same code the /tool/... API uses, so a graph gives the same pictures.
# Conventions: points and windows are JSON strings, so a client puts its values into a graph as text; a mask is
# the foreground (white is the figure), as RemoveBackground gives it; a picture with transparency is an IMAGE
# with four channels, which SaveImage writes as RGBA, and a frame loaded with LoadImage gets its alpha back
# through JoinImageWithAlpha.
import json

import numpy as np
import torch
from PIL import Image

from . import parts as cutting
from . import pose as pose_drawing
from . import post as processing

CATEGORY = "local-comfyui"
EXAMPLE_POINTS = json.dumps({"nose": [512, 150], "neck": [512, 260], "right_shoulder": [420, 270],
                             "right_elbow": [380, 400], "right_wrist": [360, 520], "left_shoulder": [604, 270],
                             "left_elbow": [644, 400], "left_wrist": [664, 520], "right_hip": [460, 560],
                             "right_knee": [450, 740], "right_ankle": [445, 920], "left_hip": [564, 560],
                             "left_knee": [574, 740], "left_ankle": [579, 920]})


def to_pictures(images):
    # IMAGE [batch, height, width, channels] in 0..1 → Pillow, rounded the way SaveImage rounds
    return [Image.fromarray(np.clip(255. * i.cpu().numpy(), 0, 255).astype(np.uint8)) for i in images]


def to_masks(masks):
    return [Image.fromarray(np.clip(255. * m.cpu().numpy(), 0, 255).astype(np.uint8), "L") for m in masks]


def to_images(pictures):
    return torch.stack([torch.from_numpy(np.array(p).astype(np.float32) / 255.0) for p in pictures])


def parse(text, what, example):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise ValueError(f"{what}: JSON вида {example}") from None


def window_of(text, width, height):
    # Empty means the default; otherwise [x, y, width, height] in pixels inside width × height
    if not text.strip():
        return None
    window = parse(text, "window", "[x, y, ширина, высота]")
    if errors := processing.check_window(window, width, height, ""):
        raise ValueError(errors[0].removeprefix("."))
    return window


def color(text, where):
    if text.strip() and not processing.HEX.match(text.strip()):
        raise ValueError(f"{where}: цвет вида \"#a1b2c3\" или пусто")
    return text.strip() or None


class PoseSkeleton:
    # The skeleton picture for the OpenPose ControlNet from named points, as /tool/generate draws it for pose
    DESCRIPTION = "Скелет OpenPose по точкам в пикселях холста — картинка для ControlNet позы."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "points": ("STRING", {"multiline": True, "default": EXAMPLE_POINTS,
                                  "tooltip": "{\"neck\": [x, y], ...} — точки OpenPose в пикселях холста"}),
            "width": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            "height": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            "face": ("BOOLEAN", {"default": True,
                                 "tooltip": "точки лица; без них ControlNet не просит лица — для закрытого шлема"}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("pose",)
    FUNCTION = "draw"
    CATEGORY = CATEGORY

    def draw(self, points, width, height, face):
        points = parse(points, "points", "{\"neck\": [x, y], ...}")
        if errors := pose_drawing.check(points, width, height):
            raise ValueError("; ".join(e.replace("pose.points", "points") for e in errors))
        if not face:
            points = {k: v for k, v in points.items() if k not in cutting.FACE + ("nose",)}
        return (to_images([pose_drawing.draw(points, width, height)]),)


class FrameLayout:
    # The layout a frame is generated from by img2img: a plain ring around the window, so the window is known
    DESCRIPTION = "Заготовка рамки: кольцо вокруг окна для img2img. Окно идёт дальше, в Cut Frame."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "width": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            "height": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            "window": ("STRING", {"default": "",
                                  "tooltip": "[x, y, ширина, высота] в пикселях генерации; пусто — квадрат "
                                             "в половину стороны по центру"}),
        }}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("layout", "window")
    FUNCTION = "draw"
    CATEGORY = CATEGORY

    def draw(self, width, height, window):
        window = window_of(window, width, height) or processing.default_window(width, height)
        return to_images([processing.frame_layout(width, height, window)]), json.dumps(window)


class CutFrame:
    # The frame out of its generation: transparent outside by the mask and inside the window, cropped square
    # around the window and scaled to the icon size; the window comes back in pixels of the frame
    DESCRIPTION = "Рамка из генерации: снаружи прозрачно по маске, окно прозрачно. Окно — в пикселях рамки."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "mask": ("MASK", {"tooltip": "передний план, из Remove Background"}),
            "window": ("STRING", {"default": "", "tooltip": "окно из Frame Layout"}),
            "size": ("INT", {"default": 128, "min": 16, "max": 4096, "tooltip": "сторона рамки"}),
        }}

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("frame", "window")
    FUNCTION = "cut"
    CATEGORY = CATEGORY

    def cut(self, image, mask, window, size):
        frames, windows, warnings = [], [], []
        for picture, alpha in zip(to_pictures(image), to_masks(mask)):
            where = window_of(window, picture.width, picture.height) \
                or processing.default_window(picture.width, picture.height)
            frame, extra, problems = processing.make(picture, alpha, {"type": "frame", "window": where, "size": size})
            frames.append(frame)
            windows.append(extra.get("window"))
            warnings += problems
        # The windows also go to /history, so a client reads them without saving a text file
        return {"ui": {"window": windows, "warnings": warnings},
                "result": (to_images(frames), json.dumps(windows[0]))}


class Icon:
    # An item fitted into the window of a frame, on a window colour, under the frame, the frame tinted if asked
    DESCRIPTION = "Иконка: предмет без фона по центру окна рамки, под рамкой. Без рамки — на холсте size."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE", {"tooltip": "предмет"}),
            "mask": ("MASK", {"tooltip": "передний план предмета, из Remove Background"}),
            "window": ("STRING", {"default": "", "tooltip": "окно рамки из Cut Frame; без рамки — "
                                                            "в пикселях холста, пусто — весь холст"}),
            "size": ("INT", {"default": 128, "min": 16, "max": 4096, "tooltip": "холст без рамки"}),
            "margin": ("INT", {"default": 6, "min": 0, "max": 1024, "tooltip": "отступ предмета от края окна"}),
            "background": ("STRING", {"default": "", "tooltip": "цвет окна #rrggbb; пусто — прозрачно"}),
            "tint": ("STRING", {"default": "", "tooltip": "цвет, в который тонируется рамка; пусто — как есть"}),
        }, "optional": {
            "frame": ("IMAGE", {"tooltip": "рамка с прозрачностью: из Cut Frame или LoadImage → "
                                           "Join Image with Alpha"}),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("icon",)
    FUNCTION = "make"
    CATEGORY = CATEGORY

    def make(self, image, mask, window, size, margin, background, tint, frame=None):
        if frame is not None:
            # Rounded, not cut like a generation: LoadImage keeps alpha as 1 - a and Join Image with Alpha turns
            # it back, so an opaque 1.0 comes as 0.99999994, which cutting makes 254
            frame = Image.fromarray(np.round(255. * frame[0].cpu().numpy()).clip(0, 255).astype(np.uint8))
            frame = frame.convert("RGBA")
            if not window.strip():
                raise ValueError("window: окно рамки из Cut Frame — с рамкой обязательно")
        width, height = frame.size if frame is not None else (size, size)
        post = {"type": "icon", "frame": frame, "window": window_of(window, width, height),
                "size": size, "margin": margin, "background": color(background, "background"),
                "tint": color(tint, "tint")}
        icons, warnings = [], []
        for picture, alpha in zip(to_pictures(image), to_masks(mask)):
            icon, _, problems = processing.make(picture, alpha, post)
            icons.append(icon)
            warnings += problems
        return {"ui": {"warnings": warnings}, "result": (to_images(icons),)}


NODE_CLASS_MAPPINGS = {
    "LocalPoseSkeleton": PoseSkeleton,
    "LocalFrameLayout": FrameLayout,
    "LocalCutFrame": CutFrame,
    "LocalIcon": Icon,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LocalPoseSkeleton": "Pose Skeleton (local-comfyui)",
    "LocalFrameLayout": "Frame Layout (local-comfyui)",
    "LocalCutFrame": "Cut Frame (local-comfyui)",
    "LocalIcon": "Icon (local-comfyui)",
}
