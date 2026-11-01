# Trainer service: trains a LoRA with kohya sd-scripts on request from the tool's API in ComfyUI.
# Listens only inside the Docker network and trains one LoRA at a time. The references come as a zip in
# /jobs/<job_id>/, the result goes into the LoRA library with a version: <project>/<name>-v<N>.safetensors,
# and intermediate saves next to it in <project>/<name>-v<N>/, so they can be compared before picking one.
# The settings that fit SDXL into 10 GB of VRAM are set here: they belong to the machine, not to the project.
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import toml

MODELS = Path("/models")
LORAS = Path("/loras")
JOBS = Path("/jobs")
SD_SCRIPTS = Path("/opt/sd-scripts")

IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
# Training needs nearly the whole card; ComfyUI is asked to free it first, which can take a few seconds
MIN_FREE_VRAM_GB = 8.5
VRAM_WAIT_SECONDS = 30

FIELDS = {"checkpoint": str, "steps": int, "rank": int, "alpha": (int, float), "learning_rate": float,
          "resolution": int, "repeats": int, "save_every": int, "seed": int}
DEFAULTS = {"checkpoint": "sd_xl_base_1.0.safetensors", "steps": 1500, "rank": 16, "learning_rate": 1e-4,
            "resolution": 1024, "repeats": 10, "save_every": 250, "seed": 42}

# tqdm line of sd-scripts: "steps:  66%|██████▌   | 394/600 [08:55<04:39,  1.36s/it, avr_loss=0.119]";
# other bars, such as caching latents, have no "steps:" in front
STEP = re.compile(r"steps:\s*\d+%.*?(\d+)/(\d+) \[[\d:]+<([\d:?]+)")
LOSS = re.compile(r"avr_loss=([\d.]+)")

jobs = {}
busy = threading.Lock()


def check_params(params):
    errors = []
    if not isinstance(params, dict):
        return None, ["params: JSON-объект"]
    unknown = set(params) - set(FIELDS)
    if unknown:
        errors.append(f"params: неизвестные поля: {', '.join(sorted(unknown))}")
    params = {**DEFAULTS, **params}
    for key, kind in FIELDS.items():
        # bool is an int in Python, but `steps: true` is a mistake
        if key in params and (not isinstance(params[key], kind) or isinstance(params[key], bool)):
            errors.append(f"{key}: {params[key]!r}, не того типа")
    for key in ("steps", "rank", "repeats", "save_every"):
        if isinstance(params.get(key), int) and params[key] < 1:
            errors.append(f"{key}: не меньше 1")
    if isinstance(params.get("resolution"), int) and (params["resolution"] < 256 or params["resolution"] % 64):
        errors.append("resolution: не меньше 256 и кратно 64")
    if isinstance(params.get("checkpoint"), str) and not (MODELS / "checkpoints" / params["checkpoint"]).is_file():
        errors.append(f"checkpoint: нет базовой модели {params['checkpoint']}; LoRA работает только с той базой, "
                      "на которой обучена")
    params.setdefault("alpha", params["rank"] / 2 if isinstance(params.get("rank"), int) else 8)
    return params, errors


def unpack(archive, dataset):
    # Images and captions are flattened: sd-scripts wants them directly in one folder
    errors, seen = [], {}
    try:
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                path = Path(member.filename)
                if member.is_dir() or "__MACOSX" in path.parts or path.name.startswith("."):
                    continue
                if path.suffix.lower() not in IMAGES | {".txt"}:
                    continue
                if path.name in seen:
                    errors.append(f"{path.name}: два файла с таким именем ({seen[path.name]} и {member.filename})")
                    continue
                seen[path.name] = member.filename
                (dataset / path.name).write_bytes(z.read(member))
    except zipfile.BadZipFile:
        return 0, ["dataset: не zip-архив"]
    images = [p for p in dataset.iterdir() if p.suffix.lower() in IMAGES]
    if not images:
        errors.append(f"в архиве нет картинок ({', '.join(sorted(IMAGES))})")
    for image in sorted(images):
        caption = image.with_suffix(".txt")
        if not caption.is_file() or not caption.read_text(encoding="utf-8", errors="replace").strip():
            errors.append(f"{image.name}: нет подписи {caption.name}")
    return len(images), errors


def free_vram_gb():
    # nvidia-smi, not torch: under WSL torch does not see memory held by other containers
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.splitlines()[0]) / 1024


def wait_for_vram():
    deadline = time.monotonic() + VRAM_WAIT_SECONDS
    while (free := free_vram_gb()) < MIN_FREE_VRAM_GB and time.monotonic() < deadline:
        time.sleep(2)
    return free


