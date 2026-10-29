# A character cut into parts on the bones of its pose, for cutout animation in the game: the game turns a bone and
# the part on it turns with it, and equipment replaces parts instead of lying on top of them.
# - Every pixel of the figure goes to the part whose bone is nearest; inside the torso the arms keep what lies on
#   them, and the torso under them is painted by a second generation, so a raised arm uncovers armor, not a hole.
#   The neck belongs to the torso and the head starts at the jaw, so any head fits any torso.
# - Each limb also takes a disk around its joint, so a turned joint shows no gap.
# - The parts land on a square canvas with one scale for the whole generation, centred on the hips;
#   rig.json gives the bones, their pivots and the drawing order in pixels of that canvas, and the ground line.
import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage

# Back to front, for a figure facing the viewer: the legs under the torso, the head and the arms over it, a weapon
# over the fist that holds it
ORDER = ["right_thigh", "right_shin", "left_thigh", "left_shin", "torso", "head",
         "right_upper_arm", "right_forearm", "right_weapon", "left_upper_arm", "left_forearm", "left_weapon"]
REQUIRED = ("nose", "neck", "right_shoulder", "right_elbow", "right_wrist", "left_shoulder", "left_elbow",
            "left_wrist", "right_hip", "right_knee", "right_ankle", "left_hip", "left_knee", "left_ankle")
FACE = ("right_eye", "left_eye", "right_ear", "left_ear")
# Equipment is generated on the base picture, only inside the zone of its slot, and replaces parts of the base:
# armor the body without the head, the hands and the feet; a helmet the head; a weapon is a part of its own
SLOTS = ("armor", "helmet", "right_hand", "left_hand")
SLOT_PARTS = {"armor": ["torso", "right_upper_arm", "right_forearm", "left_upper_arm", "left_forearm",
                        "right_thigh", "right_shin", "left_thigh", "left_shin"],
              "helmet": ["head"], "right_hand": ["right_weapon"], "left_hand": ["left_weapon"]}
WEAPON_BONES = {"right_weapon": "right_forearm", "left_weapon": "left_forearm"}
# Joint disks, in the body size: neck to the middle of the hips
JOINTS = {"upper_arm": 0.11, "forearm": 0.09, "thigh": 0.15, "shin": 0.12}
ARM = 0.11
# Mask values below ~10% are haze around the figure, not the figure
SILHOUETTE = 26
ERASED = (228, 228, 228)
FILL_PROMPT = ", torso without arms, side of the chest"
FILL_NEGATIVE = "arm, hand, "


def missing(points, slot=None):
    return [k for k in REQUIRED + (FACE if slot == "helmet" else ()) if k not in points]


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


def torso_outline(points):
    u = unit(points)
    rs, ls = point(points, "right_shoulder"), point(points, "left_shoulder")
    rh, lh, neck = point(points, "right_hip"), point(points, "left_hip"), point(points, "neck")
    return [tuple(rs + [-0.03 * u, -0.02 * u]), tuple(neck + [-0.3 * u, -0.08 * u]), tuple(neck + [0.3 * u, -0.08 * u]),
            tuple(ls + [0.03 * u, -0.02 * u]), tuple(lh + [0.12 * u, 0.08 * u]), tuple(rh + [-0.12 * u, 0.08 * u])]


def torso_area(points, width, height):
    area = Image.new("L", (width, height), 0)
    ImageDraw.Draw(area).polygon(torso_outline(points), fill=255)
    return np.array(area) > 0


def jaw(points):
    # The top of the torso area: the head part starts above it
    return points["neck"][1] - 0.08 * unit(points)


