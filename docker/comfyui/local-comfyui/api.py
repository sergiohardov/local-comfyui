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

from . import pose as pose_drawing
from .graph import txt2img

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
FIELDS = set(DEFAULTS) | {"prompt", "seed", "pose"}
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
    if pose := params.get("pose"):
        # The skeleton picture goes to ComfyUI's input folder, where LoadImage takes it from
        folder = Path(folder_paths.get_input_directory()) / "api" / job_id
        folder.mkdir(parents=True)
        pose_drawing.draw(pose["points"], params["width"], params["height"]).save(folder / "pose.png")
        pose["image"] = f"api/{job_id}/pose.png"
        extra["pose_url"] = f"/view?filename=pose.png&subfolder=api/{job_id}&type=input"
    prompts = []
    for seed in seeds:
        status, answer = await call_comfy("POST", "/prompt", {"prompt": txt2img(params, seed, f"api/{job_id}/{seed}"),
                                                          "client_id": "tool-api"})
        if status != 200:
            # 423 while training, 400 when ComfyUI rejects the graph; its own explanation goes back as is
            error = answer.get("error", {})
            reasons = [f"{e['message']}: {e['details']}" for node in answer.get("node_errors", {}).values()
                       for e in node.get("errors", [])] or [error.get("message", f"ComfyUI ответил {status}")]
            if prompts:
                reasons.append(f"уже поставлено в очередь: {len(prompts)}, задание {job_id}")
                jobs[job_id] = {"params": params, "prompts": prompts, "created": time.time()}
            return errors_response(reasons, 423 if status == 423 else 400)
        prompts.append({"seed": seed, "prompt_id": answer["prompt_id"]})

    jobs[job_id] = {"params": params, "prompts": prompts, "created": time.time()}
    public = {k: v for k, v in params.items() if k not in ("loras", "pose")} | {
        "loras": [{"name": l["name"], "strength": l["strength"]} for l in params["loras"]]}
    if pose:
        public["pose"] = {k: v for k, v in pose.items() if k != "image"}
    return reply({"job_id": job_id, "seeds": seeds, "params": public, **extra}, status=202)


async def prompt_state(prompt_id, queue):
    status, history = await call_comfy("GET", f"/history/{prompt_id}")
    entry = history.get(prompt_id) if status == 200 else None
    if entry:
        if entry["status"].get("status_str") == "success":
            # One prompt per seed with batch 1, so one image
            image = entry["outputs"]["7"]["images"][0]
            return {"state": "done",
                    "url": f"/view?filename={image['filename']}&subfolder={image['subfolder']}&type={image['type']}"}
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
        images.append({"seed": prompt["seed"], **await prompt_state(prompt["prompt_id"], queue)})
    states = {i["state"] for i in images}
    state = next(s for s in ("error", "lost", "running", "queued", "done") if s in states)
    if state == "queued" and "done" in states:
        state = "running"
    return reply({"job_id": job_id, "type": "generate", "state": state, "images": images})


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
