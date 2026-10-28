# Checks the connected project folder against its project.yaml, then hands the process over to ComfyUI.
# If something the project needs is missing, the container stops with a list of what to fix instead of
# failing later in the middle of a workflow. Without project.yaml the folder is the sandbox and is not checked.
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

COMFYUI = Path("/opt/comfyui")
MODELS = COMFYUI / "models"
PROJECT = Path("/project")
HASH_CACHE = Path("/cache/sha256.json")

SHARED_MODEL_FOLDERS = ["checkpoints", "controlnet", "vae", "upscale_models"]
FORMATS = {1}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TOP_KEYS = {"format", "name", "models", "loras", "style"}
STYLE_KEYS = {"prompt", "negative"}
MODEL_KEYS = {"path", "sha256"}
LORA_KEYS = {"file", "url", "sha256"}


def sha256_of(path, cache):
    # Hashing a 7 GB checkpoint takes tens of seconds, so a hash is reused while size and mtime stay the same
    stat = path.stat()
    key = f"{path}:{stat.st_size}:{stat.st_mtime_ns}"
    if key not in cache:
        print(f"[start] считаю sha256 {path.name}...", flush=True)
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        cache[key] = digest.hexdigest()
    return cache[key]


def check_file(errors, cache, path, shown, expected, label, hint):
    if not path.is_file():
        errors.append(f"{label}: нет файла {shown}. {hint}")
    elif sha256_of(path, cache) != expected:
        errors.append(f"{label}: sha256 {shown} не совпадает с project.yaml, это другая версия файла. {hint}")


def valid_entry(entry, keys):
    # An unquoted all-digit hash would be read by YAML as a number, hence the string check
    return (isinstance(entry, dict) and set(entry) == keys
            and isinstance(entry["sha256"], str) and SHA256.match(entry["sha256"]))


def inside(base, relative):
    # Paths in project.yaml must stay inside their folder
    path = (base / relative).resolve()
    return path if path.is_relative_to(base.resolve()) else None


def check_project(config, cache):
    errors = []
    if not isinstance(config, dict):
        return ["project.yaml должен быть словарём с полями format, name, models, loras, style"]

    unknown = set(config) - TOP_KEYS
    if unknown:
        errors.append(f"неизвестные поля: {', '.join(sorted(unknown))}")
    if config.get("format") not in FORMATS:
        errors.append(f"format: {config.get('format')!r}, а инструмент понимает {sorted(FORMATS)}")
    name = config.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        errors.append(f"name: {name!r}, нужны латиница в нижнем регистре, цифры и дефис")
    style = config.get("style")
    if style is not None and (not isinstance(style, dict) or not set(style) <= STYLE_KEYS
                              or not all(isinstance(v, str) for v in style.values())):
        errors.append("style: нужны только строки prompt и negative")

    for i, model in enumerate(config.get("models") or []):
        label = f"models[{i}]"
        if not valid_entry(model, MODEL_KEYS):
            errors.append(f"{label}: нужны ровно поля path и sha256; sha256 — строка в кавычках, "
                          "64 шестнадцатеричных символа в нижнем регистре")
            continue
        path = inside(MODELS, model["path"])
        if path is None:
            errors.append(f"{label}: путь {model['path']} выходит за папку моделей")
            continue
        check_file(errors, cache, path, f"MODELS_DIR/{model['path']}", model["sha256"], label,
                   "Базовые модели ставятся на машине с инструментом, см. раздел «Модели» в README инструмента.")

    for i, lora in enumerate(config.get("loras") or []):
        label = f"loras[{i}]"
        if not valid_entry(lora, LORA_KEYS):
            errors.append(f"{label}: нужны ровно поля file, url и sha256; sha256 — строка в кавычках, "
                          "64 шестнадцатеричных символа в нижнем регистре")
            continue
        path = inside(PROJECT / "loras", lora["file"])
        if path is None:
            errors.append(f"{label}: путь {lora['file']} выходит за папку loras/")
            continue
        check_file(errors, cache, path, f"loras/{lora['file']}", lora["sha256"], label,
                   f"Скачай {lora['url']} в loras/{lora['file']} папки проекта.")

    return errors


def main():
    for folder in SHARED_MODEL_FOLDERS:
        (MODELS / folder).mkdir(parents=True, exist_ok=True)

    config_path = PROJECT / "project.yaml"
    if not config_path.is_file():
        print("[start] project.yaml нет: подключена песочница, проверка пропущена", flush=True)
    else:
        cache = json.loads(HASH_CACHE.read_text()) if HASH_CACHE.is_file() else {}
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            errors = check_project(config, cache)
        except yaml.YAMLError as e:
            errors = [f"project.yaml не читается: {e}"]
        HASH_CACHE.write_text(json.dumps(cache, indent=2))
        if errors:
            print("[start] проект не готов, ComfyUI не запущен:", flush=True)
            for error in errors:
                print(f"  - {error}", flush=True)
            sys.exit(1)
        print(f"[start] проект {config['name']} подключён, проверка пройдена", flush=True)

    os.chdir(COMFYUI)
    os.execvp("python", ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"])


if __name__ == "__main__":
    main()