def next_version(project, name):
    folder = LORAS / project
    taken = [int(m.group(1)) for p in folder.iterdir() if (m := re.fullmatch(rf"{name}-v(\d+)(\.safetensors)?", p.name))] \
        if folder.is_dir() else []
    return max(taken, default=0) + 1


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seconds(text):
    if "?" in text:
        return None
    total = 0
    for part in text.split(":"):
        total = total * 60 + int(part)
    return total


def command(params, dataset_config, work, output_name):
    # No sample images during training: on 10 GB the second round of them pushed VRAM to the limit and training
    # hung, see docs/LOG.md. Intermediate saves are compared by generating with each of them instead.
    return [
        # One GPU, one process; given here because a saved accelerate config is written without seeing the GPU
        "accelerate", "launch", "--num_processes", "1", "--num_machines", "1", "--mixed_precision", "bf16",
        "--dynamo_backend", "no", "--num_cpu_threads_per_process", "1", "sdxl_train_network.py",
        "--pretrained_model_name_or_path", str(MODELS / "checkpoints" / params["checkpoint"]),
        "--dataset_config", str(dataset_config),
        "--output_dir", str(work), "--output_name", output_name,
        "--save_model_as", "safetensors", "--save_precision", "fp16",
        "--network_module", "networks.lora",
        "--network_dim", str(params["rank"]), "--network_alpha", str(params["alpha"]),
        "--learning_rate", str(params["learning_rate"]), "--lr_scheduler", "constant",
        "--max_train_steps", str(params["steps"]), "--save_every_n_steps", str(params["save_every"]),
        "--seed", str(params["seed"]),
        # What fits SDXL LoRA into 10 GB, as the sd-scripts SDXL docs recommend
        "--network_train_unet_only", "--cache_text_encoder_outputs", "--cache_latents",
        "--gradient_checkpointing", "--mixed_precision", "bf16", "--optimizer_type", "AdamW8bit",
        "--sdpa", "--no_half_vae",
        # Latents are cached, so loader workers bring nothing but shared-memory trouble in Docker
        "--max_data_loader_n_workers", "0",
    ]


