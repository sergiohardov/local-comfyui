# local-comfyui

Генерация 2D-графики и обучение LoRA: ComfyUI с SDXL в Docker на Windows-машине с видеокартой NVIDIA. Сервис для нескольких проектов: с Mac им пользуются через визуальный интерфейс ComfyUI и HTTP API на одном адресе. Инструмент не знает о проектах: графы, узлы, промпты, скелеты и эталоны живут в репозитории проекта и приходят по API; на Windows — только базовые модели, библиотека LoRA и кэш присланного. Почему всё устроено так — в [docs/LOG.md](docs/LOG.md).

Что умеет:

- **текст → картинка** на SDXL с LoRA из библиотеки и позой по скелету (ControlNet) — общие шаблоны;
- **графы проектов** через штатное API ComfyUI;
- **узлы проектов** — проект присылает свои узлы по API, без перезапуска;
- **обучение LoRA** и библиотека LoRA;
- **клиент** для другой машины — человеку, Claude и скриптам проектов.

Задача проекта — граф ComfyUI, который проект хранит у себя и отправляет в штатное API ComfyUI (раздел «Графы через API»); новая задача — новый граф и, если нужно, узлы проекта, а не код в инструменте. В инструмент блок попадает, только когда он понадобился второму проекту. Пример проекта — [Lootlords](https://github.com/sergiohardov/lootlords): персонаж из частей на костях, снаряжение, иконки — в его `art/comfy` и `docs/ART.md`.

## Машина

Windows 11, NVIDIA RTX 3080 на 10 ГБ, 32 ГБ памяти. На хосте только Docker и git; Python, ComfyUI и PyTorch — в образах. Portable-сборку ComfyUI не используем: её блокирует Smart App Control. База — SDXL: в 10 ГБ помещаются и генерация, и обучение LoRA.

1. **Драйвер NVIDIA** — последний Game Ready или Studio.
2. **Docker Desktop** с docker.com (не из Microsoft Store), с WSL 2.
3. **Docker видит видеокарту:** `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` показывает её имя и память.
4. **Место:** 60–80 ГБ под образы и модели.

WSL 2 отдаёт контейнерам половину оперативной памяти — для генерации и обучения хватает. Образы не пересобирай во время генерации: вместе они упираются в эту память.

**Если Docker пишет `docker-credential-desktop: executable file not found in %PATH%`** — в `PATH` запись с лишней кавычкой (например, `C:\Program Files\WireGuard"`). Найти: `$env:Path -split ';' | Select-String '"'`. Убрать из системного `PATH` (PowerShell от администратора, старое значение — в `path-backup.txt`), потом перезагрузиться; для пользовательского `PATH` — ключ `HKCU:\Environment`:

```powershell
$key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
$raw = (Get-Item $key).GetValue('Path', $null, 'DoNotExpandEnvironmentNames')
$raw | Out-File "$env:USERPROFILE\path-backup.txt"
Set-ItemProperty -Path $key -Name Path -Value $raw.Replace('"', '') -Type ExpandString
```

## Запуск

1. Скопируй `.env.example` в `.env`. Модели и библиотека LoRA — в `data/models`; другая папка — `MODELS_DIR=D:/comfyui-models` в `.env`.
2. Положи модели (раздел «Модели»).
3. `docker compose up -d --build` — первая сборка качает несколько гигабайт.
4. http://localhost:8188 — интерфейс; `curl.exe http://localhost:8188/tool/status` отвечает `"state": "idle"` и `"trainer": "available"`.

Поменял `.env` — `docker compose up -d`: папки монтируются при создании контейнера. Версии ComfyUI, PyTorch и CUDA закреплены в `docker/comfyui/Dockerfile`, обновляются вместе и с записью в журнал. Сторонних расширений нет: API, загрузчик узлов проектов и клиент — свой пакет `docker/comfyui/local-comfyui`.

С другой машины в сети — `http://<IP Windows>:8188`. Авторизации нет: порт не пробрасываем в интернет, извне — через VPN вроде Tailscale.

## Графы через API

Сценарий — граф ComfyUI в формате API: собран в интерфейсе, лежит в репозитории проекта (у Lootlords — `art/comfy/workflows/`). Клиент подставляет в него свои значения и отправляет в штатное API ComfyUI на том же адресе; инструмент для этого не нужен, кроме узлов проекта внутри графа (раздел «Узлы проектов»). Всё ниже одной командой делает клиент инструмента (раздел «Клиент»); здесь — как это устроено в штатном API.

1. **Граф.** Собери в интерфейсе или открой шаблон (раздел «Типовые графы»), проверь кнопкой Run, затем меню (логотип слева вверху) → File → Export (API). В файле — `{"<id узла>": {"class_type": "...", "inputs": {...}, "_meta": {"title": "..."}}}`: ссылка на выход другого узла — `["<id>", <номер выхода>]`, остальное — значения виджетов. Id узлов постоянны, а `_meta.title` — удобная метка, по которой клиент находит, что менять.
2. **Входные картинки** — `POST /upload/image`, в граф — `subfolder/name` из ответа.
3. **Значения** — клиент меняет `inputs` по id: промпт, seed, LoRA, точки позы, имя загруженной картинки.
4. **Отправка** — `POST /prompt`, дальше опрос `GET /history/{prompt_id}` и файлы через `GET /view`.

| Запрос | Что делает |
| --- | --- |
| `POST /upload/image` | `multipart/form-data`: `image` — файл, `subfolder` — папка во входных файлах, `overwrite=true` — заменить. Ответ `{"name", "subfolder", "type": "input"}` |
| `POST /prompt` | `{"prompt": <граф>, "client_id": "<имя клиента>"}` → `{"prompt_id", "number", "node_errors": {}}`. Неверный граф — `400` и `node_errors` по узлам (например, LoRA не из библиотеки — `value_not_in_list`); во время обучения — `423` |
| `GET /history/{prompt_id}` | `{}`, пока не выполнено; потом `status.status_str` — `success` или `error` — и `outputs.<id узла>`: `images` — файлы `{filename, subfolder, type}`, узлы проекта могут добавить свои поля. Ошибка выполнения — в `status.messages`, `execution_error`, `exception_message` |
| `GET /view?filename=&subfolder=&type=output` | файл результата: PNG, GIF, `rig.json` |
| `GET /queue` | `queue_running` и `queue_pending` — где задание |
| `POST /queue` с `{"delete": ["<prompt_id>"]}`, `POST /interrupt` | убрать из очереди; прервать текущее |
| `GET /object_info/<узел>` | входы узла и допустимые значения: базовые модели — `CheckpointLoaderSimple`, LoRA — `LoraLoader` |

Имена моделей и LoRA в графе — как в библиотеке: `проект/имя.safetensors` (список — `GET /tool/loras`). Тот же граф с тем же seed повторяет картинку пиксель в пиксель. Прогресс по шагам, если нужен, — веб-сокет `ws://<адрес>/ws?clientId=<client_id>` (события `progress`, `executing`); хватает и опроса `/history`.

Пример с Мака — персонаж по скелету по шаблону `txt2img-pose` (`jq` есть в macOS 15, иначе `brew install jq`):

```bash
HOST=http://192.168.1.50:8188
curl -s -o pose.api.json $HOST/tool/workflows/txt2img-pose
curl -s -F image=@skeleton.png -F subfolder=myproject -F overwrite=true $HOST/upload/image
# → {"name": "skeleton.png", "subfolder": "myproject", "type": "input"}
jq --arg prompt "a dwarf warrior, full body, simple background" --argjson seed 3 \
   '.["2"].inputs.text = $prompt | .["14"].inputs.image = "myproject/skeleton.png" | .["5"].inputs.seed = $seed
    | {prompt: ., client_id: "myproject"}' pose.api.json > job.json
curl -s -X POST $HOST/prompt -H "Content-Type: application/json" -d @job.json
# → {"prompt_id": "12cb…", "number": 3, "node_errors": {}}
curl -s $HOST/history/12cb…
# → {} пока идёт; потом status.status_str "success", outputs."7".images: [{"filename": "image_00008_.png", "subfolder": "txt2img", "type": "output"}]
curl -o dwarf.png "$HOST/view?filename=image_00008_.png&subfolder=txt2img&type=output"
```

Id `2`, `14`, `5`, `7` — узлы Prompt, Skeleton, KSampler и Save Image этого шаблона. Скелет — картинка OpenPose с толстыми линиями, как учили ControlNet от xinsir; рисует её проект. LoRA стиля — узел LoRA (`lora_name`: `проект/имя.safetensors`).

## Клиент

`client.mjs` — один файл на Node.js 18+ без зависимостей: им с другой машины работают человек, Claude и скрипты проектов. Сервер отдаёт его сам, всегда своей версии; проект хранит копию у себя, `update` скачивает свежую.

```bash
curl -o client.mjs http://192.168.1.50:8188/tool/client.mjs
export COMFY_URL=http://192.168.1.50:8188
node client.mjs help
```

| Команда | Что делает |
| --- | --- |
| `status`, `logs [comfyui\|trainer]`, `restart`, `cleanup --older-days N` | состояние, хвост лога, перезапуск ComfyUI, очистка кэша на сервере |
| `push-nodes <проект> <папка>`, `nodes`, `rm-nodes <проект>` | узлы проекта (раздел «Узлы проектов») |
| `pull <имя>` | граф последнего запуска из интерфейса → `<имя>.ui.json` и `<имя>.api.json` |
| `push <граф.ui.json>` | граф в боковую панель Workflows интерфейса |
| `workflows`, `workflow <имя>` | общие графы инструмента в формате API |
| `check <граф.api.json>` | есть ли на сервере узлы, модели и LoRA графа |
| `run <граф.api.json>` | загрузить входы (`--upload Узел=файл`), загрузить папку и подставить её во вход (`--upload-folder Узел.вход=папка`, ляжет в `<проект>/<имя папки>`), подставить значения (`--set Узел.вход=значение`, `--set-file Узел.вход=файл`), дождаться, скачать файлы и `outputs.json` в `--out` |
| `train <проект/имя> <папка>` | проверить подписи (`--trigger` — с какого слова начинаются), собрать zip, обучить и дождаться |
| `lora list\|push\|pull\|rm` | библиотека LoRA |
| `update` | скачать с сервера свежую версию клиента |

Узел в `--set` и `--upload` — id или заголовок (`_meta.title`); значение принимает тип того, что стоит в графе: промпт остаётся текстом, seed — числом.

```bash
node client.mjs workflow txt2img --out txt2img.api.json
node client.mjs run txt2img.api.json --set Prompt.text="a red apple on a table" --set KSampler.seed=7 --out apple
node client.mjs workflow txt2img-pose --out pose.api.json
node client.mjs run pose.api.json --upload Skeleton=skeleton.png --set Prompt.text="a dwarf warrior, full body" --project myproject --out dwarf
```

`check` проверяет граф как есть, до подстановок: имя картинки, которую `run` загрузит сам, он пока не найдёт.

## API инструмента

Своё у инструмента — состояние, библиотека LoRA, обучение, узлы проектов, логи и кэш. Ответы — JSON в UTF-8, ошибки — `{"errors": [...]}`, все сразу. Пути работают и с префиксом `/api`.

| Запрос | Что делает |
| --- | --- |
| `GET /tool/status` | `idle`, `generating` или `training`; текущее — `prompt_id` с шагом или обучение; очередь, видеопамять, базовые модели |
| `GET /tool/loras`, `GET` / `DELETE /tool/loras/{имя}`, `POST /tool/loras` | библиотека LoRA: список, скачать, удалить, загрузить |
| `POST /tool/train`, `GET /tool/jobs/{job_id}` | обучить LoRA, ход обучения |
| `GET /tool/nodes`, `POST` / `DELETE /tool/nodes/{проект}` | узлы проектов: список, загрузить (`multipart`, поле `package` — zip пакета), убрать |
| `GET /tool/workflows`, `GET /tool/workflows/{имя}` | общие графы в формате API |
| `GET /tool/logs?service=comfyui\|trainer&lines=200` | хвост лога ComfyUI или обучения (`&job=` — какого; по умолчанию последнего), текстом |
| `POST /tool/cleanup` | `{"older_than_days": 7, "folders": ["input", "output"]}` — удалить из кэша файлы старше N дней (0 — всё) |
| `POST /tool/restart` | перезапустить ComfyUI, около 20 с |
| `GET /tool/client.mjs` | клиент |

Перезапуск и очистка при занятой очереди отвечают `409`. Контейнеры поднимаются вместе с Docker (`restart: unless-stopped`), так что перезапуск — это выход процесса ComfyUI, а Docker запускает его снова.

Пока идёт обучение, штатный `POST /prompt` отвечает `423` — и клиенту, и интерфейсу: генерация и обучение вместе не помещаются в видеопамять.

### Библиотека LoRA

`data/models/loras`, имена `проект/имя`, у обученных — с версией `-vN`. Видны и в интерфейсе (LoraLoader). Загрузка проверяет формат safetensors и отдаёт sha256; есть такая — `409`, заменить — `?overwrite=true`.

```bash
curl -F name=lootlords/style-v2 -F file=@style-v2.safetensors http://192.168.1.50:8188/tool/loras
curl -o style-v2.safetensors http://192.168.1.50:8188/tool/loras/lootlords/style-v2
```

## Обучение LoRA

```bash
curl -F name=lootlords/style -F 'params={"steps": 1500}' -F dataset=@style-refs.zip http://192.168.1.50:8188/tool/train
```

Поля по порядку: `name` (`проект/имя`, версия `-vN` добавится сама), `params` (JSON, необязательно), `dataset` — zip с картинками и `.txt`-подписями того же имени. Подпись описывает содержимое и начинается со слова-триггера, слов о стиле в ней нет: `mystyle, a dwarf warrior with an axe`.

| `params` | По умолчанию |
| --- | --- |
| `checkpoint` | `sd_xl_base_1.0.safetensors` — LoRA работает только с этой базой |
| `steps` | 1500 |
| `rank`, `alpha` | 16, `rank / 2` |
| `learning_rate` | 0.0001 |
| `resolution` | 1024 |
| `repeats` | 10 |
| `save_every` | 250 — промежуточные сохранения для выбора точки |
| `seed` | 42 |

Тренер — kohya sd-scripts в своём образе `docker/trainer`, только во внутренней сети Docker; настройки под 10 ГБ ставит он сам. Обучение стартует при пустой очереди (иначе `409`), и до конца любая генерация получает `423`. Прогресс — в `GET /tool/status` и `GET /tool/jobs/{job_id}`; готовое задание — имя LoRA, sha256 и промежуточные сохранения. Задания тренер помнит до своего перезапуска; журнал остаётся в `data/jobs/<job_id>/train.log`. Точку выбирают генерацией с одним seed и разными сохранениями. RTX 3080: около 1,3 с на шаг, 8,2 ГБ видеопамяти.

**Видеопамять.** Тренеру нужно 8,5 ГБ свободных, иначе `503` «видеокарту держит что-то кроме ComfyUI»: на Windows не должно быть программ, которые её держат, — браузеров, Discord, Telegram, VS Code, GitHub Desktop, оверлея NVIDIA App. Сколько занято — `vram` в `status`.

**Оперативная память.** Как только тренер принял задание, инструмент сам перезапускает ComfyUI: после множества генераций тот держит до 10 ГБ оперативной памяти (`/free` её не возвращает), а вместе с тренером (до 12,5 ГБ) это больше, чем WSL отдаёт контейнерам, — sd-scripts убивался бы на загрузке модели. Блокировка генерации переживает перезапуск: её источник — тренер.

## Папки

| Папка | Что в ней |
| --- | --- |
| `data/models` (или `MODELS_DIR`) | базовые модели и библиотека LoRA `loras/<проект>/` |
| `data/input` | картинки из интерфейса и загруженные входы (`/upload/image`) |
| `data/output` | результаты генераций |
| `data/user` | настройки интерфейса и сохранённые графы |
| `data/project_nodes` | присланные пакеты узлов проектов |
| `data/jobs` | записи обучений |

Вся `data/` — в `.gitignore`. `data/input`, `data/output` и `data/project_nodes` — кэш: проект хранит своё у себя и может прислать снова (`cleanup`).

## Узлы проектов

Узлы, нужные одному проекту, живут в его репозитории стандартным пакетом узлов ComfyUI: папка с `__init__.py`, где `NODE_CLASS_MAPPINGS` (и, если надо, `NODE_DISPLAY_NAME_MAPPINGS`); рядом — свои модули, импорт относительный. Проект присылает пакет — `node client.mjs push-nodes lootlords art/comfy/nodes`:

- узлы регистрируются как `<проект>.<узел>` (в интерфейсе — «Название (проект)») без перезапуска ComfyUI; в открытом интерфейсе — обновить страницу;
- тот же граф после новой загрузки считает узлы проекта и всё после них заново, а модели берёт из кэша;
- пакет с ошибкой не регистрируется, остаётся прежний; в ответе — конец traceback;
- пакет лежит в `data/project_nodes/<проект>/` и регистрируется заново при старте; это кэш — проект может прислать его снова.

Пример — пакет Lootlords, `art/comfy/nodes` в его репозитории. Ограничения: только классический формат (`NODE_CLASS_MAPPINGS`, не `comfy_entrypoint`); библиотеки — те, что в образе (numpy, Pillow, scipy, torch), новая — правка инструмента; состояние в `self` между запусками не храни. Код проекта выполняется на Windows внутри контейнера, поэтому порт — только для домашней сети.

## Типовые графы

Меню (логотип слева вверху) → Browse Templates → раздел **local-comfyui**: `txt2img`, `txt2img-lora` (LoRA из библиотеки), `txt2img-pose` (поза по картинке скелета OpenPose). Те же графы в формате API — `GET /tool/workflows/{имя}`, `node client.mjs workflow <имя>`. Сохранённая копия уходит в `data/user`, в проект — через меню → File → Export или `client.mjs pull`, для отправки в `/prompt` — File → Export (API).

## Модели

Только `.safetensors`: `.ckpt` и `.pt` при загрузке могут выполнить чужой код.

| Что | Файл в `MODELS_DIR` | Размер, байт | sha256 | Откуда | Лицензия |
| --- | --- | --- | --- | --- | --- |
| Базовая SDXL | `checkpoints/sd_xl_base_1.0.safetensors` | 6938078334 | `31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b` | [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors) | CreativeML Open RAIL++-M |
| ControlNet позы | `controlnet/xinsir-openpose-sdxl.safetensors` | 2502139104 | `b8524e557a7df60d081f5d4a0eb109967d107df217943bf88c2d99b9ebcc06c5` | [xinsir/controlnet-openpose-sdxl-1.0](https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors), переименован | Apache 2.0 |
| Удаление фона | `background_removal/birefnet.safetensors` | 444473596 | `9ab37426bf4de0567af6b5d21b16151357149139362e6e8992021b8ce356a154` | [Comfy-Org/BiRefNet](https://huggingface.co/Comfy-Org/BiRefNet/resolve/main/background_removal/birefnet.safetensors) | MIT |

```powershell
curl.exe -L -o data\models\checkpoints\sd_xl_base_1.0.safetensors https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors
curl.exe -L -o data\models\controlnet\xinsir-openpose-sdxl.safetensors https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors
curl.exe -L -o data\models\background_removal\birefnet.safetensors https://huggingface.co/Comfy-Org/BiRefNet/resolve/main/background_removal/birefnet.safetensors
Get-FileHash data\models\checkpoints\sd_xl_base_1.0.safetensors
```

Размер и sha256 сверь с таблицей. Новые файлы в интерфейсе появляются по клавише `R`.

## Проверка

Меню → Browse Templates → **local-comfyui** → `txt2img`, кнопка Run: картинка в узле Save Image — работает. С другой машины — граф в `POST /prompt` (раздел «Графы через API»). RTX 3080: первая картинка 1024×1024 — около 30 с, следующие — около 10 с.

Если ComfyUI не видит модели: `GET /tool/status` (список `checkpoints`), `docker compose config` (куда смонтирован `MODELS_DIR`), `docker compose exec comfyui ls -la models/checkpoints` (файлы на месте). Контейнер создан до правки `.env` — `docker compose up -d`.

## Команды

| Команда | Что получим |
| --- | --- |
| `docker compose up -d --build` | собранные образы ComfyUI и тренера, инструмент на http://localhost:8188 |
| `docker compose up -d` | пересозданные контейнеры после правки `.env` или `compose.yaml` |
| `curl.exe http://localhost:8188/tool/status` | что делает инструмент |
| `docker compose logs -f comfyui` | логи ComfyUI и API |
| `docker compose logs -f trainer` | логи тренера |
| `docker compose stop` | инструмент остановлен, видеопамять свободна |

## Документация

- [docs/ROADMAP.md](docs/ROADMAP.md) — что сделано и что дальше
- [docs/LOG.md](docs/LOG.md) — журнал: что сделано, какие решения приняты и почему
- [CLAUDE.md](CLAUDE.md) — правила работы для Claude Code