def slot_zone(points, slot, width, height):
    # Where the model redraws the base for a slot (white), from the skeleton alone
    u = unit(points)
    zone = Image.new("L", (width, height), 0)
    pen = ImageDraw.Draw(zone)

    def capsule(a, b, radius, fill=255):
        pen.line([tuple(a), tuple(b)], fill=fill, width=round(2 * radius))
        for x, y in (a, b):
            pen.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)

    def fist(side):
        wrist, elbow = point(points, f"{side}_wrist"), point(points, f"{side}_elbow")
        return wrist + (wrist - elbow) / np.linalg.norm(wrist - elbow) * 0.1 * u

    if slot == "armor":
        # The body with room for the bulk of armor; the head, the fists and the feet stay as they are
        outline = torso_outline(points)
        pen.polygon(outline, fill=255)
        for a, b in zip(outline, outline[1:] + outline[:1]):
            capsule(a, b, 0.12 * u)
        for side in ("right", "left"):
            capsule(point(points, f"{side}_shoulder"), point(points, f"{side}_elbow"), 0.2 * u)
            capsule(point(points, f"{side}_elbow"), point(points, f"{side}_wrist"), 0.17 * u)
            capsule(point(points, f"{side}_hip"), point(points, f"{side}_knee"), 0.25 * u)
            capsule(point(points, f"{side}_knee"), point(points, f"{side}_ankle"), 0.2 * u)
        pen.rectangle((0, 0, width, jaw(points)), fill=0)
        for side in ("right", "left"):
            x, y = fist(side)
            pen.ellipse((x - 0.13 * u, y - 0.13 * u, x + 0.13 * u, y + 0.13 * u), fill=0)
            ax, ay = point(points, f"{side}_ankle")
            pen.rectangle((ax - 0.25 * u, ay + 0.02 * u, ax + 0.25 * u, height), fill=0)
    elif slot == "helmet":
        # The head with room above for crests and horns, nothing below the jaw
        width_ = 1.5 * math.dist(points["right_ear"], points["left_ear"])
        height_ = 1.25 * width_
        cx = points["nose"][0]
        cy = (points["right_eye"][1] + points["left_eye"][1]) / 2 - 0.03 * height_
        pen.ellipse((cx - 0.95 * width_, cy - height_, cx + 0.95 * width_, cy + 0.65 * height_), fill=255)
        pen.rectangle((0, jaw(points), width, height), fill=0)
    else:
        # Around the fist, reaching out, up and down; the arm stays and only the fist is redrawn around the grip.
        # "right" is the figure's own, on the left of the picture
        side = slot.split("_")[0]
        x, y = points[f"{side}_wrist"]
        out = -1 if side == "right" else 1
        near, far = x - out * 0.25 * u, x + out * 1.0 * u
        pen.rectangle((min(near, far), y - 1.2 * u, max(near, far), y + 1.0 * u), fill=255)
        capsule(point(points, f"{side}_shoulder"), point(points, f"{side}_elbow"), 0.1 * u + 4, fill=0)
        elbow, wrist = point(points, f"{side}_elbow"), point(points, f"{side}_wrist")
        pen.line([tuple(elbow), tuple(wrist)], fill=0, width=round(0.18 * u + 8))
    return zone.filter(ImageFilter.GaussianBlur(2))


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


def weapon_alpha(image, alpha, base, points, slot):
    # A weapon is what the model drew in the zone of the fist, the fist around the grip included: the figure there,
    # where it differs from the base. Holes enclosed by the weapon are filled, so a grey blade over a grey sleeve
    # stays whole
    zone = np.array(slot_zone(points, slot, *image.size)) >= 128
    changed = np.abs(np.array(image.convert("RGB")).astype(int)
                     - np.array(base.convert("RGB").resize(image.size)).astype(int)).max(axis=2) >= 24
    result = np.where(zone & changed, alpha, 0)
    solid = result >= 128
    result = np.where(ndimage.binary_fill_holes(solid) & ~solid & zone, 255, result)
    # The weapon and the fist on it are one piece; specks where the sleeve changed a little are not
    pieces, count = ndimage.label(result >= 128)
    if count > 1:
        largest = pieces == 1 + int(np.argmax(ndimage.sum(pieces > 0, pieces, range(1, count + 1))))
        result = np.where(ndimage.binary_dilation(largest, iterations=3), result, 0)
    return result


def assemble(image, mask, filled, points, cut, size, slot=None, base=None):
    # The parts on the canvas, trimmed, and the rig. With a slot,
    # only the parts that slot replaces; a weapon needs the base picture it was drawn on
    from .post import resized
    width, height = image.size
    u = unit(points)
    skeleton = bones(points)
    rgb = np.array(image.convert("RGB"))
    alpha = np.array(mask.convert("L").resize(image.size))
    alpha = np.where(alpha >= SILHOUETTE, alpha, 0)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)

    scale = size / height
    axis = (points["right_hip"][0] + points["left_hip"][0]) / 2
    shift = round(size / 2 - axis * scale)
    on_canvas = lambda p: [round(float(p[0]) * scale + shift, 1), round(float(p[1]) * scale, 1)]

    images, rig_parts, warnings = {}, [], []
    for name in SLOT_PARTS[slot] if slot else ["torso"] + cut["limbs"]:
        pixels = rgb.copy()
        if name in WEAPON_BONES:
            part_alpha = weapon_alpha(image, alpha, base, points, slot)
        else:
            own = cut["owner"] == (-1 if name == "torso" else cut["limbs"].index(name))
            part_alpha = np.where(own, alpha, 0)
        if name == "torso" and filled is not None:
            hidden = cut["hidden"]
            part_alpha = np.where(hidden, 255, part_alpha)
            pixels[hidden] = np.array(filled.convert("RGB"))[hidden]
        elif name in skeleton and name not in ("torso", "head"):
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
        part = resized(part, (round(width * scale), round(height * scale)))
        box = part.getchannel("A").getbbox()
        if not box:
            warnings.append(f"часть {name} пустая: на картинке нет " +
                            ("оружия в зоне кулака" if name in WEAPON_BONES else "этой части тела"))
            continue
        images[name] = part.crop(box)
        rig_parts.append({"name": name, "bone": WEAPON_BONES.get(name, name), "image": f"{name}.png",
                          "offset": [box[0] + shift, box[1]], "order": ORDER.index(name)})
    rig_parts.sort(key=lambda p: p["order"])

    figure_box = Image.fromarray(((alpha > 0) * 255).astype(np.uint8)).getbbox()
    ground = round(figure_box[3] * scale, 1) if figure_box else None
    if figure_box and figure_box[3] >= height:
        warnings.append("ступни упираются в нижний край генерации: подними лодыжки в позе, иначе линия пола неточна")
    rig = {"size": size, "ground": ground,
           "bones": [{"name": k, "parent": skeleton[k][0], "pivot": on_canvas(skeleton[k][1])} for k in skeleton],
           "parts": rig_parts}
    return images, rig, warnings


