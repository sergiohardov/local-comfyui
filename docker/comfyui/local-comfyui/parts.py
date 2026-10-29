# A character cut into parts on the bones of its pose, for cutout animation in the game: the game turns a bone and
# the part on it turns with it, and equipment replaces parts instead of lying on top of them.
# - Every pixel of the figure goes to the part whose bone is nearest; inside the torso the arms keep what lies on
#   them, and the torso under them is painted by a second generation, so a raised arm uncovers armor, not a hole.
#   The neck belongs to the torso and the head starts at the jaw, so any head fits any torso.
# - Each limb also takes a disk around its joint, so a turned joint shows no gap.
# - The parts land on the canvas of the sprites with one scale for the whole generation, centred on the hips;
#   rig.json gives the bones, their pivots and the drawing order in pixels of that canvas, and the ground line.
import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

# Back to front, for a figure facing the viewer: the legs under the torso, the head and the arms over it
ORDER = ["right_thigh", "right_shin", "left_thigh", "left_shin", "torso", "head",
         "right_upper_arm", "right_forearm", "left_upper_arm", "left_forearm"]
REQUIRED = ("nose", "neck", "right_shoulder", "right_elbow", "right_wrist", "left_shoulder", "left_elbow",
            "left_wrist", "right_hip", "right_knee", "right_ankle", "left_hip", "left_knee", "left_ankle")
# Joint disks, in the body size: neck to the middle of the hips
JOINTS = {"upper_arm": 0.11, "forearm": 0.09, "thigh": 0.15, "shin": 0.12}
ARM = 0.11
# Mask values below ~10% are haze around the figure, not the figure
SILHOUETTE = 26
ERASED = (228, 228, 228)
FILL_PROMPT = ", torso without arms, side of the chest"
FILL_NEGATIVE = "arm, hand, "


def missing(points):
    return [k for k in REQUIRED if k not in points]


def point(points, name):
    return np.array(points[name], dtype=float)


def unit(points):
    hips = (point(points, "right_hip") + point(points, "left_hip")) / 2
    return float(np.linalg.norm(point(points, "neck") - hips))


def bones(points):
    # name -> (parent, pivot, the segment the part's pixels gather around)
    u = unit(points)
    neck, nose = point(points, "neck"), point(points, "nose")

    def beyond(a, b, k):
        a, b = point(points, a), point(points, b)
        return b + (b - a) / np.linalg.norm(b - a) * k * u

    result = {"torso": (None, (point(points, "right_hip") + point(points, "left_hip")) / 2, None),
              "head": ("torso", neck, (neck + 0.35 * (nose - neck), nose - np.array([0, 0.45 * u])))}
    for side in ("right", "left"):
        result[f"{side}_upper_arm"] = ("torso", point(points, f"{side}_shoulder"),
                                       (point(points, f"{side}_shoulder"), point(points, f"{side}_elbow")))
        result[f"{side}_forearm"] = (f"{side}_upper_arm", point(points, f"{side}_elbow"),
                                     (point(points, f"{side}_elbow"), beyond(f"{side}_elbow", f"{side}_wrist", 0.25)))
        result[f"{side}_thigh"] = ("torso", point(points, f"{side}_hip"),
                                   (point(points, f"{side}_hip"), point(points, f"{side}_knee")))
        result[f"{side}_shin"] = (f"{side}_thigh", point(points, f"{side}_knee"),
                                  (point(points, f"{side}_knee"), beyond(f"{side}_knee", f"{side}_ankle", 0.25)))
    return result


def torso_area(points, width, height):
    u = unit(points)
    rs, ls = point(points, "right_shoulder"), point(points, "left_shoulder")
    rh, lh, neck = point(points, "right_hip"), point(points, "left_hip"), point(points, "neck")
    area = Image.new("L", (width, height), 0)
    ImageDraw.Draw(area).polygon([tuple(rs + [-0.03 * u, -0.02 * u]), tuple(neck + [-0.3 * u, -0.08 * u]),
                                  tuple(neck + [0.3 * u, -0.08 * u]), tuple(ls + [0.03 * u, -0.02 * u]),
                                  tuple(lh + [0.12 * u, 0.08 * u]), tuple(rh + [-0.12 * u, 0.08 * u])], fill=255)
    return np.array(area) > 0


