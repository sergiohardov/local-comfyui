# Batch run of a prompt list from the connected project through ComfyUI's API: text to image, one job per seed,
# so any image can be repeated from the list in git. The graph is the same as the txt2img and txt2img-lora
# templates in the UI.
# Usage: docker compose exec comfyui python /opt/tool/run.py prompts/<list>.yaml [--dry-run]
import argparse
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

PROJECT = Path("/project")
API = "http://127.0.0.1:8188"

# Settings of the list; each one can be overridden by an item
SETTINGS = {
    "checkpoint": str,
    "width": int,
    "height": int,
    "steps": int,
    "cfg": (int, float),
    "sampler": str,
    "scheduler": str,
    "negative": str,
    "lora": str,
    "lora_strength": (int, float),
}
DEFAULTS = {"width": 1024, "height": 1024, "steps": 28, "cfg": 6, "sampler": "dpmpp_2m",
            "scheduler": "karras", "negative": "", "lora": None, "lora_strength": 1.0}
ITEM_KEYS = {"name", "prompt", "count", "seed"} | set(SETTINGS)
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_SEED = 2**32 - 1


def fail(errors):
    print("[run] список не запущен:", flush=True)
    for error in errors:
        print(f"  - {error}", flush=True)
    sys.exit(1)


def check_settings(errors, where, values):
    for key, kind in SETTINGS.items():
        # `lora: null` turns the list's LoRA off for an item, to compare with and without it
        if key == "lora" and values.get(key, "") is None:
            continue
        # bool is an int in Python, but `width: true` is a mistake
        if key in values and (not isinstance(values[key], kind) or isinstance(values[key], bool)):
            errors.append(f"{where}: {key} — {values[key]!r}, не того типа")
    for key in ("width", "height"):
        value = values.get(key)
        if isinstance(value, int) and (value < 64 or value % 8):
            errors.append(f"{where}: {key} {value} — нужно не меньше 64 и кратно 8")
    if isinstance(values.get("steps"), int) and values["steps"] < 1:
        errors.append(f"{where}: steps должно быть не меньше 1")


def load_jobs(path, style):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        fail(["в списке нужны общие настройки и непустой items"])

    errors = []
    unknown = set(data) - set(SETTINGS) - {"items"}
    if unknown:
        errors.append(f"неизвестные поля: {', '.join(sorted(unknown))}")
    if "checkpoint" not in data:
        errors.append("checkpoint: укажи базовую модель явно, например sd_xl_base_1.0.safetensors")
    check_settings(errors, "список", data)

    jobs, names = [], set()
    for i, item in enumerate(data["items"]):
        where = f"items[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: нужен словарь с name и prompt")
            continue
        unknown = set(item) - ITEM_KEYS
        if unknown:
            errors.append(f"{where}: неизвестные поля: {', '.join(sorted(unknown))}")
        name, prompt = item.get("name"), item.get("prompt")
        if not isinstance(name, str) or not NAME.match(name):
            errors.append(f"{where}: name {name!r} — латиница в нижнем регистре, цифры и дефис")
        elif name in names:
            errors.append(f"{where}: name {name} уже был в списке")
        names.add(name)
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{where}: нужен prompt")
        count, seed = item.get("count", 1), item.get("seed")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            errors.append(f"{where}: count {count!r} — целое не меньше 1")
            count = 0
        if seed is None:
            seed = random.randint(0, MAX_SEED - count)
        elif not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= MAX_SEED - count:
            errors.append(f"{where}: seed {seed!r} — целое от 0 до {MAX_SEED - count}")
        check_settings(errors, where, item)

        settings = {**DEFAULTS, **{k: v for k, v in data.items() if k in SETTINGS},
                    **{k: v for k, v in item.items() if k in SETTINGS}}
        positive = ", ".join(p for p in (prompt, style.get("prompt")) if p)
        negative = ", ".join(n for n in (settings["negative"], style.get("negative")) if n)
        for n in range(count):
            jobs.append({**settings, "name": name, "seed": seed + n, "positive": positive, "negative": negative})

    if errors:
        fail(errors)
    return jobs


