# local-comfyui

Локальная генерация картинок: ComfyUI с моделью SDXL в Docker на Windows-машине с видеокартой NVIDIA. Генерация идёт целиком на этой машине и без интернета; другие машины, например Mac, обращаются к ней по сети.

Инструмент общий и ничего не знает о конкретных играх. Всё, что задаёт проект (стиль, промпты, эталоны, позы, LoRA), лежит в репозитории самого проекта; сюда попадают только движок, общие базовые модели и типовые workflow. Проект подключается к инструменту своей папкой — раздел «Проекты».

## Машина

Windows 11 с видеокартой NVIDIA. Сейчас это RTX 3080 на 10 ГБ и 32 ГБ оперативной памяти. ComfyUI работает в Docker: на хосте нужны только Docker и git. Docker на macOS видеокарту не видит, поэтому на Mac сервис не запускаем.

База — **SDXL**: она помещается в 10 ГБ видеопамяти и для генерации, и для обучения LoRA, а её лицензия разрешает коммерческое использование. Flux на 10 ГБ только генерирует, и медленно, а его LoRA на такой карте не обучить.

### Подготовка Windows

1. **Драйвер NVIDIA** — последний Game Ready или Studio.
2. **Docker Desktop** — установщик с docker.com, а не из Microsoft Store: версия из Store не кладёт свои помощники в `PATH`. При установке оставь включённым WSL 2.
3. **Проверь, что Docker видит видеокарту:**
   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
   ```
   В таблице должны быть имя видеокарты и её память, например RTX 3080 и 10240MiB.
4. **Место на диске** с репозиторием (или там, куда указывает `MODELS_DIR`): 60–80 ГБ. Одна SDXL-модель весит около 7 ГБ, ControlNet — 2,5 ГБ, сам образ ComfyUI — около 10 ГБ.

Почему не portable-сборка ComfyUI: «Интеллектуальное управление приложениями» (Smart App Control) в Windows 11 блокирует неподписанные `.dll` и `.pyd` из Python-пакетов, и portable-сборка не запускается. Внутри Docker работает Linux, туда эта защита не дотягивается, и выключать её не нужно.

WSL 2 по умолчанию отдаёт контейнерам половину оперативной памяти (на 32 ГБ ComfyUI пишет в логе `total RAM 15914 MB`). Для генерации SDXL этого хватает; лимит поднимается через `%USERPROFILE%\.wslconfig`, если понадобится для обучения.

### Если Docker пишет `docker-credential-desktop: executable file not found in %PATH%`

Причина — кривая запись в `PATH`, а не Docker. Например, запись с лишней кавычкой, которую оставил установщик (у нас была `C:\Program Files\WireGuard"`). Docker написан на Go, а Go на Windows считает кавычку началом пути. Если пары у неё нет, все папки после неё сливаются в одну, и помощник Docker перестаёт находиться. `where.exe` при этом файл находит, потому что делит `PATH` просто по `;`.

Найти кавычку (команда ничего не выведет, если всё чисто):

```powershell
$env:Path -split ';' | Select-String '"'
```

Убрать её из системного `PATH`: PowerShell от имени администратора, старое значение сохраняется в `path-backup.txt`:

```powershell
$key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment'
$raw = (Get-Item $key).GetValue('Path', $null, 'DoNotExpandEnvironmentNames')
$raw | Out-File "$env:USERPROFILE\path-backup.txt"
Set-ItemProperty -Path $key -Name Path -Value $raw.Replace('"', '') -Type ExpandString
```

Если кавычка в пользовательском `PATH`, то же самое в обычном PowerShell с ключом `'HKCU:\Environment'`. После правки перезагрузись. Окно «Переменные среды» эту кавычку может не показывать, поэтому правим через реестр.

## Запуск

Образ — `docker/comfyui/Dockerfile`: Python 3.13, PyTorch с CUDA 13.0, ComfyUI закреплённой версии. Обновлять их вместе и записывать в [docs/LOG.md](docs/LOG.md), иначе одни и те же настройки начнут давать другие картинки. Сторонние расширения (custom nodes) в образ не ставим; свой пакет `local-comfyui` без узлов только показывает типовые графы. При старте контейнера `docker/comfyui/start.py` проверяет подключённый проект и только потом запускает ComfyUI.

1. Скопируй `.env.example` в `.env`. Базовые модели лягут в `data/models` внутри репозитория: папка в `.gitignore`, и всё хранится в одном месте, а не разбросано по дискам. Если на диске с репозиторием мало места, укажи в `.env` другую папку:
   ```
   MODELS_DIR=D:/comfyui-models
   ```
2. Собери образ и запусти. Первая сборка качает несколько гигабайт и идёт долго:
   ```powershell
   docker compose up -d --build
   ```
