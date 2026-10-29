# HTTP API of the tool for projects on other machines: generation, job results, status and the LoRA library.
# It runs inside ComfyUI as an extension, so the UI and the API share one address, but it queues work through
# ComfyUI's own HTTP API on localhost: jobs from here and from the UI go into one queue under the same rules.
import asyncio
import hashlib
import io
import json
import os
import random
import re
import shutil
import time
import uuid
from pathlib import Path

import aiohttp
import comfy.samplers
import folder_paths
from aiohttp import web
from server import PromptServer

from PIL import Image, UnidentifiedImageError

from . import parts as cutting
from . import pose as pose_drawing
from . import post as sprite
from .graph import IMAGE, MASK, remove_background, txt2img

COMFY = "http://127.0.0.1:8188"
# The trainer service, reachable only inside the Docker network
TRAINER = "http://trainer:9000"
JOBS = Path("/jobs")
server = PromptServer.instance
routes = server.routes

# The same range as the seed widget of KSampler in the UI, so any seed seen there can be repeated here
MAX_SEED = 2**64 - 1
MAX_COUNT = 16
MAX_LORA_BYTES = 2 * 2**30
DEFAULTS = {"checkpoint": "sd_xl_base_1.0.safetensors", "negative": "", "width": 1024, "height": 1024,
            "steps": 28, "cfg": 6.0, "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1, "loras": []}
FIELDS = set(DEFAULTS) | {"prompt", "seed", "pose", "post", "init"}
# Pose ControlNet: the xinsir one listed in the README, at full strength for the whole sampling
POSE_DEFAULTS = {"model": "xinsir-openpose-sdxl.safetensors", "strength": 1.0, "start": 0.0, "end": 1.0}
POSE_FIELDS = set(POSE_DEFAULTS) | {"points"}
# Library names are <project>/<name>; deeper paths are intermediate saves of training
UPLOAD_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9_.-]*$")
# A trained LoRA gets -v<N> added to its name, so the name itself is plainer
TRAIN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

jobs = {}
# Step progress of the prompt being sampled, from the "progress" events ComfyUI sends to the UI
progress = {}
# Set while a training is being handed to the trainer, before the trainer itself reports it
handing_over = False
last_trainer = {"state": "unavailable"}


def watch_progress(send_sync):
    def wrapped(event, data, sid=None):
        if event == "progress" and isinstance(data, dict) and data.get("prompt_id"):
            progress[data["prompt_id"]] = {"step": data.get("value"), "steps": data.get("max")}
        send_sync(event, data, sid)
    return wrapped


server.send_sync = watch_progress(server.send_sync)


def reply(data, status=200):
    # Messages are in Russian; plain UTF-8 keeps them readable in a terminal instead of \u escapes
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


def errors_response(errors, status=400):
    return reply({"errors": errors}, status=status)


async def call_comfy(method, path, body=None):
    return await call(method, COMFY + path, body)


async def call(method, url, body=None, timeout=None):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.request(method, url, json=body) as response:
            # Some ComfyUI routes, such as /free, answer with an empty body
            text = await response.text()
            return response.status, json.loads(text) if text.strip() else {}


async def trainer_status():
    # The trainer is the source of truth about training: the lock survives a restart of ComfyUI mid-training.
    # While sd-scripts loads a model the trainer can be slow to answer; a slow trainer keeps its last known
    # state, so a training is never taken for idle and the lock never lifts by mistake
    global last_trainer
    try:
        _, last_trainer = await call("GET", TRAINER + "/status", timeout=10)
    except aiohttp.ClientConnectorError:
        last_trainer = {"state": "unavailable"}
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return last_trainer


@web.middleware
async def block_while_training(request, handler):
    # Generation and training do not fit into 10 GB together. This covers the API and the UI alike:
    # both queue through POST /prompt, and the UI shows the message as an error
    if request.method == "POST" and request.path in ("/prompt", "/api/prompt"):
        trainer = await trainer_status()
        if handing_over or trainer["state"] == "training":
            name = trainer.get("job", {}).get("version", "")
            return reply({"error": {"type": "training", "message": f"идёт обучение LoRA {name}, генерация недоступна; "
                                    "прогресс — GET /tool/status", "details": "", "extra_info": {}},
                          "node_errors": {}}, status=423)
    return await handler(request)


server.app.middlewares.append(block_while_training)


def loras_dir():
    return Path(folder_paths.get_folder_paths("loras")[0])


def lora_file(name):
    return name if name.endswith(".safetensors") else name + ".safetensors"


