# HTTP API of the tool for projects on other machines: status, the LoRA library and training. Generation goes
# through ComfyUI's own API on the same address, with graphs the projects keep (README, "Графы через API").
# It runs inside ComfyUI as an extension, so the UI and the API share one address, and it guards ComfyUI's own
# POST /prompt: generation and training do not fit into the GPU together.
import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import aiohttp
import folder_paths
from aiohttp import web
from server import PromptServer

COMFY = "http://127.0.0.1:8188"
# The trainer service, reachable only inside the Docker network
TRAINER = "http://trainer:9000"
JOBS = Path("/jobs")
server = PromptServer.instance
routes = server.routes

MAX_LORA_BYTES = 2 * 2**30
# Library names are <project>/<name>; deeper paths are intermediate saves of training
UPLOAD_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9_.-]*$")
# A trained LoRA gets -v<N> added to its name, so the name itself is plainer
TRAIN_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")

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


@routes.get("/tool/jobs/{job_id}")
async def job(request):
    # A training job, which the trainer keeps; generations are followed through ComfyUI's /history
    job_id = request.match_info["job_id"]
    if re.fullmatch(r"[0-9a-f-]{36}", job_id):
        try:
            status, training = await call("GET", f"{TRAINER}/jobs/{job_id}", timeout=10)
            if status == 200:
                return reply(training)
        except aiohttp.ClientConnectorError:
            return errors_response(["тренер недоступен: docker compose ps, docker compose logs trainer"], 503)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return errors_response(["тренер не ответил вовремя, он занят загрузкой модели; повтори запрос"], 503)
    return errors_response(["нет такого обучения: тренер помнит задания до своего перезапуска, журнал — "
                            "data/jobs/<job_id>/train.log; генерации — GET /history/{prompt_id}"], 404)


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
        current = {"prompt_id": running[0], **progress.get(running[0], {})}
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