3. Открой http://localhost:8188. Пока проект не подключён, работает песочница `data/sandbox`, в логе (`docker compose logs comfyui`) строка `[start] project.yaml нет: подключена песочница`. Как подключить проект — в разделе «Проекты».

Поменял `.env` — пересоздай контейнер той же командой `docker compose up -d`: папки монтируются при создании контейнера, и простого перезапуска мало. Проверить, какие папки подключены: `docker compose config`, раздел `volumes`.

### Доступ с Mac

С другой машины в той же сети — `http://<IP Windows-машины>:8188`, и в браузере, и для скриптов через API. При первом подключении Windows спросит разрешение брандмауэра для Docker. Результаты при этом сохраняются в папку проекта на Windows и попадают на Mac через git проекта.

У ComfyUI нет авторизации: кто достучался до порта, тот может генерировать и читать результаты. Порт не пробрасываем в интернет; для доступа не из дома — VPN вроде Tailscale.

## Папки

Инструмент хранит у себя только то, что относится к машине, а не к проекту. Всё это в `data/`, папка в `.gitignore`:

| Папка | Что в ней |
| --- | --- |
| `data/models` (или `MODELS_DIR`) | базовые модели, общие для всех проектов: `checkpoints`, `controlnet`, `vae`, `upscale_models` |
| `data/user` | настройки интерфейса ComfyUI |
| `data/cache` | кеш sha256 моделей для проверки при старте |
| `data/sandbox` | песочница: подключается, когда проект не задан |

## Проекты

Всё проектное — входы, LoRA, графы, результаты — хранит сам проект в своём репозитории, в папке со строгой структурой. Инструмент подключает эту папку целиком и только одну: ComfyUI видит файлы подключённого проекта и ничьи больше, поэтому проекты не смешиваются.

### Как работаем

1. Клонируй репозиторий проекта на Windows-машину.
2. В `.env` инструмента укажи папку проекта для инструмента и пересоздай контейнер:
   ```
   PROJECT_DIR=E:/Projects/lootlords/art
   ```
   ```powershell
   docker compose up -d
   ```
   Это и есть переключение между проектами. В логе (`docker compose logs comfyui`) будет `[start] проект <имя> подключён` или список того, чего не хватает.
3. Генерируй в интерфейсе ComfyUI — с Mac по сети или на самой Windows-машине — или пачкой по списку промптов (раздел «Типовые графы и прогон»). Графы, сохранённые в интерфейсе (Workflow → Save), и результаты сразу оказываются в папке проекта.
4. Отобранное переложи в ассеты проекта, закоммить и запушь с Windows. На Mac — `git pull`.

### Структура папки

Заготовка — `template/`: скопируй её содержимое в репозиторий проекта, а папку назови как удобно (у Lootlords — `art/`).

| В папке проекта | Что в ней | Где видна в ComfyUI | В git проекта |
| --- | --- | --- | --- |
| `project.yaml` | конфиг, проверяется при старте | — | да |
| `input/` | что идёт на вход графам: скелеты поз, рамки иконок | `input` | да, картинки через Git LFS |
| `dataset/` | эталоны с подписями (`.txt` рядом с картинкой) для обучения LoRA | — | да, через Git LFS |
| `workflows/` | графы проекта | сохранённые workflow интерфейса | да |
| `prompts/` | списки промптов для пакетного прогона | — | да |
| `loras/` | LoRA проекта | `models/loras` | нет, из GitHub Releases проекта |
| `output/` | черновики генерации | `output` | нет |

`loras/` подключается поверх общей `models/loras`, поэтому в списке узла видны только LoRA подключённого проекта. Папок `loras/` и `output/` после клона нет (они в `.gitignore`), их создаёт Docker при старте.

### project.yaml

```yaml
format: 1
name: my-project
models:
  - path: checkpoints/sd_xl_base_1.0.safetensors
    sha256: "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b"
loras:
  - file: style-v1.safetensors
    url: https://github.com/<owner>/<repo>/releases/download/<tag>/style-v1.safetensors
    sha256: "<64 шестнадцатеричных символа>"
style:
  prompt: "flat colors, bold black outline"
  negative: "gradient"
```

| Поле | Что значит |
| --- | --- |
| `format` | версия формата, инструмент понимает `1` |
| `name` | имя проекта: латиница в нижнем регистре, цифры и дефис; обычно как имя репозитория |
| `models` | базовые модели, нужные проекту: путь внутри `MODELS_DIR` и sha256 из таблицы «Модели» |
| `loras` | LoRA проекта: файл в `loras/`, ссылка на Release и sha256 |
| `style` | стиль проекта: `prompt` и `negative` дописываются к каждому промпту пакетного прогона; необязательное |

