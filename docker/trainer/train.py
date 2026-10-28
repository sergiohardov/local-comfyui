# Trains a LoRA of the connected project with kohya sd-scripts.
# The project says what to train in training/<name>.yaml, with its images and captions in dataset/<name>/.
# The settings that fit SDXL into 10 GB of VRAM are set here: they belong to the machine, not to the project.
# Usage: docker compose run --rm trainer training/<name>.yaml
import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import toml
import yaml

PROJECT = Path("/project")
MODELS = Path("/models")
SD_SCRIPTS = Path("/opt/sd-scripts")

IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Training needs nearly the whole card; ComfyUI alone holds about 7 GB of it
MIN_FREE_VRAM_GB = 8.5

FIELDS = {
    "checkpoint": str,
    "steps": int,
    "rank": int,
    "alpha": (int, float),
    "learning_rate": float,
    "resolution": int,
    "repeats": int,
    "save_every": int,
    "seed": int,
}
DEFAULTS = {"rank": 16, "learning_rate": 1e-4, "resolution": 1024, "repeats": 10, "save_every": 250, "seed": 42}


def fail(errors):
    print("[train] обучение не запущено:", flush=True)
    for error in errors:
        print(f"  - {error}", flush=True)
    sys.exit(1)


def load_config(path):
    errors = []
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        fail(["нужен словарь с полями checkpoint, steps и другими, см. README инструмента"])
    unknown = set(config) - set(FIELDS)
    if unknown:
        errors.append(f"неизвестные поля: {', '.join(sorted(unknown))}")
    for key in ("checkpoint", "steps"):
        if key not in config:
            errors.append(f"{key}: обязательное поле")
    for key, kind in FIELDS.items():
        # bool is an int in Python, but `steps: true` is a mistake
        if key in config and (not isinstance(config[key], kind) or isinstance(config[key], bool)):
            errors.append(f"{key}: {config[key]!r}, не того типа")
    for key in ("steps", "rank", "repeats", "save_every"):
        if isinstance(config.get(key), int) and config[key] < 1:
            errors.append(f"{key}: должно быть не меньше 1")
    if isinstance(config.get("resolution"), int) and (config["resolution"] < 256 or config["resolution"] % 64):
        errors.append("resolution: не меньше 256 и кратно 64")

    if errors:
        fail(errors)
    config = {**DEFAULTS, **config}
    config.setdefault("alpha", config["rank"] / 2)
    return config


def check_project(config, name):
    errors = []
    project_file = PROJECT / "project.yaml"
    if not project_file.is_file():
        fail(["project.yaml нет: обучение идёт для подключённого проекта, укажи PROJECT_DIR в .env"])
    declared = [m.get("path") for m in yaml.safe_load(project_file.read_text(encoding="utf-8")).get("models") or []]
    if f"checkpoints/{config['checkpoint']}" not in declared:
        errors.append(f"checkpoint: {config['checkpoint']} не объявлен в models у project.yaml, "
                      "а LoRA работает только с той базой, на которой обучена")
    if not (MODELS / "checkpoints" / config["checkpoint"]).is_file():
        errors.append(f"checkpoint: нет файла MODELS_DIR/checkpoints/{config['checkpoint']}")

    dataset = PROJECT / "dataset" / name
    images = sorted(p for p in dataset.glob("*") if p.suffix.lower() in IMAGES) if dataset.is_dir() else []
    if not images:
        errors.append(f"в dataset/{name}/ нет картинок ({', '.join(sorted(IMAGES))})")
    for image in images:
        caption = image.with_suffix(".txt")
        if not caption.is_file() or not caption.read_text(encoding="utf-8").strip():
            errors.append(f"dataset/{name}/{image.name}: нет подписи {caption.name}")
    if errors:
        fail(errors)
    return dataset, len(images)


def check_vram():
    # Not torch.cuda.mem_get_info: under WSL it does not see memory held by other containers, nvidia-smi does
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    free, total = (int(v) / 1024 for v in out.splitlines()[0].split(","))
    if free < MIN_FREE_VRAM_GB:
        fail([f"свободно {free:.1f} ГБ видеопамяти из {total:.1f}, нужно хотя бы {MIN_FREE_VRAM_GB}. "
              "Останови ComfyUI: docker compose stop comfyui"])


def next_version(name):
    taken = [int(m.group(1)) for folder in (PROJECT / "loras", PROJECT / "output" / "training") if folder.is_dir()
             for p in folder.iterdir() if (m := re.fullmatch(rf"{name}-v(\d+)(\.safetensors)?", p.name))]
    return max(taken, default=0) + 1