def transfer_matrix(name, source, target):
    # Moves a part from one skeleton onto another by the bones it hangs on: the torso by the shoulders and the middle
    # of the hips, the head by the ears and the eyes, a limb along its bone by its length and across it by the width
    # of the shoulders, a weapon with one scale, the body size, turned along the forearm with the grip in the fist
    def middle(points, a, b):
        return (point(points, a) + point(points, b)) / 2

    def turn(t):
        return np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])

    def matrix(linear, a, b):
        return np.vstack([np.column_stack([linear, b - linear @ a]), [0, 0, 1.0]])

    if name == "torso":
        anchors = lambda p: [point(p, "right_shoulder"), point(p, "left_shoulder"), middle(p, "right_hip", "left_hip")]
        solution = np.linalg.solve(np.array([[x, y, 1] for x, y in anchors(source)]), np.array(anchors(target)))
        return np.vstack([solution.T, [0, 0, 1.0]])
    if name == "head":
        scale = math.dist(target["right_ear"], target["left_ear"]) / math.dist(source["right_ear"], source["left_ear"])
        return matrix(scale * np.eye(2), middle(source, "right_eye", "left_eye"), middle(target, "right_eye", "left_eye"))
    if name in WEAPON_BONES:
        side = name.split("_")[0]
        before = point(source, f"{side}_elbow") - point(source, f"{side}_wrist")
        after = point(target, f"{side}_elbow") - point(target, f"{side}_wrist")
        angle = math.atan2(after[1], after[0]) - math.atan2(before[1], before[0])
        return matrix(unit(target) / unit(source) * turn(angle), point(source, f"{side}_wrist"),
                      point(target, f"{side}_wrist"))
    side, kind = name.split("_", 1)
    start, end = {"upper_arm": ("shoulder", "elbow"), "forearm": ("elbow", "wrist"),
                  "thigh": ("hip", "knee"), "shin": ("knee", "ankle")}[kind]
    a, b = point(source, f"{side}_{start}"), point(source, f"{side}_{end}")
    a2, b2 = point(target, f"{side}_{start}"), point(target, f"{side}_{end}")
    before, after = b - a, b2 - a2
    across = (np.linalg.norm(point(target, "left_shoulder") - point(target, "right_shoulder"))
              / np.linalg.norm(point(source, "left_shoulder") - point(source, "right_shoulder")))
    linear = (turn(math.atan2(after[1], after[0])) @ np.diag([np.linalg.norm(after) / np.linalg.norm(before), across])
              @ turn(-math.atan2(before[1], before[0])))
    return matrix(linear, a, a2)


def transfer(parts, source, size, target, width, height):
    # Parts of another skeleton's result (name, picture, offset on its canvas of side size), moved onto the target
    # skeleton in pixels of the generation, back to front
    scale = size / height
    shift = round(size / 2 - (source["right_hip"][0] + source["left_hip"][0]) / 2 * scale)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for name, picture, offset in sorted(parts, key=lambda p: ORDER.index(p[0])):
        placed = Image.new("RGBa", (width, height), (0, 0, 0, 0))
        big = picture.convert("RGBa").resize((round(picture.width / scale), round(picture.height / scale)),
                                             Image.Resampling.LANCZOS)
        placed.paste(big, (round((offset[0] - shift) / scale), round(offset[1] / scale)))
        inverse = np.linalg.inv(transfer_matrix(name, source, target))
        layer.alpha_composite(placed.transform((width, height), Image.Transform.AFFINE, tuple(inverse[:2].flatten()),
                                               Image.Resampling.BICUBIC).convert("RGBA"))
    return layer


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