sha256 пиши в кавычках: хеш из одних цифр YAML прочитает как число. Другие поля (холст, палитра, скелет, выгрузка) появятся в тех шагах [ROADMAP](docs/ROADMAP.md), которые начнут их читать.

Перед запуском ComfyUI контейнер проверяет формат, имя и лишние поля, а ещё есть ли каждая модель и LoRA и совпадает ли sha256. Если что-то не так, ComfyUI не запускается, а в логе — список проблем и что делать. Модель ставится по разделу «Модели». LoRA скачивается по ссылке из `project.yaml`; репозитории проектов приватные, поэтому качай из браузера, где ты вошёл в GitHub. Хеш файла на 7 ГБ считается около 40 с, потом берётся из кеша, пока у файла не поменяются размер или дата.

Без `project.yaml` проверка пропускается: так работает песочница.

Поправил `project.yaml` — `docker compose restart`: проверка идёт при старте контейнера, а `docker compose up -d` пересоздаёт его, только если поменялись `.env` или `compose.yaml`.

## Типовые графы и прогон

Типовые графы инструмента лежат в `docker/comfyui/local-comfyui/example_workflows/` и видны в интерфейсе: Workflow → Browse Templates → раздел **local-comfyui** внизу списка. Это пакет расширения без узлов, только с графами; он собирается в образ, поэтому графы обновляются вместе с инструментом. Открыл, подстроил модель, промпты и размер, сохранил (Workflow → Save) — копия ушла в `workflows/` проекта, а типовой граф остался как был.

| Граф | Что делает |
| --- | --- |
| `txt2img` | текст → картинка на SDXL без LoRA: кандидаты в эталоны, фоны |

### Пакетный прогон

Когда картинок нужно много — кандидаты в эталоны, варианты фонов, — список промптов кладут в `prompts/` проекта и прогоняют одной командой. Граф тот же, что `txt2img`.

```powershell
docker compose exec comfyui python /opt/tool/run.py prompts/references.yaml
```

С `--dry-run` скрипт только покажет итоговые промпты и настройки каждой картинки. Список:

```yaml
# Общие настройки; любой пункт может переопределить любую из них
checkpoint: sd_xl_base_1.0.safetensors
width: 1024
height: 1024
steps: 28
cfg: 6
sampler: dpmpp_2m
scheduler: karras
negative: "photo, realistic, 3d render, blurry, text, watermark"

items:
  - name: dwarf-warrior
    prompt: "dwarf warrior, full body, simple background"
    count: 4
    seed: 1000
  - name: dungeon-hall
    prompt: "dungeon hall with torches, wide view"
    width: 1344
    height: 768
```

| Поле | Что значит |
| --- | --- |
| `checkpoint` | базовая модель из `MODELS_DIR/checkpoints`; обязательно, чтобы смена умолчаний инструмента не меняла картинки проекта |
| `width`, `height` | размер, кратно 8; SDXL лучше всего около 1 Мп: 1024×1024, для широких фонов 1344×768. По умолчанию 1024×1024 |
| `steps`, `cfg`, `sampler`, `scheduler` | настройки сэмплера; по умолчанию 28, 6, `dpmpp_2m`, `karras` |
| `negative` | негативный промпт |
| `items[].name` | имя пункта: латиница в нижнем регистре, цифры и дефис; уникально в списке |
| `items[].prompt` | промпт |
| `items[].count` | сколько вариантов, по умолчанию 1 |
| `items[].seed` | seed первого варианта, следующие — `seed+1`, `seed+2`…; без него — случайный |

К `prompt` и `negative` каждого пункта дописывается `style` из `project.yaml`. Каждая картинка — отдельное задание со своим seed, и seed стоит в имени файла: `output/<имя списка>/<пункт>-s<seed>_00001_.png`. Понравилась картинка — впиши её seed в пункт, и с той же моделью и настройками она повторится пиксель в пиксель (проверено).

Ошибки в списке (неизвестное поле, неверный размер, нет модели, повтор имени) скрипт называет все сразу и ничего не генерирует. Если ComfyUI сам отклонил задание, например из-за неизвестного сэмплера, скрипт передаёт его объяснение.

## Модели

| Что | Файл в `MODELS_DIR` | Размер, байт | sha256 | Откуда | Лицензия |
| --- | --- | --- | --- | --- | --- |
| Базовая SDXL | `checkpoints/sd_xl_base_1.0.safetensors` | 6938078334 | `31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b` | [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors) | CreativeML Open RAIL++-M |
| ControlNet позы | `controlnet/xinsir-openpose-sdxl.safetensors` | 2502139104 | `b8524e557a7df60d081f5d4a0eb109967d107df217943bf88c2d99b9ebcc06c5` | [xinsir/controlnet-openpose-sdxl-1.0](https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors), файл переименовываем: в оригинале безликое имя | Apache 2.0 |