def train(job):
    work, version = job["work"], job["version"]
    project, name = job["name"].split("/")
    params = job["params"]
    try:
        if job.get("cancel"):
            raise Cancelled
        job["state"] = "training"
        # Line-buffered, so the log can be read while training goes
        log = (work / "train.log").open("w", encoding="utf-8", buffering=1)
        # A session of its own: accelerate starts the training as a child process, and a cancel stops them both
        process = subprocess.Popen(command(params, work / "dataset.toml", work, version.split("/")[1]),
                                   cwd=SD_SCRIPTS, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        job["process"] = process
        tail = []
        buffer = b""
        # tqdm redraws its line with \r, so the output is split on both \r and \n to see every step
        while chunk := process.stdout.read1(4096):
            buffer += chunk
            *lines, buffer = re.split(rb"[\r\n]", buffer)
            for raw in lines:
                line = raw.decode("utf-8", errors="replace")
                if not line.strip():
                    continue
                log.write(line + "\n")
                if m := STEP.search(line):
                    job.update(step=int(m.group(1)), steps=int(m.group(2)), eta_seconds=seconds(m.group(3)))
                    if loss := LOSS.search(line):
                        job["loss"] = float(loss.group(1))
                else:
                    print(line, flush=True)
                    tail = (tail + [line])[-20:]
        log.close()
        if process.wait() and job.get("cancel"):
            raise Cancelled
        if process.returncode:
            raise RuntimeError(f"sd-scripts завершился с кодом {process.returncode}: " + " | ".join(tail[-5:]))

        # Intermediate saves into <project>/<name>-v<N>/, the final one next to them; both are seen by ComfyUI
        local = version.split("/")[1]
        steps_dir = LORAS / project / local
        steps = sorted(work.glob(f"{local}-step*.safetensors"))
        saves = [f"{project}/{local}/{p.stem}" for p in steps]
        if steps:
            steps_dir.mkdir(parents=True)
        for step in steps:
            shutil.move(step, steps_dir / step.name)
        final = LORAS / project / f"{local}.safetensors"
        shutil.move(work / f"{local}.safetensors", final)
        job.update(state="done", lora=version, sha256=sha256_of(final), intermediate=saves,
                   minutes=round((time.monotonic() - job["started"]) / 60, 1))
    except Cancelled:
        # Nothing of a cancelled training goes into the library; its log stays with the job
        for partial in work.glob("*.safetensors"):
            partial.unlink()
        job.update(state="cancelled", minutes=round((time.monotonic() - job["started"]) / 60, 1))
    except Exception as e:  # noqa: BLE001  any failure must end up in the job, or the GPU lock never lifts
        job.update(state="error", error=str(e))
    finally:
        job.pop("process", None)
        # The references belong to the project; only the records stay here
        shutil.rmtree(work / "dataset", ignore_errors=True)
        (work / "dataset.zip").unlink(missing_ok=True)
        busy.release()


def start(body):
    job_id, name = body.get("job_id"), body.get("name")
    work = JOBS / str(job_id)
    if not re.fullmatch(r"[0-9a-f-]{36}", str(job_id)) or not (work / "dataset.zip").is_file():
        return 400, {"errors": ["нет архива задания"]}
    if not isinstance(name, str) or not NAME.match(name):
        shutil.rmtree(work, ignore_errors=True)
        return 400, {"errors": ["name: «проект/имя» латиницей в нижнем регистре, цифры и дефис"]}
    params, errors = check_params(body.get("params") or {})
    dataset = work / "dataset"
    dataset.mkdir(exist_ok=True)
    count, unpack_errors = unpack(work / "dataset.zip", dataset)
    errors += unpack_errors
    if errors:
        shutil.rmtree(work, ignore_errors=True)
        return 400, {"errors": errors}
    if not busy.acquire(blocking=False):
        shutil.rmtree(work, ignore_errors=True)
        return 409, {"errors": ["уже идёт обучение, прогресс — GET /tool/status"]}
    if (free := wait_for_vram()) < MIN_FREE_VRAM_GB:
        busy.release()
        shutil.rmtree(work, ignore_errors=True)
        return 503, {"errors": [f"свободно {free:.1f} ГБ видеопамяти, нужно {MIN_FREE_VRAM_GB}: видеокарту держит "
                                "что-то кроме ComfyUI"]}

    project, local = name.split("/")
    version = f"{project}/{local}-v{next_version(project, local)}"
    (work / "dataset.toml").write_text(toml.dumps({
        # Captions cannot be shuffled with cached text encoder outputs
        "general": {"caption_extension": ".txt", "enable_bucket": True, "bucket_no_upscale": True},
        "datasets": [{"resolution": params["resolution"], "batch_size": 1,
                      "subsets": [{"image_dir": str(dataset), "num_repeats": params["repeats"]}]}],
    }))
    (work / "params.json").write_text(json.dumps({"name": name, "version": version, **params}, indent=2))
    job = {"job_id": job_id, "type": "train", "state": "starting", "name": name, "version": version,
           "images": count, "params": params, "step": 0, "steps": params["steps"], "work": work,
           "started": time.monotonic()}
    jobs[job_id] = job
    print(f"[trainer] {version}: {count} картинок × {params['repeats']} повторов, {params['steps']} шагов", flush=True)
    threading.Thread(target=train, args=(job,), daemon=True).start()
    return 202, public(job)


def public(job):
    return {k: v for k, v in job.items() if k not in ("work", "started", "process", "cancel")}


class Cancelled(Exception):
    pass


def cancel(job_id):
    # Asks sd-scripts to stop, and kills it if it has not stopped in a while; the training thread then finishes
    # the job as cancelled and lifts the lock
    job = jobs.get(job_id)
    if not job:
        return 404, {"errors": ["нет такого обучения"]}
    if job["state"] not in ("starting", "training"):
        return 409, {"errors": [f"обучение уже {job['state']}"]}
    job["cancel"] = True
    if process := job.get("process"):
        def stop():
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        threading.Thread(target=stop, daemon=True).start()
    print(f"[trainer] {job['version']}: отмена", flush=True)
    return 202, {**public(job), "state": "cancelling"}


def current():
    running = [j for j in jobs.values() if j["state"] in ("starting", "training")]
    return {"state": "training", "job": public(running[0])} if running else {"state": "idle"}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            return self.reply(200, current())
        if m := re.fullmatch(r"/jobs/([0-9a-f-]{36})", self.path):
            return self.reply(200, public(jobs[m.group(1)])) if m.group(1) in jobs else self.reply(404, {})
        self.reply(404, {})

    def do_POST(self):
        if m := re.fullmatch(r"/jobs/([0-9a-f-]{36})/cancel", self.path):
            return self.reply(*cancel(m.group(1)))
        if self.path != "/train":
            return self.reply(404, {})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.reply(*start(body))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print("[trainer] слушаю :9000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