def graph(job, prefix):
    nodes = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": job["checkpoint"]}},
    }
    model, clip = ["1", 0], ["1", 1]
    if job["lora"]:
        # Same as the txt2img-lora template: the LoRA sits between the checkpoint and everything that uses it
        nodes["8"] = {"class_type": "LoraLoader", "inputs": {
            "model": model, "clip": clip, "lora_name": job["lora"],
            "strength_model": job["lora_strength"], "strength_clip": job["lora_strength"]}}
        model, clip = ["8", 0], ["8", 1]
    return nodes | {
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": job["positive"]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": job["negative"]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": job["width"], "height": job["height"], "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": model, "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": job["seed"], "steps": job["steps"], "cfg": job["cfg"],
            "sampler_name": job["sampler"], "scheduler": job["scheduler"], "denoise": 1}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": prefix}},
    }


def call(path, data=None):
    request = urllib.request.Request(API + path, method="POST" if data is not None else "GET",
                                     data=json.dumps(data).encode() if data is not None else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description="Прогон списка промптов проекта: текст → картинка")
    parser.add_argument("list", help="файл списка относительно папки проекта, например prompts/references.yaml")
    parser.add_argument("--dry-run", action="store_true", help="показать задания и ничего не генерировать")
    args = parser.parse_args()

    path = (PROJECT / args.list).resolve()
    if not path.is_relative_to(PROJECT) or not path.is_file():
        fail([f"нет файла {args.list} в папке проекта"])
    config = PROJECT / "project.yaml"
    style = (yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("style") or {} if config.is_file() else {}
    jobs = load_jobs(path, style)
    folder = path.stem

    try:
        checkpoints = call("/models/checkpoints")
    except urllib.error.URLError:
        fail([f"ComfyUI не отвечает на {API}: контейнер только что запущен или проект не прошёл проверку, "
              "см. docker compose logs comfyui"])
    missing = sorted({job["checkpoint"] for job in jobs} - set(checkpoints))
    if missing:
        fail([f"нет базовой модели {name}, есть: {', '.join(checkpoints) or 'ничего'}" for name in missing])
    loras = call("/models/loras")
    missing = sorted({job["lora"] for job in jobs if job["lora"]} - set(loras))
    if missing:
        fail([f"нет LoRA {name} в loras/ проекта, есть: {', '.join(loras) or 'ничего'}" for name in missing])

    print(f"[run] {path.relative_to(PROJECT)}: картинок {len(jobs)}, в output/{folder}/", flush=True)
    if args.dry_run:
        for job in jobs:
            lora = f", LoRA {job['lora']} × {job['lora_strength']}" if job["lora"] else ""
            print(f"  {job['name']}-s{job['seed']}  {job['width']}×{job['height']}, {job['steps']} шагов, "
                  f"cfg {job['cfg']}{lora}\n    + {job['positive']}\n    - {job['negative']}", flush=True)
        return

    queued = []
    for job in jobs:
        label = f"{job['name']}-s{job['seed']}"
        try:
            queued.append((label, call("/prompt", {"prompt": graph(job, f"{folder}/{label}")})["prompt_id"]))
        except urllib.error.HTTPError as e:
            # ComfyUI explains a rejected graph in the body: unknown sampler, missing model and so on
            body = json.loads(e.read() or b"{}")
            reasons = [f"{err['message']}: {err['details']}" for node in body.get("node_errors", {}).values()
                       for err in node["errors"]] or [body.get("error", {}).get("message", f"HTTP {e.code}")]
            fail([f"{label}: ComfyUI не принял задание: {r}" for r in reasons]
                 + ([f"уже поставлено в очередь и будет сгенерировано: {len(queued)}"] if queued else []))

    failed = 0
    for n, (label, prompt_id) in enumerate(queued, 1):
        started = time.monotonic()
        while not (entry := call(f"/history/{prompt_id}").get(prompt_id)):
            time.sleep(1)
        status = entry["status"]["status_str"]
        if status == "success":
            files = [f"output/{i['subfolder']}/{i['filename']}" for i in entry["outputs"]["7"]["images"]]
            print(f"[run] {n}/{len(queued)} {label}: {', '.join(files)} ({time.monotonic() - started:.0f} с)", flush=True)
        else:
            failed += 1
            messages = [m for m in entry["status"].get("messages", []) if m[0] == "execution_error"]
            print(f"[run] {n}/{len(queued)} {label}: ошибка {messages}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