sha256 нужен проектам: его пишут в `project.yaml`, и контейнер сверяет с ним файл при старте.

Скачать сразу на место можно `curl.exe`, он есть в Windows 10 и 11. PowerShell из корня репозитория, пути — для `MODELS_DIR` по умолчанию; папки появляются при первом запуске контейнера:

```powershell
curl.exe -L -o data\models\checkpoints\sd_xl_base_1.0.safetensors https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors
curl.exe -L -o data\models\controlnet\xinsir-openpose-sdxl.safetensors https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors
```

Размер и sha256 скачанного файла сверь с таблицей: недокачанный файл ComfyUI не загрузит.

```powershell
Get-ChildItem data\models -Recurse -Filter *.safetensors | Select-Object FullName, Length
```

```powershell
Get-FileHash data\models\checkpoints\sd_xl_base_1.0.safetensors
```

Перезапускать ComfyUI не нужно: в интерфейсе нажми `R`, и новые файлы появятся в списках. Мультяшное дообучение SDXL с Civitai можно положить рядом с базовой, если в его карточке разрешено коммерческое использование.

Качаем только `.safetensors`: файлы `.ckpt` и `.pt` при загрузке могут выполнить чужой код.

У ControlNet от xinsir есть особенность: его учили на скелетах с толстыми линиями. Со стандартной тонкой отрисовкой OpenPose поза держится хуже, поэтому скелеты поз рисуем толстыми линиями.

## Проверка

В ComfyUI: Workflow → Browse Templates → «SDXL Simple». Шаблон собран из двух ступеней, base и refiner, и просит ещё `sd_xl_refiner_1.0.safetensors` — его не качаем: в узле `Load Checkpoint - REFINER` выбери `sd_xl_base_1.0.safetensors`, и предупреждение пропадёт. Стартовый workflow, который ComfyUI открывает сам, собран не на SDXL и тоже жалуется на отсутствующие модели — это нормально, просто открой шаблон.

Задай 1024×1024, 25–30 шагов, CFG 5–7, сэмплер `dpmpp_2m`, планировщик `karras`. Промпт, например, `cartoon fantasy dwarf warrior, full body, bold black outline, flat colors, simple background`, негативный — `photo, realistic, 3d render, blurry, text, watermark`. Картинка появилась в `output` подключённой папки (без проекта — `data/sandbox/output`) — генерация работает.

На RTX 3080 первая картинка 1024×1024 после запуска занимает около 30 с (модель грузится в видеопамять), следующие — около 10 с, примерно 3,3 шага в секунду.

Если ComfyUI не видит модели:

1. `docker compose logs comfyui` — если подключён проект, проверка при старте пишет, какой модели не хватает или чья версия не та.
2. `docker compose config` — в `volumes` у `/opt/comfyui/models` та папка, где лежат модели: `data/models` или твой `MODELS_DIR`.
3. `docker compose exec comfyui ls -la models/checkpoints models/controlnet` — файлы на месте и нужного размера. Если нет, контейнер создан до правки `.env`: `docker compose up -d`.
4. Узел ждёт другое имя файла (как refiner в шаблоне выше) — выбери свой файл в узле.

## Команды

| Команда | Что получим |
| --- | --- |
| `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` | проверка, что Docker видит видеокарту: таблица с её именем и памятью |
| `docker compose up -d --build` | собранный образ ComfyUI и сервис на http://localhost:8188; первая сборка долгая |
| `docker compose up -d` | контейнер пересоздан, если поменялся `.env` (например, подключён другой проект) или `compose.yaml` |
| `docker compose logs -f comfyui` | логи ComfyUI и проверки проекта при старте |
| `docker compose restart` | проект проверен заново после правки `project.yaml` |
| `docker compose exec comfyui python /opt/tool/run.py prompts/<список>.yaml` | картинки по списку промптов в `output/<список>/` проекта; с `--dry-run` — только список заданий |
| `docker compose stop` | ComfyUI остановлен, видеопамять свободна; модели и файлы проектов остаются на диске |
| `curl.exe http://localhost:8188/object_info/CheckpointLoaderSimple` | какие базовые модели видит ComfyUI |

## Документация

- [docs/ROADMAP.md](docs/ROADMAP.md) — что сделано и что дальше
- [docs/LOG.md](docs/LOG.md) — журнал изменений и принятых решений
- [CLAUDE.md](CLAUDE.md) — правила работы для Claude Code
