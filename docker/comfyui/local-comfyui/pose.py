# Pose skeleton for the OpenPose ControlNet: a project sends 18 named body points in pixels of the canvas,
# and the tool draws the skeleton picture. The drawing follows OpenPose (its limb pairs and colours, limbs at 60%
# brightness, joints as dots) with thick lines: the xinsir ControlNet was trained on thick ones, and keeping the
# drawing here means every project gets the same lines, while the points stay usable as attachment points.
import math

from PIL import Image, ImageDraw

# OpenPose body order; "right" is the figure's own right, so on the left of a figure facing the viewer
POINTS = ["nose", "neck", "right_shoulder", "right_elbow", "right_wrist", "left_shoulder", "left_elbow", "left_wrist",
          "right_hip", "right_knee", "right_ankle", "left_hip", "left_knee", "left_ankle",
          "right_eye", "left_eye", "right_ear", "left_ear"]
LIMBS = [("neck", "right_shoulder"), ("neck", "left_shoulder"), ("right_shoulder", "right_elbow"),
         ("right_elbow", "right_wrist"), ("left_shoulder", "left_elbow"), ("left_elbow", "left_wrist"),
         ("neck", "right_hip"), ("right_hip", "right_knee"), ("right_knee", "right_ankle"),
         ("neck", "left_hip"), ("left_hip", "left_knee"), ("left_knee", "left_ankle"),
         ("neck", "nose"), ("nose", "right_eye"), ("right_eye", "right_ear"), ("nose", "left_eye"),
         ("left_eye", "left_ear")]
COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0), (0, 255, 0),
          (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255),
          (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85)]


def check(points, width, height):
    errors = []
    if not isinstance(points, dict) or not points:
        return ["pose.points: объект {\"neck\": [x, y], ...} с точками OpenPose"]
    unknown = set(points) - set(POINTS)
    if unknown:
        errors.append(f"pose.points: неизвестные точки {', '.join(sorted(unknown))}; есть {', '.join(POINTS)}")
    for name, xy in points.items():
        if name in POINTS and not (isinstance(xy, list) and len(xy) == 2
                                   and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xy)
                                   and 0 <= xy[0] <= width and 0 <= xy[1] <= height):
            errors.append(f"pose.points.{name}: [x, y] в пикселях холста {width}×{height}")
    return errors


def draw(points, width, height):
    image = Image.new("RGB", (width, height), "black")
    pen = ImageDraw.Draw(image)
    # About 16 px for a 1024 canvas
    thickness = max(4, round(max(width, height) / 64))
    for (a, b), color in zip(LIMBS, COLORS):
        if a in points and b in points:
            (x1, y1), (x2, y2) = points[a], points[b]
            dim = tuple(int(c * 0.6) for c in color)
            # A limb is an ellipse along the bone, as OpenPose draws it
            length, angle = math.hypot(x2 - x1, y2 - y1), math.atan2(y2 - y1, x2 - x1)
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            outline = []
            for i in range(36):
                t = 2 * math.pi * i / 36
                ex, ey = length / 2 * math.cos(t), thickness / 2 * math.sin(t)
                outline.append((cx + ex * math.cos(angle) - ey * math.sin(angle),
                                cy + ex * math.sin(angle) + ey * math.cos(angle)))
            pen.polygon(outline, fill=dim)
    radius = thickness / 2
    for name, color in zip(POINTS, COLORS):
        if name in points:
            x, y = points[name]
            pen.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    return image
