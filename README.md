# local-comfyui

Локальная генерация картинок: ComfyUI с моделью SDXL в Docker на Windows-машине с видеокартой NVIDIA. Генерация идёт целиком на этой машине и без интернета; другие машины, например Mac, обращаются к ней по сети.

Инструмент общий и ничего не знает о конкретных играх. Всё, что задаёт проект (стиль, промпты, эталоны, позы, LoRA), лежит в репозитории самого проекта; сюда попадают только движок, общие базовые модели и типовые workflow. Как проекты разделены на диске — в разделе «Проекты».

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
4. **Место на диске** с репозиторием (или там, куда указывает `DATA_DIR`): 60–80 ГБ. Одна SDXL-модель весит около 7 ГБ, ControlNet — 2,5 ГБ, сам образ ComfyUI — около 10 ГБ.

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

Образ — `docker/comfyui/Dockerfile`: Python 3.13, PyTorch с CUDA 13.0, ComfyUI закреплённой версии. Обновлять их вместе и записывать в [docs/LOG.md](docs/LOG.md), иначе одни и те же настройки начнут давать другие картинки. Расширения (custom nodes) в образ пока не ставим.

1. Скопируй `.env.example` в `.env`. Модели и данные проектов лягут в `data/` внутри репозитория: папка в `.gitignore`, и всё хранится в одном месте, а не разбросано по дискам. Если на диске с репозиторием мало места, укажи в `.env` другую папку:
   ```
   DATA_DIR=D:/comfyui-data
   ```
2. Собери образ и запусти. Первая сборка качает несколько гигабайт и идёт долго:
   ```powershell
   docker compose up -d --build
   ```
3. Открой http://localhost:8188.

Поменял `.env` — пересоздай контейнер той же командой `docker compose up -d`: папки монтируются при создании контейнера, и простого перезапуска мало. Проверить, куда смотрит контейнер: `docker compose config` показывает `DATA_DIR` в `volumes`.

### Доступ с Mac

С другой машины в той же сети — `http://<IP Windows-машины>:8188`, и в браузере, и для скриптов через API. При первом подключении Windows спросит разрешение брандмауэра для Docker.

У ComfyUI нет авторизации: кто достучался до порта, тот может генерировать и читать результаты. Порт не пробрасываем в интернет; для доступа не из дома — VPN вроде Tailscale.

## Папки

Внутри `DATA_DIR` при первом запуске появятся:

| Папка | Что в ней |
| --- | --- |
| `models/checkpoints` | базовые модели SDXL, общие для всех проектов |
| `models/controlnet` | ControlNet, общие |
| `models/loras` | LoRA, по папке на проект |
| `models/vae`, `models/upscale_models` | VAE и апскейлеры, если понадобятся |
| `input` | картинки на вход: скелеты поз, эталоны |
| `output` | всё, что сгенерировано |
| `user` | сохранённые workflow и настройки интерфейса ComfyUI |

## Проекты

Базовые модели общие, а всё проектное — в подпапке с именем проекта (латиница, нижний регистр, как имя репозитория):

```
DATA_DIR/
  models/checkpoints/        общие базовые модели
  models/controlnet/         общие
  models/loras/<проект>/     LoRA проекта
  input/<проект>/            скелеты поз и картинки на вход
  output/<проект>/           генерации проекта
```

Как ComfyUI с этим работает (проверено на v0.42.1):

- **LoRA:** подпапки видны сами, в списке узла файл называется `<проект>/<файл>.safetensors`.
- **Результаты:** префикс `<проект>/<имя>` в узле Save Image сохраняет в `output/<проект>/`.
- **Входы:** узел Load Image принимает путь `<проект>/<файл>.png` из workflow, но в выпадающем списке интерфейса файлы из подпапок не показывает. Для скриптов через API это не мешает; руками в интерфейсе картинку проще загрузить кнопкой upload.

Изоляцию держит соглашение: workflow проекта всегда ставят имя проекта в пути и префиксы. Исходники проекта — стайлгайд, промпты, эталоны, позы, настройки — хранятся в git-репозитории проекта, а здесь, на диске, только рабочие копии и результаты.

## Модели