def library_path(name):
    # Any name from a request must stay inside the library
    root = loras_dir().resolve()
    path = (root / lora_file(name)).resolve()
    return path if path.is_relative_to(root) and path != root else None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def check_generate(body):
    if not isinstance(body, dict):
        return None, ["нужен JSON-объект с полем prompt"]
    errors = []
    unknown = set(body) - FIELDS
    if unknown:
        errors.append(f"неизвестные поля: {', '.join(sorted(unknown))}")
    params = {**DEFAULTS, **{k: v for k, v in body.items() if k in FIELDS}}

    if not isinstance(params.get("prompt"), str) or not params["prompt"].strip():
        errors.append("prompt: обязательная непустая строка")
    if not isinstance(params["negative"], str):
        errors.append("negative: строка")
    if params["checkpoint"] not in folder_paths.get_filename_list("checkpoints"):
        errors.append(f"checkpoint: нет базовой модели {params['checkpoint']!r}, есть: "
                      f"{', '.join(folder_paths.get_filename_list('checkpoints')) or 'ничего'}")
    for key in ("width", "height"):
        if not is_int(params[key]) or params[key] < 64 or params[key] > 4096 or params[key] % 8:
            errors.append(f"{key}: целое от 64 до 4096, кратное 8")
    if not is_int(params["steps"]) or not 1 <= params["steps"] <= 150:
        errors.append("steps: целое от 1 до 150")
    if not is_number(params["cfg"]) or not 0 <= params["cfg"] <= 30:
        errors.append("cfg: число от 0 до 30")
    if params["sampler"] not in comfy.samplers.KSampler.SAMPLERS:
        errors.append(f"sampler: {params['sampler']!r} не из {', '.join(comfy.samplers.KSampler.SAMPLERS)}")
    if params["scheduler"] not in comfy.samplers.KSampler.SCHEDULERS:
        errors.append(f"scheduler: {params['scheduler']!r} не из {', '.join(comfy.samplers.KSampler.SCHEDULERS)}")
    if not is_int(params["count"]) or not 1 <= params["count"] <= MAX_COUNT:
        errors.append(f"count: целое от 1 до {MAX_COUNT}")

    seed = params.get("seed")
    if seed is None:
        params["seed"] = random.randint(0, MAX_SEED - MAX_COUNT)
    elif not is_int(seed) or not 0 <= seed <= MAX_SEED - (params["count"] if is_int(params["count"]) else 1) + 1:
        errors.append(f"seed: целое от 0 до {MAX_SEED}, с учётом count")

    loras = []
    available = set(folder_paths.get_filename_list("loras"))
    if not isinstance(params["loras"], list):
        errors.append("loras: список вида [{\"name\": \"проект/имя\", \"strength\": 0.8}]")
    else:
        for i, lora in enumerate(params["loras"]):
            if not isinstance(lora, dict) or not isinstance(lora.get("name"), str) or set(lora) - {"name", "strength"}:
                errors.append(f"loras[{i}]: нужны name и, если надо, strength")
                continue
            strength = lora.get("strength", 1.0)
            if not is_number(strength) or not -5 <= strength <= 5:
                errors.append(f"loras[{i}]: strength — число от -5 до 5")
            if lora_file(lora["name"]) not in available:
                errors.append(f"loras[{i}]: нет LoRA {lora['name']!r} в библиотеке, список — GET /tool/loras")
            loras.append({"name": lora["name"].removesuffix(".safetensors"), "file": lora_file(lora["name"]),
                          "strength": strength})
    params["loras"] = loras

    if "pose" in params:
        pose = params["pose"]
        if not isinstance(pose, dict) or set(pose) - POSE_FIELDS or "points" not in pose:
            errors.append(f"pose: объект с points и, если надо, {', '.join(sorted(POSE_FIELDS - {'points'}))}")
        else:
            pose = {**POSE_DEFAULTS, **pose}
            if is_int(params["width"]) and is_int(params["height"]):
                errors += pose_drawing.check(pose["points"], params["width"], params["height"])
            for key, top in (("strength", 2), ("start", 1), ("end", 1)):
                if not is_number(pose[key]) or not 0 <= pose[key] <= top:
                    errors.append(f"pose.{key}: число от 0 до {top}")
            if is_number(pose["start"]) and is_number(pose["end"]) and pose["start"] >= pose["end"]:
                errors.append("pose: start должен быть меньше end")
            if pose["model"] not in folder_paths.get_filename_list("controlnet"):
                errors.append(f"pose.model: нет ControlNet {pose['model']!r}, есть: "
                              f"{', '.join(folder_paths.get_filename_list('controlnet')) or 'ничего'}")
            params["pose"] = pose

    if "init" in params:
        init = params["init"]
        if not isinstance(init, dict) or set(init) - {"image", "denoise", "slot"} or "image" not in init:
            errors.append("init: объект с image (PNG в base64) и, если надо, denoise и slot")
        else:
            # A slot redraws its zone anew; a whole picture only part of the way
            init = {"denoise": 1.0 if "slot" in init else 0.75, **init}
            picture = sprite.decode_png(init["image"])
            if picture is None:
                errors.append("init.image: PNG или JPEG в base64")
            init["picture"] = picture
            if not is_number(init["denoise"]) or not 0.05 <= init["denoise"] <= 1:
                errors.append("init.denoise: от 0.05 до 1 — насколько уходить от исходной картинки")
            if "slot" in init:
                points = params["pose"].get("points") if isinstance(params.get("pose"), dict) else None
                if init["slot"] not in cutting.SLOTS:
                    errors.append(f"init.slot: {' / '.join(cutting.SLOTS)}")
                elif not isinstance(points, dict):
                    errors.append("init.slot: нужна поза — по её скелету строится зона слота")
                elif lacking := cutting.missing(points, init["slot"]):
                    errors.append(f"pose.points: для слота {init['slot']} нужны ещё {', '.join(lacking)}")
            params["init"] = init

    if "post" in params:
        size_ok = is_int(params["width"]) and is_int(params["height"])
        points = params["pose"].get("points") if isinstance(params.get("pose"), dict) else None
        params["post"], post_errors = sprite.check(params["post"], folder_paths.get_filename_list("background_removal"),
                                                   params["width"] if size_ok else 1024,
                                                   params["height"] if size_ok else 1024, points)
        errors += post_errors
    return params, errors