def plan(points, mask):
    # Which part owns each pixel (-1 the torso, -2 nothing, else an index into limbs) and what the arms hide
    width, height = mask.size
    u = unit(points)
    skeleton = bones(points)
    limbs = [k for k in skeleton if k != "torso"]
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)

    def distance(a, b):
        ab = b - a
        t = np.clip(((xs - a[0]) * ab[0] + (ys - a[1]) * ab[1]) / (ab @ ab), 0, 1)
        return np.hypot(xs - (a[0] + t * ab[0]), ys - (a[1] + t * ab[1]))

    distances = np.stack([distance(*skeleton[k][2]) for k in limbs])
    figure = np.array(mask.convert("L")) >= SILHOUETTE
    inside = torso_area(points, width, height)
    owner = np.full((height, width), -1)
    order = np.argsort(distances, axis=0)
    owner[~inside] = order[0][~inside]
    arms = [i for i, k in enumerate(limbs) if "arm" in k]
    arm_order = np.array(arms)[np.argmin(distances[arms], axis=0)]
    on_arm = inside & (np.min(distances[arms], axis=0) < ARM * u)
    owner[on_arm] = arm_order[on_arm]
    owner[~figure] = -2
    # A limb is one piece: the rest, such as a belt pouch next to the fist, goes to the part it touches most, usually
    # the torso. Pieces are counted on solid pixels, shrunk a little, so a faint bridge a pixel or two wide does not
    # join them
    solid = np.array(mask.convert("L")) >= 128
    for i in range(len(limbs)):
        own = owner == i
        pieces, count = ndimage.label(ndimage.binary_erosion(own & solid, iterations=2))
        if count < 2:
            continue
        largest = pieces == 1 + int(np.argmax(ndimage.sum(pieces > 0, pieces, range(1, count + 1))))
        strays, stray_count = ndimage.label(own & ~ndimage.binary_dilation(largest, iterations=3))
        for stray in range(1, stray_count + 1):
            where = strays == stray
            ring = owner[ndimage.binary_dilation(where, iterations=3) & ~where]
            ring = ring[(ring != i) & (ring != -2)]
            owner[where] = np.bincount(ring + 1).argmax() - 1 if ring.size else -1
    return {"owner": owner, "limbs": limbs, "hidden": on_arm & figure, "figure": figure}


