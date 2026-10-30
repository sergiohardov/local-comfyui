# Our nodes: operations ComfyUI has no node for, so any project graph can use them, in the UI as well as through
# ComfyUI's own API. They wrap the same code the /tool/... API uses, so a graph gives the same pictures.
# Conventions: points and windows are JSON strings, so a client puts its values into a graph as text; a mask is
# the foreground (white is the figure), as RemoveBackground gives it; a picture with transparency is an IMAGE
# with four channels, which SaveImage writes as RGBA, and a frame loaded with LoadImage gets its alpha back
# through JoinImageWithAlpha.
import json
import os
from pathlib import Path

import folder_paths
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


def points_of(text, width, height, where="points", bones=True, slot=None):
    # Named OpenPose points in pixels of a width × height canvas; the parts and the slots are built on the bones,
    # so they need all body points, a helmet the face as well
    points = parse(text, where, "{\"neck\": [x, y], ...}")
    if errors := pose_drawing.check(points, width, height):
        raise ValueError("; ".join(e.replace("pose.points", where) for e in errors))
    if bones and (lacking := cutting.missing(points, slot)):
        raise ValueError(f"{where}: для {'слота ' + slot if slot else 'частей'} нужны ещё {', '.join(lacking)}")
    return points


def folder_of(text):
    # A folder of parts: in input, where /upload/image puts them, or in output with " [output]", as a result of
    # Assemble Parts names it
    name, base = folder_paths.annotated_filepath(text.strip())
    base = os.path.abspath(base or folder_paths.get_input_directory())
    path = os.path.abspath(os.path.join(base, name))
    if os.path.commonpath((path, base)) != base or not os.path.isdir(path):
        raise ValueError(f"folder: нет папки {text.strip()!r} во входных файлах — загрузи части через /upload/image "
                         f"с subfolder; папка результата — с \" [output]\" в конце")
    return Path(path)


def part_picture(folder, name, where):
    path = Path(os.path.abspath(folder / str(name)))
    if not isinstance(name, str) or os.path.commonpath((str(path), str(folder))) != str(folder) or not path.is_file():
        raise ValueError(f"{where}: нет файла {name!r} в папке частей")
    with Image.open(path) as picture:
        return picture.convert("RGBA")


def offset_of(value):
    return isinstance(value, list) and len(value) == 2 and all(processing.is_int(v) for v in value)


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
        points = points_of(points, width, height, bones=False)
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


POINTS_INPUT = ("STRING", {"multiline": True, "default": EXAMPLE_POINTS,
                           "tooltip": "точки позы, на которой сгенерирована картинка"})


class CutPlan:
    # The first stage of the parts: the arms erased from the picture and the mask of the torso they hide, for an
    # inpaint of the torso under them with the style of the generation; an empty mask when the arms hide nothing
    DESCRIPTION = ("Нарезка, шаг 1: картинка со стёртыми руками и маска скрытого ими туловища — для дорисовки "
                   "(VAE Encode → Set Latent Noise Mask → KSampler, denoise 1).")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "mask": ("MASK", {"tooltip": "передний план, из Remove Background"}),
            "points": POINTS_INPUT,
        }}

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("erased", "hidden")
    FUNCTION = "plan"
    CATEGORY = CATEGORY

    def plan(self, image, mask, points):
        erased, hidden = [], []
        for picture, alpha in zip(to_pictures(image), to_masks(mask)):
            where = points_of(points, picture.width, picture.height)
            inputs = cutting.fill_inputs(picture, cutting.plan(where, alpha.resize(picture.size)))
            erased.append(inputs[0] if inputs else picture)
            hidden.append(inputs[1] if inputs else Image.new("L", picture.size, 0))
        return to_images(erased), to_images(hidden)