@routes.post("/tool/generate")
async def generate(request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return errors_response(["тело запроса — не JSON"])
    params, errors = check_generate(body)
    if errors:
        return errors_response(errors)

    job_id = str(uuid.uuid4())
    seeds = [params["seed"] + i for i in range(params["count"])]
    extra = {}
    # Pictures the graph loads go to ComfyUI's input folder, where LoadImage takes them from
    folder = Path(folder_paths.get_input_directory()) / "api" / job_id
    folder.mkdir(parents=True)
    slot = (params.get("init") or {}).get("slot")
    if pose := params.get("pose"):
        # A helmet covers the face: without the face points ControlNet does not ask the model for one
        points = {k: v for k, v in pose["points"].items() if not (slot == "helmet" and k in cutting.FACE + ("nose",))}
        pose_drawing.draw(points, params["width"], params["height"]).save(folder / "pose.png")
        pose["image"] = f"api/{job_id}/pose.png"
        extra["pose_url"] = f"/view?filename=pose.png&subfolder=api/{job_id}&type=input"
    if slot:
        # Equipment is drawn on the base only inside the zone of its slot
        cutting.slot_zone(pose["points"], slot, params["width"], params["height"]).save(folder / "zone.png")
        params["init"]["mask"] = f"api/{job_id}/zone.png"
        extra["zone_url"] = f"/view?filename=zone.png&subfolder=api/{job_id}&type=input"
    post = params.get("post")
    if post and post["type"] == "frame" and "init" not in params:
        # A frame is generated from a layout the tool draws, so its window is exactly where the post-processing
        # cuts it out
        params["init"] = {"picture": sprite.frame_layout(params["width"], params["height"], post["window"]),
                          "denoise": post["denoise"], "drawn": True}
    if init := params.get("init"):
        init["picture"].convert("RGB").resize((params["width"], params["height"])).save(folder / "init.png")
        init["image"] = f"api/{job_id}/init.png"
        extra["init_url"] = f"/view?filename=init.png&subfolder=api/{job_id}&type=input"
    prompts = []
    for seed in seeds:
        status, answer = await call_comfy("POST", "/prompt", {"prompt": txt2img(params, seed, f"api/{job_id}/{seed}"),
                                                          "client_id": "tool-api"})
        if status != 200:
            reasons = rejection(status, answer)
            if prompts:
                reasons.append(f"уже поставлено в очередь: {len(prompts)}, задание {job_id}")
                jobs[job_id] = {"id": job_id, "type": "generate", "params": params, "prompts": prompts}
            return errors_response(reasons, 423 if status == 423 else 400)
        prompts.append({"seed": seed, "prompt_id": answer["prompt_id"]})

    jobs[job_id] = {"id": job_id, "type": "generate", "params": params, "prompts": prompts}
    public = {k: v for k, v in params.items() if k not in ("loras", "pose", "post", "init")} | {
        "loras": [{"name": l["name"], "strength": l["strength"]} for l in params["loras"]]}
    if pose:
        public["pose"] = {k: v for k, v in pose.items() if k != "image"}
    if post:
        public["post"] = sprite.public(post)
    if init := params.get("init"):
        # The picture itself is not sent back: init_url points at it
        public["init"] = {"denoise": init["denoise"], **({"drawn": True} if init.get("drawn") else {}),
                          **({"slot": init["slot"]} if init.get("slot") else {})}
    return reply({"job_id": job_id, "seeds": seeds, "params": public, **extra}, status=202)


def rejection(status, answer):
    # 423 while training, 400 when ComfyUI rejects the graph; its own explanation goes back as is
    error = answer.get("error", {})
    return [f"{e['message']}: {e['details']}" for node in answer.get("node_errors", {}).values()
            for e in node.get("errors", [])] or [error.get("message", f"ComfyUI ответил {status}")]


def view_url(image):
    return f"/view?filename={image['filename']}&subfolder={image['subfolder']}&type={image['type']}"


def build_result(picture, mask, post, points, target):
    # Pillow work in a thread, so the server keeps answering meanwhile
    picture = picture if isinstance(picture, Image.Image) else Image.open(picture)
    mask = mask if isinstance(mask, Image.Image) else Image.open(mask)
    result, extra, warnings = sprite.make(picture, mask, post, points)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.save(target)
    return extra, warnings


async def make_result(found, prompt, picture, mask):
    # The post-processed picture: <seed>_sprite.png, _frame.png or _icon.png, linked as sprite_url, frame_url, icon_url
    params = found["params"]
    kind = params["post"]["type"]
    name = f"{prompt['seed']}_{kind}.png" if prompt["seed"] is not None else f"{kind}.png"
    points = (params.get("pose") or {}).get("points")
    target = Path(folder_paths.get_output_directory()) / "api" / found["id"] / name
    extra, warnings = await asyncio.get_running_loop().run_in_executor(
        None, build_result, picture, mask, params["post"], points, target)
    return {f"{kind}_url": f"/view?filename={name}&subfolder=api/{found['id']}&type=output", **extra,
            "warnings": warnings}


async def make_sprite(found, prompt, outputs):
    output = Path(folder_paths.get_output_directory())
    picture = output / outputs[IMAGE]["images"][0]["subfolder"] / outputs[IMAGE]["images"][0]["filename"] \
        if IMAGE in outputs else found["source"]
    mask = output / outputs[MASK]["images"][0]["subfolder"] / outputs[MASK]["images"][0]["filename"]
    return await make_result(found, prompt, picture, mask)


def load(picture):
    return picture if isinstance(picture, Image.Image) else Image.open(picture)


def prepare_fill(picture, mask, points, folder, name, slot):
    # The torso under the arms is painted from the picture with the arms erased; False when the arms hide nothing
    # or the result has no torso: a helmet or a weapon
    if slot and "torso" not in cutting.SLOT_PARTS[slot]:
        return False
    inputs = cutting.fill_inputs(load(picture), cutting.plan(points, load(mask)))
    if inputs is None:
        return False
    folder.mkdir(parents=True, exist_ok=True)
    inputs[0].save(folder / f"{name}_erased.png")
    inputs[1].save(folder / f"{name}_hidden.png")
    return True


def build_parts(picture, mask, filled, points, post, folder, slot, base):
    picture, mask = load(picture), load(mask)
    cut = None if slot in ("right_hand", "left_hand") else cutting.plan(points, mask)
    images, rig, warnings = cutting.assemble(picture, mask, load(filled) if filled else None, points, cut,
                                             post["size"], lambda part: sprite.finish(part, post), slot,
                                             load(base) if base else None)
    folder.mkdir(parents=True, exist_ok=True)
    for name, image in images.items():
        image.save(folder / f"{name}.png")
    (folder / "rig.json").write_text(json.dumps(rig, ensure_ascii=False, indent=1))
    return rig, warnings


async def parts_stage(found, prompt):
    # Parts take a second generation, the torso under the arms; this runs on each poll until the parts are ready
    params, job_id = found["params"], found["id"]
    points = params["pose"]["points"]
    name = str(prompt["seed"]) if prompt["seed"] is not None else "source"
    # Equipment gives only the parts of its slot; a weapon is told from the base it was drawn on
    slot = (params.get("init") or {}).get("slot")
    base = Path(folder_paths.get_input_directory()) / "api" / job_id / "init.png" if slot else None
    loop = asyncio.get_running_loop()
    busy = {"state": "running", "stage": "нарезка на части"}
    if prompt.get("fill") == "preparing" or prompt.get("assembling"):
        return busy
    if "fill" not in prompt:
        prompt["fill"] = "preparing"
        folder = Path(folder_paths.get_input_directory()) / "api" / job_id
        needed = await loop.run_in_executor(None, prepare_fill, prompt["picture"], prompt["mask"], points, folder, name,
                                            slot)
        if not needed:
            prompt["fill"] = None
        else:
            # The style of the generation: its checkpoint, LoRA and prompt; a picture sent to /tool/post has none
            base = params if "prompt" in params else {**DEFAULTS, "prompt": "a game character"}
            fill = {**base, "prompt": base["prompt"] + cutting.FILL_PROMPT,
                    "negative": cutting.FILL_NEGATIVE + base["negative"], "pose": None, "post": None,
                    "init": {"image": f"api/{job_id}/{name}_erased.png", "denoise": 1.0,
                             "mask": f"api/{job_id}/{name}_hidden.png"}}
            status, answer = await call_comfy("POST", "/prompt", {
                "prompt": txt2img(fill, prompt["seed"] or 0, f"api/{job_id}/{name}_fill"), "client_id": "tool-api"})
            if status != 200:
                del prompt["fill"]
                return {"state": "error", "error": "; ".join(rejection(status, answer))}
            prompt["fill"] = answer["prompt_id"]
    filled = None
    if prompt["fill"]:
        status, history = await call_comfy("GET", f"/history/{prompt['fill']}")
        entry = history.get(prompt["fill"]) if status == 200 else None
        if not entry:
            return {"state": "running", "stage": "дорисовка туловища под руками"}
        if entry["status"].get("status_str") != "success" or IMAGE not in entry["outputs"]:
            return {"state": "error", "error": "дорисовка туловища под руками не удалась"}
        image = entry["outputs"][IMAGE]["images"][0]
        filled = Path(folder_paths.get_output_directory()) / image["subfolder"] / image["filename"]
    prompt["assembling"] = True
    try:
        subfolder = f"api/{job_id}/{name}_parts"
        rig, warnings = await loop.run_in_executor(None, build_parts, prompt["picture"], prompt["mask"], filled,
                                                   points, params["post"],
                                                   Path(folder_paths.get_output_directory()) / subfolder, slot, base)
    finally:
        prompt["assembling"] = False
    return {"state": "done", "rig": rig, "warnings": warnings,
            "parts": {p["name"]: f"/view?filename={p['image']}&subfolder={subfolder}&type=output" for p in rig["parts"]}}


async def finish_parts(found, prompt, result):
    stage = await parts_stage(found, prompt)
    if stage["state"] != "done":
        return result | stage
    prompt["result"] = result | stage
    return prompt["result"]


async def prompt_state(found, prompt, queue):
    # A finished result does not change, and a sprite is made once
    if "result" in prompt:
        return prompt["result"]
    if prompt["prompt_id"] is None:
        # A picture sent with its own transparency: straight to the parts
        return await finish_parts(found, prompt, {"state": "done"})
    prompt_id = prompt["prompt_id"]
    status, history = await call_comfy("GET", f"/history/{prompt_id}")
    entry = history.get(prompt_id) if status == 200 else None
    if entry:
        if entry["status"].get("status_str") == "success":
            # One prompt per seed with batch 1, so one image
            result = {"state": "done"}
            if IMAGE in entry["outputs"]:
                result["url"] = view_url(entry["outputs"][IMAGE]["images"][0])
            post = found["params"].get("post")
            if post and post["type"] == "parts":
                output = Path(folder_paths.get_output_directory())
                outputs = entry["outputs"]
                prompt.setdefault("picture", output / outputs[IMAGE]["images"][0]["subfolder"] /
                                  outputs[IMAGE]["images"][0]["filename"] if IMAGE in outputs else found["source"])
                prompt.setdefault("mask", output / outputs[MASK]["images"][0]["subfolder"] /
                                  outputs[MASK]["images"][0]["filename"])
                return await finish_parts(found, prompt, result)
            if post:
                result |= await make_sprite(found, prompt, entry["outputs"])
            prompt["result"] = result
            return result
        messages = [m[1].get("exception_message", "") for m in entry["status"].get("messages", [])
                    if m[0] == "execution_error"]
        return {"state": "error", "error": "; ".join(messages) or "ComfyUI не выполнил задание"}
    if any(item[1] == prompt_id for item in queue["queue_running"]):
        return {"state": "running", **progress.get(prompt_id, {})}
    pending = sorted(item[0] for item in queue["queue_pending"])
    own = [item[0] for item in queue["queue_pending"] if item[1] == prompt_id]
    if own:
        return {"state": "queued", "ahead": pending.index(own[0])}
    return {"state": "lost", "error": "задания нет ни в очереди, ни в истории: ComfyUI перезапускался"}


@routes.get("/tool/jobs/{job_id}")
async def job(request):
    job_id = request.match_info["job_id"]
    found = jobs.get(job_id)
    if not found:
        # Not a generation: maybe a training, which the trainer keeps
        if re.fullmatch(r"[0-9a-f-]{36}", job_id):
            try:
                status, training = await call("GET", f"{TRAINER}/jobs/{job_id}", timeout=10)
                if status == 200:
                    return reply(training)
            except aiohttp.ClientConnectorError:
                pass
            except (aiohttp.ClientError, asyncio.TimeoutError):
                return errors_response(["тренер не ответил вовремя, он занят загрузкой модели; повтори запрос"], 503)
        return errors_response(["нет такого задания; задания живут до перезапуска инструмента"], 404)
    _, queue = await call_comfy("GET", "/queue")
    images = []
    for prompt in found["prompts"]:
        images.append({"seed": prompt["seed"], **await prompt_state(found, prompt, queue)})
    states = {i["state"] for i in images}
    state = next(s for s in ("error", "lost", "running", "queued", "done") if s in states)
    if state == "queued" and "done" in states:
        state = "running"
    return reply({"job_id": job_id, "type": found["type"], "state": state, "images": images})


async def vram():
    # nvidia-smi, not torch: under WSL torch does not see memory held by other containers, such as the trainer
    try:
        process = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE)
        out, _ = await process.communicate()
        used, total = (int(v) for v in out.decode().splitlines()[0].split(","))
        return {"used_mb": used, "total_mb": total}
    except (OSError, ValueError, IndexError):
        return None