def fill_inputs(image, cut):
    # The picture with the arms erased and the mask of what they hid; None when the arms hide nothing
    if not cut["hidden"].any():
        return None
    erased = np.array(image.convert("RGB"))
    erased[np.isin(cut["owner"], [i for i, k in enumerate(cut["limbs"]) if "arm" in k])] = ERASED
    hidden = Image.fromarray((cut["hidden"] * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(9))
    return Image.fromarray(erased), hidden


def assemble(image, mask, filled, points, cut, size, finish):
    # The parts on the canvas, trimmed, and the rig; finish is the hard edges and palette of post.py
    from .post import resized
    width, height = image.size
    u = unit(points)
    skeleton = bones(points)
    limbs = cut["limbs"]
    owner = cut["owner"]
    rgb = np.array(image.convert("RGB"))
    alpha = np.array(mask.convert("L").resize(image.size))
    alpha = np.where(alpha >= SILHOUETTE, alpha, 0)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)

    scale = size / height
    axis = (points["right_hip"][0] + points["left_hip"][0]) / 2
    shift = round(size / 2 - axis * scale)
    on_canvas = lambda p: [round(float(p[0]) * scale + shift, 1), round(float(p[1]) * scale, 1)]

    images, rig_parts, warnings = {}, [], []
    for name in ["torso"] + limbs:
        own = owner == (-1 if name == "torso" else limbs.index(name))
        pixels, part_alpha = rgb.copy(), np.where(own, alpha, 0)
        if name == "torso" and filled is not None:
            hidden = cut["hidden"]
            part_alpha = np.where(hidden, 255, part_alpha)
            pixels[hidden] = np.array(filled.convert("RGB"))[hidden]
        elif name != "head":
            px, py = skeleton[name][1]
            disk = np.hypot(xs - px, ys - py) < JOINTS[name.split("_", 1)[1]] * u
            part_alpha = np.where(disk & cut["figure"], np.maximum(part_alpha, alpha), part_alpha)
            # The disk may catch a bit of a neighbour that does not touch the limb; such bits would float in the air,
            # so the limb keeps its largest solid piece and the soft edge around it
            pieces, count = ndimage.label(part_alpha >= 128)
            if count > 1:
                largest = pieces == 1 + int(np.argmax(ndimage.sum(pieces > 0, pieces, range(1, count + 1))))
                part_alpha = np.where(ndimage.binary_dilation(largest, iterations=3), part_alpha, 0)
        part = Image.fromarray(np.dstack([pixels, part_alpha]).astype(np.uint8), "RGBA")
        part = finish(resized(part, (round(width * scale), round(height * scale))))
        box = part.getchannel("A").getbbox()
        if not box:
            warnings.append(f"часть {name} пустая: на картинке нет этой части тела")
            continue
        images[name] = part.crop(box)
        rig_parts.append({"name": name, "bone": name, "image": f"{name}.png",
                          "offset": [box[0] + shift, box[1]], "order": ORDER.index(name)})
    rig_parts.sort(key=lambda p: p["order"])

    figure_box = Image.fromarray((cut["figure"] * 255).astype(np.uint8)).getbbox()
    ground = round(figure_box[3] * scale, 1) if figure_box else None
    if figure_box and figure_box[3] >= height:
        warnings.append("ступни упираются в нижний край генерации: подними лодыжки в позе, иначе линия пола неточна")
    rig = {"size": size, "ground": ground,
           "bones": [{"name": k, "parent": skeleton[k][0], "pivot": on_canvas(skeleton[k][1])} for k in skeleton],
           "parts": rig_parts}
    return images, rig, warnings


def rotation(pivot, degrees):
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    return np.array([[c, -s, pivot[0] - c * pivot[0] + s * pivot[1]],
                     [s, c, pivot[1] - s * pivot[0] - c * pivot[1]], [0, 0, 1.0]])


def frame(size, skeleton, parts, angles, background):
    # skeleton: bone -> (parent, pivot), in order from the root; parts: (bone, picture, offset) back to front
    world = {}
    for bone, (parent, pivot) in skeleton.items():
        local = rotation(pivot, angles.get(bone, 0))
        world[bone] = local if parent is None else world[parent] @ local
    canvas = Image.new("RGBA", (size, size), background)
    for bone, picture, offset in parts:
        layer = Image.new("RGBa", (size, size), (0, 0, 0, 0))
        layer.paste(picture.convert("RGBa"), tuple(offset))
        inverse = np.linalg.inv(world[bone])
        canvas.alpha_composite(layer.transform((size, size), Image.Transform.AFFINE, tuple(inverse[:2].flatten()),
                                               Image.Resampling.BICUBIC).convert("RGBA"))
    return canvas


def animate(size, skeleton, parts, poses, steps, fps, background):
    # A GIF through the poses and back to the first, eased between them
    frames = []
    for a, b in zip(poses, poses[1:] + poses[:1]):
        for i in range(steps):
            t = (1 - math.cos(math.pi * i / steps)) / 2
            angles = {k: a.get(k, 0) * (1 - t) + b.get(k, 0) * t for k in set(a) | set(b)}
            frames.append(frame(size, skeleton, parts, angles, background).convert("RGB"))
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=round(1000 / fps), loop=0)
    return out.getvalue()