class AssembleParts:
    # The parts on their bones: PNG of each part and rig.json in a folder of output. With a slot only the parts it
    # replaces; a weapon is told by what changed against the clean base
    DESCRIPTION = ("Нарезка, шаг 2: PNG частей и rig.json в папке output. Со слотом — только части слота; "
                   "оружию нужна чистая база.")
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE",),
            "mask": ("MASK", {"tooltip": "передний план, из Remove Background"}),
            "points": POINTS_INPUT,
            "size": ("INT", {"default": 512, "min": 16, "max": 4096, "tooltip": "сторона холста частей"}),
            "slot": (["none", *cutting.SLOTS], {"tooltip": "none — база целиком"}),
            "filename_prefix": ("STRING", {"default": "parts/character"}),
        }, "optional": {
            "filled": ("IMAGE", {"tooltip": "дорисованное туловище под руками, после Cut Plan"}),
            "base": ("IMAGE", {"tooltip": "чистая база, на которой нарисовано оружие"}),
        }}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("rig", "folder")
    FUNCTION = "assemble"
    CATEGORY = CATEGORY

    def assemble(self, image, mask, points, size, slot, filename_prefix, filled=None, base=None):
        slot = None if slot == "none" else slot
        weapon = slot in ("right_hand", "left_hand")
        if weapon and base is None:
            raise ValueError("base: оружие отделяется от чистой базы — подключи её")
        filled = to_pictures(filled) if filled is not None else []
        base = to_pictures(base)[0] if base is not None else None
        output = folder_paths.get_output_directory()
        folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, output)
        files, rigs, folders, warnings = [], [], [], []
        for i, (picture, alpha) in enumerate(zip(to_pictures(image), to_masks(mask))):
            where = points_of(points, picture.width, picture.height, slot=slot)
            cut = None if weapon else cutting.plan(where, alpha.resize(picture.size))
            images, rig, problems = cutting.assemble(picture, alpha, filled[min(i, len(filled) - 1)] if filled else None,
                                                     where, cut, size, slot, base)
            target = Path(folder) / f"{name}_{counter + i:05}_"
            target.mkdir(parents=True, exist_ok=True)
            for part, part_image in images.items():
                part_image.save(target / f"{part}.png")
            (target / "rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=1))
            place = "/".join(p for p in (subfolder, target.name) if p)
            files += [{"filename": p["image"], "subfolder": place, "type": "output"} for p in rig["parts"]]
            rigs.append(rig)
            folders.append(f"{place} [output]")
            warnings += problems
        # The parts show in the node; the rig and the folder also go to /history
        return {"ui": {"images": files, "rig": rigs, "folder": folders, "warnings": warnings},
                "result": (json.dumps(rigs[0], ensure_ascii=False), folders[0])}


class SlotZone:
    # Where equipment is drawn on the base, from the skeleton alone: the mask for Set Latent Noise Mask
    DESCRIPTION = "Зона слота по скелету: где модель перерисовывает базу для снаряжения (Set Latent Noise Mask)."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "points": POINTS_INPUT,
            "width": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            "height": ("INT", {"default": 1024, "min": 64, "max": 4096, "step": 8}),
            "slot": (list(cutting.SLOTS),),
        }}

    RETURN_TYPES = ("MASK",)
    RETURN_NAMES = ("zone",)
    FUNCTION = "zone"
    CATEGORY = CATEGORY

    def zone(self, points, width, height, slot):
        where = points_of(points, width, height, slot=slot)
        return (to_images([cutting.slot_zone(where, slot, width, height)]),)


def rig_parts(rig, folder, where):
    # Parts of a rig as (name, bone, picture, offset), in its order, with their PNG from the folder
    parts = []
    for i, part in enumerate(rig.get("parts") if isinstance(rig, dict) and isinstance(rig.get("parts"), list) else [None]):
        if not (isinstance(part, dict) and isinstance(part.get("bone", part.get("name")), str)
                and offset_of(part.get("offset"))):
            raise ValueError(f"{where}.parts[{i}]: bone или name, image и offset [x, y], как в rig.json")
        parts.append((part.get("name"), part.get("bone", part.get("name")),
                      part_picture(folder, part.get("image"), f"{where}.parts[{i}]"), part["offset"]))
    return parts


class TransferParts:
    # Parts of an item made on another skeleton, moved by the bones onto this one and laid over the base: the start
    # of an img2img in the zone of the slot (denoise about 0.45), so the item keeps its design and fits the body
    DESCRIPTION = ("Перенос снаряжения на другое телосложение: части предмета со скелета-источника — поверх базы "
                   "по костям. Дальше img2img в зоне слота, denoise около 0.45.")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "image": ("IMAGE", {"tooltip": "база, на которую переносится предмет"}),
            "points": ("STRING", {"multiline": True, "default": EXAMPLE_POINTS, "tooltip": "скелет базы"}),
            "source_points": ("STRING", {"multiline": True, "default": EXAMPLE_POINTS,
                                         "tooltip": "скелет, на котором сделан предмет"}),
            "rig": ("STRING", {"multiline": True, "default": "", "tooltip": "rig.json предмета"}),
            "folder": ("STRING", {"default": "", "tooltip": "папка с PNG частей: во входных файлах или "
                                                            "«… [output]» из Assemble Parts"}),
            "slot": (list(cutting.SLOTS),),
        }}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "transfer"
    CATEGORY = CATEGORY

    def transfer(self, image, points, source_points, rig, folder, slot):
        rig = parse(rig, "rig", "{\"size\": 512, \"parts\": [...]} из rig.json")
        if not isinstance(rig, dict) or not processing.is_int(rig.get("size")) or not 16 <= rig["size"] <= 4096:
            raise ValueError("rig.size: сторона холста частей из rig.json")
        parts = [(name, picture, offset) for name, _, picture, offset in rig_parts(rig, folder_of(folder), "rig")
                 if name in cutting.SLOT_PARTS[slot]]
        if not parts:
            raise ValueError(f"rig.parts: нет частей слота {slot} — {', '.join(cutting.SLOT_PARTS[slot])}")
        results = []
        for picture in to_pictures(image):
            target = points_of(points, picture.width, picture.height, slot=slot)
            source = points_of(source_points, picture.width, picture.height, "source_points", slot=slot)
            moved = cutting.transfer(parts, source, rig["size"], target, picture.width, picture.height)
            picture = picture.convert("RGBA")
            picture.alpha_composite(moved)
            results.append(picture.convert("RGB"))
        return (to_images(results),)