@routes.get("/tool/status")
async def status(request):
    _, queue = await call_comfy("GET", "/queue")
    running = [item[1] for item in queue["queue_running"]]
    current = None
    if running:
        prompt_id = running[0]
        job_id = next((j for j, v in jobs.items() if any(p["prompt_id"] == prompt_id for p in v["prompts"])), None)
        current = {"job_id": job_id, "prompt_id": prompt_id, **progress.get(prompt_id, {})}
    trainer = await trainer_status()
    if trainer["state"] == "training":
        current = trainer["job"]
    return reply({
        "state": "training" if trainer["state"] == "training" else "generating" if running else "idle",
        "current": current,
        "trainer": "available" if trainer["state"] != "unavailable" else "unavailable",
        "queue": {"running": len(running), "pending": len(queue["queue_pending"])},
        "vram": await vram(),
        "checkpoints": folder_paths.get_filename_list("checkpoints"),
    })


@routes.get("/tool/loras")
async def list_loras(request):
    root = loras_dir()
    items = []
    for path in sorted(root.rglob("*.safetensors")):
        stat = path.stat()
        items.append({"name": path.relative_to(root).as_posix().removesuffix(".safetensors"), "size": stat.st_size,
                      "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime))})
    return reply({"loras": items})


@routes.get("/tool/loras/{name:.+}")
async def download_lora(request):
    path = library_path(request.match_info["name"])
    if not path or not path.is_file():
        return errors_response(["нет такой LoRA в библиотеке"], 404)
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


@routes.delete("/tool/loras/{name:.+}")
async def delete_lora(request):
    path = library_path(request.match_info["name"])
    if not path or not path.is_file():
        return errors_response(["нет такой LoRA в библиотеке"], 404)
    path.unlink()
    # A project folder left empty goes too, so the list in the UI stays clean
    root = loras_dir().resolve()
    for parent in path.parents:
        if parent == root or any(parent.iterdir()):
            break
        parent.rmdir()
    return reply({"deleted": request.match_info["name"].removesuffix(".safetensors")})


def check_safetensors(path):
    # A safetensors file starts with the length of its JSON header; anything else is not a LoRA we can load
    with path.open("rb") as f:
        size = int.from_bytes(f.read(8), "little")
        if not 2 <= size <= 100 * 2**20:
            return False
        try:
            json.loads(f.read(size))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
    return True


@routes.post("/tool/loras")
async def upload_lora(request):
    # multipart/form-data: name (<project>/<name>) first, then file; ?overwrite=true replaces an existing one
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return errors_response(["нужен multipart/form-data с полями name и file"])
    name, saved = None, None
    async for part in reader:
        if part.name == "name":
            name = (await part.text()).strip().removesuffix(".safetensors")
        elif part.name == "file":
            if not name or not UPLOAD_NAME.match(name):
                return errors_response(["name: «проект/имя» латиницей в нижнем регистре, и поле name — перед file"])
            target = library_path(name)
            if target.exists() and request.query.get("overwrite") != "true":
                return errors_response([f"LoRA {name} уже есть; заменить — ?overwrite=true"], 409)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".uploading")
            digest, size = hashlib.sha256(), 0
            with temp.open("wb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    size += len(chunk)
                    if size > MAX_LORA_BYTES:
                        f.close()
                        temp.unlink()
                        return errors_response(["файл больше 2 ГБ — это не LoRA"], 413)
                    digest.update(chunk)
                    f.write(chunk)
            if not check_safetensors(temp):
                temp.unlink()
                return errors_response(["файл не в формате safetensors"])
            os.replace(temp, target)
            saved = {"name": name, "size": size, "sha256": digest.hexdigest()}
    if not saved:
        return errors_response(["нет поля file"])
    return reply(saved, status=201)


@routes.post("/tool/train")
async def train(request):
    # multipart/form-data: name (<project>/<name>), params (JSON, optional), then dataset: a zip of images
    # with captions in .txt files of the same name. The trainer checks them and trains; this side hands over the GPU
    global handing_over
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return errors_response(["нужен multipart/form-data с полями name, params и dataset"])
    job_id = str(uuid.uuid4())
    work = JOBS / job_id
    name, params, saved = None, {}, False
    async for part in reader:
        if part.name == "name":
            name = (await part.text()).strip()
        elif part.name == "params":
            try:
                params = json.loads(await part.text() or "{}")
            except json.JSONDecodeError:
                return errors_response(["params: не JSON"])
        elif part.name == "dataset":
            if not name or not TRAIN_NAME.match(name):
                return errors_response(["name: «проект/имя» латиницей в нижнем регистре, цифры и дефис; "
                                        "поле name — перед dataset"])
            work.mkdir(parents=True)
            with (work / "dataset.zip").open("wb") as f:
                while chunk := await part.read_chunk(1 << 20):
                    f.write(chunk)
            saved = True
    if not saved:
        shutil.rmtree(work, ignore_errors=True)
        return errors_response(["нет поля dataset с zip-архивом"])

    if handing_over or (await trainer_status())["state"] == "training":
        shutil.rmtree(work, ignore_errors=True)
        return errors_response(["уже идёт обучение, прогресс — GET /tool/status"], 409)
    handing_over = True
    try:
        # Training takes the whole GPU, so it starts only when nothing is generating; from here on the lock
        # turns new generations away, and ComfyUI unloads its models to free the memory
        _, queue = await call_comfy("GET", "/queue")
        busy = len(queue["queue_running"]) + len(queue["queue_pending"])
        if busy:
            shutil.rmtree(work, ignore_errors=True)
            return errors_response([f"идёт генерация, в очереди {busy}; обучение — когда очередь опустеет"], 409)
        await call_comfy("POST", "/free", {"unload_models": True, "free_memory": True})
        try:
            status, answer = await call("POST", TRAINER + "/train", {"job_id": job_id, "name": name, "params": params},
                                        timeout=180)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            shutil.rmtree(work, ignore_errors=True)
            return errors_response(["тренер недоступен: docker compose ps, docker compose logs trainer"], 503)
    finally:
        handing_over = False
    return reply(answer, status)


@routes.post("/tool/pose")
async def draw_pose(request):
    # Only the skeleton picture, without generating: to check a pose while editing it, or to load it in the UI
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return errors_response(["тело запроса — не JSON"])
    if not isinstance(body, dict) or set(body) - {"points", "width", "height"}:
        return errors_response(["нужен объект с points и, если надо, width и height"])
    width, height = body.get("width", DEFAULTS["width"]), body.get("height", DEFAULTS["height"])
    if not all(is_int(v) and 64 <= v <= 4096 for v in (width, height)):
        return errors_response(["width, height: целые от 64 до 4096"])
    errors = pose_drawing.check(body.get("points"), width, height)
    if errors:
        return errors_response(errors)
    buffer = io.BytesIO()
    pose_drawing.draw(body["points"], width, height).save(buffer, "PNG")
    return web.Response(body=buffer.getvalue(), content_type="image/png")


@routes.post("/tool/post")
async def post_picture(request):
    # multipart/form-data: post (JSON, optional), pose (JSON {"points": ...}, optional: the body axis; parts need it),
    # then image. A picture with transparency is laid out by its own alpha at once; one without goes through
    # background removal
    try:
        reader = await request.multipart()
    except (AssertionError, ValueError):
        return errors_response(["нужен multipart/form-data с полями post, pose и image"])
    fields, data = {}, None
    async for part in reader:
        if part.name in ("post", "pose"):
            try:
                fields[part.name] = json.loads(await part.text() or "{}")
            except json.JSONDecodeError:
                return errors_response([f"{part.name}: не JSON"])
        elif part.name == "image":
            data = await part.read()
    if data is None:
        return errors_response(["нет поля image с картинкой"])
    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except (UnidentifiedImageError, OSError):
        return errors_response(["image: не картинка"])

    pose = fields.get("pose", {})
    points = pose.get("points") if isinstance(pose, dict) else None
    errors = []
    if pose and points is None:
        errors.append("pose: объект {\"points\": {...}} с точками OpenPose")
    elif points is not None:
        errors += pose_drawing.check(points, picture.width, picture.height)
    post, post_errors = sprite.check(fields.get("post", {}), folder_paths.get_filename_list("background_removal"),
                                     picture.width, picture.height, None if errors else points)
    errors += post_errors
    if errors:
        return errors_response(errors)

    job_id = str(uuid.uuid4())
    params = {"post": post, "pose": {"points": points} if points else None}
    found = {"id": job_id, "type": "post", "params": params}
    if "A" in picture.getbands() and picture.getchannel("A").getextrema()[0] < 255:
        prompt = {"seed": None, "prompt_id": None}
        if post["type"] == "parts":
            # Parts still need the torso under the arms painted: the job finishes on polling
            prompt |= {"picture": picture.convert("RGB"), "mask": picture.getchannel("A")}
        else:
            prompt["result"] = {"state": "done", **await make_result(found, prompt, picture.convert("RGB"),
                                                                     picture.getchannel("A"))}
        found["prompts"] = [prompt]
    else:
        folder = Path(folder_paths.get_input_directory()) / "api" / job_id
        folder.mkdir(parents=True)
        picture.convert("RGB").save(folder / "source.png")
        found["source"] = folder / "source.png"
        status, answer = await call_comfy("POST", "/prompt", {
            "prompt": remove_background(f"api/{job_id}/source.png", post["model"], f"api/{job_id}/source_mask"),
            "client_id": "tool-api"})
        if status != 200:
            return errors_response(rejection(status, answer), 423 if status == 423 else 400)
        found["prompts"] = [{"seed": None, "prompt_id": answer["prompt_id"]}]
    jobs[job_id] = found
    return reply({"job_id": job_id, "params": {"post": sprite.public(post), "pose": params["pose"]}}, status=202)


def check_animation(body):
    # The bones of a rig, parts from any rigs of the same skeleton (back to front) and poses as angles of bones
    if not isinstance(body, dict) or set(body) - {"size", "bones", "parts", "poses", "steps", "fps", "background"}:
        return None, ["нужен объект с size, bones, parts, poses и, если надо, steps, fps, background"]
    errors = []
    size, steps, fps = body.get("size"), body.get("steps", 8), body.get("fps", 12)
    background = body.get("background", "#ffffff")
    if not is_int(size) or not 16 <= size <= 2048:
        errors.append("size: сторона холста частей из rig, целое от 16 до 2048")
    if not is_int(steps) or not 1 <= steps <= 30:
        errors.append("steps: кадров между позами, от 1 до 30")
    if not is_int(fps) or not 1 <= fps <= 50:
        errors.append("fps: от 1 до 50")
    if not isinstance(background, str) or not sprite.HEX.match(background):
        errors.append("background: цвет вида \"#a1b2c3\"")
    skeleton = {}
    for i, bone in enumerate(body.get("bones") if isinstance(body.get("bones"), list) else [None]):
        if not (isinstance(bone, dict) and isinstance(bone.get("name"), str) and bone.get("name") not in skeleton
                and (bone.get("parent") is None or bone.get("parent") in skeleton)
                and isinstance(bone.get("pivot"), list) and len(bone["pivot"]) == 2
                and all(is_number(v) for v in bone["pivot"])):
            errors.append(f"bones[{i}]: name, parent (уже названная кость или null) и pivot [x, y], как в rig")
            continue
        skeleton[bone["name"]] = (bone["parent"], bone["pivot"])
    parts = []
    for i, part in enumerate(body.get("parts") if isinstance(body.get("parts"), list) else [None]):
        picture = sprite.decode_png(part.get("image")) if isinstance(part, dict) else None
        if not (isinstance(part, dict) and part.get("bone") in skeleton and picture is not None
                and isinstance(part.get("offset"), list) and len(part["offset"]) == 2
                and all(is_int(v) for v in part["offset"])):
            errors.append(f"parts[{i}]: bone из bones, image (PNG в base64) и offset [x, y] из rig")
            continue
        parts.append((part["bone"], picture.convert("RGBA"), part["offset"]))
    poses = body.get("poses")
    if not isinstance(poses, list) or not 1 <= len(poses) <= 32 or not all(
            isinstance(p, dict) and all(k in skeleton and is_number(v) and -360 <= v <= 360 for k, v in p.items())
            for p in poses):
        errors.append("poses: от 1 до 32 поз вида {\"right_forearm\": 30, ...} — углы костей в градусах")
    return (size, skeleton, parts, poses, steps, fps, background), errors


@routes.post("/tool/animate")
async def animate(request):
    # A preview GIF of parts on their bones going through the poses, to check parts and their mixes without the game
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return errors_response(["тело запроса — не JSON"])
    args, errors = check_animation(body)
    if errors:
        return errors_response(errors)
    gif = await asyncio.get_running_loop().run_in_executor(None, cutting.animate, *args)
    return web.Response(body=gif, content_type="image/gif")