def sha256_of(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(config, dataset_config, work, output_name):
    # No sample images during training: on 10 GB the second round of them pushed VRAM to the limit
    # and training hung, see docs/LOG.md. Intermediate saves are compared with run.py instead.
    return [
        # One GPU, one process; given here because a saved accelerate config is written without seeing the GPU
        "accelerate", "launch", "--num_processes", "1", "--num_machines", "1", "--mixed_precision", "bf16",
        "--dynamo_backend", "no", "--num_cpu_threads_per_process", "1", "sdxl_train_network.py",
        "--pretrained_model_name_or_path", str(MODELS / "checkpoints" / config["checkpoint"]),
        "--dataset_config", str(dataset_config),
        "--output_dir", str(work), "--output_name", output_name,
        "--save_model_as", "safetensors", "--save_precision", "fp16",
        "--network_module", "networks.lora",
        "--network_dim", str(config["rank"]), "--network_alpha", str(config["alpha"]),
        "--learning_rate", str(config["learning_rate"]), "--lr_scheduler", "constant",
        "--max_train_steps", str(config["steps"]), "--save_every_n_steps", str(config["save_every"]),
        "--seed", str(config["seed"]),
        # What fits SDXL LoRA into 10 GB, as the sd-scripts SDXL docs recommend
        "--network_train_unet_only", "--cache_text_encoder_outputs", "--cache_latents",
        "--gradient_checkpointing", "--mixed_precision", "bf16", "--optimizer_type", "AdamW8bit",
        "--sdpa", "--no_half_vae",
        # Latents are cached, so loader workers bring nothing but shared-memory trouble in Docker
        "--max_data_loader_n_workers", "0",
    ]


def main():
    parser = argparse.ArgumentParser(description="Обучение LoRA проекта на sd-scripts")
    parser.add_argument("config", help="файл обучения относительно папки проекта, например training/style.yaml")
    args = parser.parse_args()

    path = (PROJECT / args.config).resolve()
    if not path.is_relative_to(PROJECT) or not path.is_file():
        fail([f"нет файла {args.config} в папке проекта"])
    name = path.stem
    if not NAME.match(name):
        fail([f"имя файла {path.name}: латиница в нижнем регистре, цифры и дефис — это имя LoRA"])
    config = load_config(path)
    dataset, image_count = check_project(config, name)
    check_vram()

    version = f"{name}-v{next_version(name)}"
    work = PROJECT / "output" / "training" / version
    work.mkdir(parents=True)
    # A copy of what was asked for, next to the result, so the version can be told apart and repeated
    shutil.copy(path, work / "training.yaml")

    dataset_config = work / "dataset.toml"
    dataset_config.write_text(toml.dumps({
        # Captions cannot be shuffled with cached text encoder outputs
        "general": {"caption_extension": ".txt", "enable_bucket": True, "bucket_no_upscale": True},
        "datasets": [{"resolution": config["resolution"], "batch_size": 1,
                      "subsets": [{"image_dir": str(dataset), "num_repeats": config["repeats"]}]}],
    }))

    print(f"[train] {version}: {image_count} картинок × {config['repeats']} повторов, {config['steps']} шагов, "
          f"rank {config['rank']}, база {config['checkpoint']}", flush=True)
    started = time.monotonic()
    result = subprocess.run(command(config, dataset_config, work, version), cwd=SD_SCRIPTS)
    if result.returncode:
        fail([f"sd-scripts завершился с кодом {result.returncode}, подробности выше; черновики — в output/training/{version}/"])

    # Intermediate saves go to loras/ too, so ComfyUI sees them and run.py can compare them
    steps_dir = PROJECT / "loras" / version
    steps_dir.mkdir()
    for step in sorted(work.glob(f"{version}-step*.safetensors")):
        shutil.move(step, steps_dir / step.name)
    final = PROJECT / "loras" / f"{version}.safetensors"
    shutil.move(work / f"{version}.safetensors", final)

    minutes = (time.monotonic() - started) / 60
    print(f"\n[train] готово за {minutes:.0f} мин: loras/{final.name}", flush=True)
    print(f"[train] промежуточные: loras/{version}/, записи: output/training/{version}/", flush=True)
    print("[train] для project.yaml, после загрузки файла в Release проекта:\n", flush=True)
    print(f"  - file: {final.name}\n"
          f"    url: https://github.com/<owner>/<repo>/releases/download/<tag>/{final.name}\n"
          f"    sha256: \"{sha256_of(final)}\"", flush=True)


if __name__ == "__main__":
    main()