class AnimationPreview:
    # A GIF of parts on their bones going through poses, to check parts and their mixes without the game
    DESCRIPTION = "Превью анимации частей на костях: GIF по позам — углам костей в градусах."
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "rig": ("STRING", {"multiline": True, "default": "",
                               "tooltip": "rig.json: size, bones и parts сзади вперёд — можно из разных rig "
                                          "одного скелета"}),
            "folder": ("STRING", {"default": "", "tooltip": "папка с PNG частей: во входных файлах или "
                                                            "«… [output]» из Assemble Parts"}),
            "poses": ("STRING", {"multiline": True,
                                 "default": "[{}, {\"right_upper_arm\": 60, \"right_forearm\": 50}]",
                                 "tooltip": "1–32 позы: углы костей в градусах по часовой"}),
            "steps": ("INT", {"default": 8, "min": 1, "max": 30, "tooltip": "кадров между позами"}),
            "fps": ("INT", {"default": 12, "min": 1, "max": 50}),
            "background": ("STRING", {"default": "#ffffff"}),
            "filename_prefix": ("STRING", {"default": "parts/preview"}),
        }}

    RETURN_TYPES = ()
    FUNCTION = "animate"
    CATEGORY = CATEGORY

    def animate(self, rig, folder, poses, steps, fps, background, filename_prefix):
        rig = parse(rig, "rig", "{\"size\": 512, \"bones\": [...], \"parts\": [...]} из rig.json")
        if not isinstance(rig, dict) or not processing.is_int(rig.get("size")) or not 16 <= rig["size"] <= 2048:
            raise ValueError("rig.size: сторона холста частей, целое от 16 до 2048")
        skeleton = {}
        for i, bone in enumerate(rig.get("bones") if isinstance(rig.get("bones"), list) else [None]):
            if not (isinstance(bone, dict) and isinstance(bone.get("name"), str) and bone["name"] not in skeleton
                    and (bone.get("parent") is None or bone.get("parent") in skeleton)
                    and isinstance(bone.get("pivot"), list) and len(bone["pivot"]) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in bone["pivot"])):
                raise ValueError(f"rig.bones[{i}]: name, parent (уже названная кость или null) и pivot [x, y]")
            skeleton[bone["name"]] = (bone["parent"], bone["pivot"])
        parts = [(bone, picture, offset) for _, bone, picture, offset in rig_parts(rig, folder_of(folder), "rig")]
        if not parts:
            raise ValueError("rig.parts: нет частей — нечего показывать")
        if unknown := sorted({bone for bone, _, _ in parts} - set(skeleton)):
            raise ValueError(f"rig.parts: нет костей {', '.join(unknown)} в rig.bones")
        poses = parse(poses, "poses", "[{\"right_forearm\": 30}, ...]")
        if not isinstance(poses, list) or not 1 <= len(poses) <= 32 or not all(
                isinstance(p, dict) and all(k in skeleton and isinstance(v, (int, float)) and not isinstance(v, bool)
                                            and -360 <= v <= 360 for k, v in p.items()) for p in poses):
            raise ValueError("poses: от 1 до 32 поз вида {\"right_forearm\": 30, ...} — углы костей из rig.bones")
        background = color(background, "background") or "#ffffff"
        gif = cutting.animate(rig["size"], skeleton, parts, poses, steps, fps, background)
        folder, name, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory())
        filename = f"{name}_{counter:05}_.gif"
        Path(folder, filename).write_bytes(gif)
        return {"ui": {"images": [{"filename": filename, "subfolder": subfolder, "type": "output"}],
                       "animated": [True]}}


NODE_CLASS_MAPPINGS = {
    "LocalPoseSkeleton": PoseSkeleton,
    "LocalFrameLayout": FrameLayout,
    "LocalCutFrame": CutFrame,
    "LocalIcon": Icon,
    "LocalCutPlan": CutPlan,
    "LocalAssembleParts": AssembleParts,
    "LocalSlotZone": SlotZone,
    "LocalTransferParts": TransferParts,
    "LocalAnimationPreview": AnimationPreview,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LocalPoseSkeleton": "Pose Skeleton (local-comfyui)",
    "LocalFrameLayout": "Frame Layout (local-comfyui)",
    "LocalCutFrame": "Cut Frame (local-comfyui)",
    "LocalIcon": "Icon (local-comfyui)",
    "LocalCutPlan": "Cut Plan (local-comfyui)",
    "LocalAssembleParts": "Assemble Parts (local-comfyui)",
    "LocalSlotZone": "Slot Zone (local-comfyui)",
    "LocalTransferParts": "Transfer Parts (local-comfyui)",
    "LocalAnimationPreview": "Animation Preview (local-comfyui)",
}