| Что | Файл в `DATA_DIR` | Размер, байт | Откуда | Лицензия |
| --- | --- | --- | --- | --- |
| Базовая SDXL | `models/checkpoints/sd_xl_base_1.0.safetensors` | 6938078334 | [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors) | CreativeML Open RAIL++-M |
| ControlNet позы | `models/controlnet/xinsir-openpose-sdxl.safetensors` | 2502139104 | [xinsir/controlnet-openpose-sdxl-1.0](https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors), файл переименовываем: в оригинале безликое имя | Apache 2.0 |

Скачать сразу на место можно `curl.exe`, он есть в Windows 10 и 11. PowerShell из корня репозитория, пути — для `DATA_DIR` по умолчанию; папки `models/...` появляются при первом запуске контейнера:

```powershell
curl.exe -L -o data\models\checkpoints\sd_xl_base_1.0.safetensors https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors
curl.exe -L -o data\models\controlnet\xinsir-openpose-sdxl.safetensors https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors
```

Размер скачанного файла сверь с таблицей: недокачанный файл ComfyUI не загрузит.

```powershell
Get-ChildItem data\models -Recurse -Filter *.safetensors | Select-Object FullName, Length
```

Перезапускать ComfyUI не нужно: в интерфейсе нажми `R`, и новые файлы появятся в списках. Мультяшное дообучение SDXL с Civitai можно положить рядом с базовой, если в его карточке разрешено коммерческое использование.

Качаем только `.safetensors`: файлы `.ckpt` и `.pt` при загрузке могут выполнить чужой код.

У ControlNet от xinsir есть особенность: его учили на скелетах с толстыми линиями. Со стандартной тонкой отрисовкой OpenPose поза держится хуже, поэтому скелеты поз рисуем толстыми линиями.

## Проверка

В ComfyUI: Workflow → Browse Templates → «SDXL Simple». Шаблон собран из двух ступеней, base и refiner, и просит ещё `sd_xl_refiner_1.0.safetensors` — его не качаем: в узле `Load Checkpoint - REFINER` выбери `sd_xl_base_1.0.safetensors`, и предупреждение пропадёт. Стартовый workflow, который ComfyUI открывает сам, собран не на SDXL и тоже жалуется на отсутствующие модели — это нормально, просто открой шаблон.

Задай 1024×1024, 25–30 шагов, CFG 5–7, сэмплер `dpmpp_2m`, планировщик `karras`. Промпт, например, `cartoon fantasy dwarf warrior, full body, bold black outline, flat colors, simple background`, негативный — `photo, realistic, 3d render, blurry, text, watermark`. Картинка появилась в `output` — генерация работает.

На RTX 3080 первая картинка 1024×1024 после запуска занимает около 30 с (модель грузится в видеопамять), следующие — около 10 с, примерно 3,3 шага в секунду.

Если ComfyUI не видит модели:

1. `docker compose config` — в `volumes` та папка, где лежат модели: `data/` репозитория или твой `DATA_DIR`.
2. `docker compose exec comfyui ls -la models/checkpoints models/controlnet` — файлы на месте и нужного размера. Если нет, контейнер создан до правки `.env`: `docker compose up -d`.
3. Узел ждёт другое имя файла (как refiner в шаблоне выше) — выбери свой файл в узле.

## Команды

| Команда | Что получим |
| --- | --- |
| `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` | проверка, что Docker видит видеокарту: таблица с её именем и памятью |
| `docker compose up -d --build` | собранный образ ComfyUI и сервис на http://localhost:8188; первая сборка долгая |
| `docker compose up -d` | контейнер пересоздан, если поменялся `.env` или `compose.yaml` |
| `docker compose logs -f comfyui` | логи ComfyUI |
| `docker compose stop` | ComfyUI остановлен, видеопамять свободна; модели и результаты в `DATA_DIR` остаются |
| `curl.exe http://localhost:8188/object_info/CheckpointLoaderSimple` | какие базовые модели видит ComfyUI |

## Документация

- [docs/ROADMAP.md](docs/ROADMAP.md) — что сделано и что дальше
- [docs/LOG.md](docs/LOG.md) — журнал изменений и принятых решений
- [CLAUDE.md](CLAUDE.md) — правила работы для Claude Code
