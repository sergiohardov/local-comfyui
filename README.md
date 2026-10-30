# local-comfyui

Генерация 2D-графики и обучение LoRA: ComfyUI с SDXL в Docker на Windows-машине с видеокартой NVIDIA. Сервис для нескольких проектов: с Mac им пользуются через визуальный интерфейс ComfyUI и HTTP API на одном адресе. Инструмент не знает о проектах: стиль, промпты, скелеты и LoRA живут в репозитории проекта и приходят в запросах; на Windows — только базовые модели и библиотека LoRA. Почему всё устроено так — в [docs/LOG.md](docs/LOG.md).

Что умеет:

- **персонаж из частей** для анимации на костях: база по скелету телосложения, нарезка на части, превью анимации;
- **снаряжение** — доспех, закрытый шлем, оружие — генерируется на базе и заменяет её части; переносится между телосложениями;
- **рамки и иконки** предметов и навыков;
- **пиксель-арт** — предметы и здания в стиле набора эталонов;
- **обучение LoRA** стиля и библиотека LoRA.

Задача проекта — граф ComfyUI, который проект хранит у себя и отправляет в штатное API ComfyUI (раздел «Графы через API»); новая задача — новый граф, а не код в инструменте.

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

Поменял `.env` — `docker compose up -d`: папки монтируются при создании контейнера. Версии ComfyUI, PyTorch и CUDA закреплены в `docker/comfyui/Dockerfile`, обновляются вместе и с записью в журнал. Сторонних расширений нет: API и узлы — свой пакет `docker/comfyui/local-comfyui`.

С другой машины в сети — `http://<IP Windows>:8188`. Авторизации нет: порт не пробрасываем в интернет, извне — через VPN вроде Tailscale.

## Графы через API

Сценарий — граф ComfyUI в формате API: собран в интерфейсе, лежит в репозитории проекта (у Lootlords — `art/workflows/`). Клиент подставляет в него свои значения и отправляет в штатное API ComfyUI на том же адресе; инструмент для этого не нужен, кроме своих узлов внутри графа (раздел «Узлы»).

1. **Граф.** Собери в интерфейсе или открой шаблон (раздел «Типовые графы»), проверь кнопкой Run, затем меню (логотип слева вверху) → File → Export (API). В файле — `{"<id узла>": {"class_type": "...", "inputs": {...}, "_meta": {"title": "..."}}}`: ссылка на выход другого узла — `["<id>", <номер выхода>]`, остальное — значения виджетов. Id узлов постоянны, а `_meta.title` — удобная метка, по которой клиент находит, что менять.
2. **Входные картинки** — `POST /upload/image`, в граф — `subfolder/name` из ответа.
3. **Значения** — клиент меняет `inputs` по id: промпт, seed, LoRA, точки позы, имя загруженной картинки.
4. **Отправка** — `POST /prompt`, дальше опрос `GET /history/{prompt_id}` и файлы через `GET /view`.

| Запрос | Что делает |
| --- | --- |
| `POST /upload/image` | `multipart/form-data`: `image` — файл, `subfolder` — папка во входных файлах, `overwrite=true` — заменить. Ответ `{"name", "subfolder", "type": "input"}` |
| `POST /prompt` | `{"prompt": <граф>, "client_id": "<имя клиента>"}` → `{"prompt_id", "number", "node_errors": {}}`. Неверный граф — `400` и `node_errors` по узлам (например, LoRA не из библиотеки — `value_not_in_list`); во время обучения — `423` |
| `GET /history/{prompt_id}` | `{}`, пока не выполнено; потом `status.status_str` — `success` или `error` — и `outputs.<id узла>`: `images` — файлы `{filename, subfolder, type}`, у наших узлов ещё `window`, `rig`, `folder`, `warnings`. Ошибка выполнения — в `status.messages`, `execution_error`, `exception_message` |
| `GET /view?filename=&subfolder=&type=output` | файл результата: PNG, GIF, `rig.json` |
| `GET /queue` | `queue_running` и `queue_pending` — где задание |
| `POST /queue` с `{"delete": ["<prompt_id>"]}`, `POST /interrupt` | убрать из очереди; прервать текущее |
| `GET /object_info/<узел>` | входы узла и допустимые значения: базовые модели — `CheckpointLoaderSimple`, LoRA — `LoraLoader` |

Имена моделей и LoRA в графе — как в библиотеке: `проект/имя.safetensors` (список — `GET /tool/loras`). Тот же граф с тем же seed повторяет картинку пиксель в пиксель. Прогресс по шагам, если нужен, — веб-сокет `ws://<адрес>/ws?clientId=<client_id>` (события `progress`, `executing`); хватает и опроса `/history`.

Пример с Мака — доспех на базе по шаблону `equipment` (`jq` есть в macOS 15, иначе `brew install jq`):

```bash
HOST=http://192.168.1.50:8188
curl -s -F image=@base.png -F subfolder=lootlords -F overwrite=true $HOST/upload/image
# → {"name": "base.png", "subfolder": "lootlords", "type": "input"}
jq --arg prompt "lootstyle, a man wearing bronze scale armor, bald head, bare hands, full body" \
   --rawfile points skeletons/tall.json --argjson seed 5 \
   '.["90"].inputs.value = $prompt | .["94"].inputs.value = $points | .["40"].inputs.image = "lootlords/base.png"
    | .["5"].inputs.seed = $seed | .["75"].inputs.seed = $seed | {prompt: ., client_id: "lootlords"}' \
   workflows/equipment.json > job.json
curl -s -X POST $HOST/prompt -H "Content-Type: application/json" -d @job.json
# → {"prompt_id": "12cb…", "number": 3, "node_errors": {}}
curl -s $HOST/history/12cb…
# → {} пока идёт; потом status.status_str "success", outputs."80": images (части), rig, folder "parts/armor_00002_ [output]"
curl -o torso.png "$HOST/view?filename=torso.png&subfolder=parts/armor_00002_&type=output"
curl -o rig.json "$HOST/view?filename=rig.json&subfolder=parts/armor_00002_&type=output"
```

`skeletons/tall.json` — точки позы `{"neck": [x, y], ...}`; id `90`, `94`, `40`, `5`, `75` — узлы Prompt, Points, Base, KSampler и Fill этого шаблона. LoRA стиля проекта добавляется в граф узлом LoraLoader после чекпойнта; обучение и библиотека LoRA — в разделах ниже.

## API инструмента

Своё у инструмента — состояние, библиотека LoRA и обучение. Ответы — JSON в UTF-8, ошибки — `{"errors": [...]}`, все сразу. Пути работают и с префиксом `/api`.

| Запрос | Что делает |
| --- | --- |
| `GET /tool/status` | `idle`, `generating` или `training`; текущее — `prompt_id` с шагом или обучение; очередь, видеопамять, базовые модели |
| `GET /tool/loras`, `GET` / `DELETE /tool/loras/{имя}`, `POST /tool/loras` | библиотека LoRA: список, скачать, удалить, загрузить |
| `POST /tool/train`, `GET /tool/jobs/{job_id}` | обучить LoRA, ход обучения |

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

**Перед обучением на Windows:**

1. Закрой программы, которые держат видеопамять: браузеры, Discord, Telegram, VS Code, GitHub Desktop, оверлей NVIDIA App. Тренеру нужно 8,5 ГБ свободных; сколько занято — `nvidia-smi`.
2. Перезапусти ComfyUI: `docker compose restart comfyui`. После множества генераций он держит в оперативной памяти до 10 ГБ, а вместе с тренером (до 12,5 ГБ) это больше, чем WSL отдаёт контейнерам. Тогда sd-scripts убивается на загрузке модели, и в задании ошибка «died with SIGKILL».

## Персонаж из частей

Персонаж — 2D в одном ракурсе из частей на костях, для анимации в игре: игра поворачивает кость, часть на ней поворачивается вместе с ней; снаряжение подменяет части, а не лежит поверх. Шаблоны `parts`, `equipment`, `transfer`, узлы — в разделе «Узлы».

**Поза** — 18 точек OpenPose в пикселях холста: `nose`, `neck`, `right_/left_` + `shoulder`, `elbow`, `wrist`, `hip`, `knee`, `ankle`, `eye`, `ear`. `right_*` — правая сторона фигуры, то есть слева на картинке. Скелет с толстыми линиями, как его учили у ControlNet от xinsir, рисует узел Pose Skeleton; точки остаются точками крепления частей. Телосложение — это скелет. Для частей — поза с руками в стороны (кисти не касаются бёдер, ноги чуть врозь) и местом под ступнями. Высокое телосложение:

```json
{"nose": [512, 200], "neck": [512, 292],
 "right_shoulder": [430, 302], "right_elbow": [372, 432], "right_wrist": [326, 556],
 "left_shoulder": [594, 302], "left_elbow": [652, 432], "left_wrist": [698, 556],
 "right_hip": [468, 565], "right_knee": [452, 760], "right_ankle": [440, 945],
 "left_hip": [556, 565], "left_knee": [572, 760], "left_ankle": [584, 945],
 "right_eye": [496, 185], "left_eye": [528, 185], "right_ear": [476, 195], "left_ear": [548, 195]}
```

**Части** (шаблон `parts`). База режется на 10 частей по костям: `torso`, `head`, `right_upper_arm`, `right_forearm`, `left_upper_arm`, `left_forearm`, `right_thigh`, `right_shin`, `left_thigh`, `left_shin` (кисть — с предплечьем, стопа — с голенью). Бока туловища под руками дорисовываются второй генерацией с тем же стилем. Генерация ложится на холст `size` целиком, в одном масштабе, по оси бёдер. Рядом с PNG частей — `rig.json`:

```json
{"size": 512, "ground": 508.5,
 "bones": [{"name": "torso", "parent": null, "pivot": [256.0, 282.5]},
           {"name": "right_forearm", "parent": "right_upper_arm", "pivot": [186.0, 216.0]}],
 "parts": [{"name": "right_thigh", "bone": "right_thigh", "image": "right_thigh.png", "offset": [205, 247], "order": 0}]}
```

`bones` — от корня к концам, `pivot` — точка поворота на холсте. `parts` — сзади вперёд, `offset` — левый верхний угол PNG на холсте. `ground` — линия пола (низ силуэта). В PixiJS кость — `Container` с `pivot`, часть — `Sprite` в нём. Превью — узел Animation Preview: позы — углы костей в градусах по часовой, `{"right_upper_arm": 60, "right_forearm": 50}`.

**Снаряжение** (шаблон `equipment`) генерируется на картинке базы с её позой и перерисовывает только зону слота (`denoise` 1). В результате только части слота, в координатах базы — ими подменяют части базы.

| `slot` | Перерисовывается | Части |
| --- | --- | --- |
| `armor` | тело без головы, кистей и стоп | туловище и конечности; можно взять только верх или низ |
| `helmet` | голова выше челюсти; нужны глаза и уши в позе, у Pose Skeleton выключен `face` | `head` |
| `right_hand`, `left_hand` | зона у кулака, рука не трогается | `right_weapon` / `left_weapon` на кости предплечья |

Шлемы — только закрытые. База — без шлема и с пустыми руками.

**Другое телосложение** (шаблон `transfer`): части предмета, сделанного на опорном телосложении, переносятся по костям на скелет базы и дорабатываются в зоне слота (`denoise` 0.45) — дизайн тот же, предмет садится на тело.

Промпты, проверенные на базовой SDXL:

| Что | Промпт | Негатив |
| --- | --- | --- |
| база | «… bald head, empty hands, arms held away from the body, full body, front view» | «helmet, hat, hood, weapon, sword, shield, cloak, cape, armor, holding» |
| доспех | «a man wearing … armor, bald head, bare hands, full body» | «helmet, weapon, cloak, gauntlets» |
| шлем | «a knight wearing a closed … helm covering the whole face» | «face, eyes, beard, hat, hood» |
| оружие | «a man holding a … in the right hand, plain light grey background» | «sky, blue background, gradient background» |

На другом телосложении — тот же промпт с поправкой на носителя: «a stocky dwarf wearing …».

## Рамки и иконки

**Рамка** (шаблон `frame`): Frame Layout рисует заготовку-кольцо вокруг окна (`window` в пикселях генерации, по умолчанию квадрат в половину стороны), модель перерисовывает её по промпту, Cut Frame вырезает рамку размером `size` и отдаёт `window` в пикселях рамки. Рамку с `window` проект хранит у себя.

**Иконка** (шаблон `icon`): предмет без фона вписывается в окно рамки по центру, под рамку. `tint` тонирует рамку — одна рамка на все редкости, `background` — цвет окна, `margin` — отступ предмета от края окна; без рамки — предмет на холсте `size`. Другая редкость готовой иконки — тот же граф с LoadImage предмета вместо генерации.

## Папки

| Папка | Что в ней |
| --- | --- |
| `data/models` (или `MODELS_DIR`) | базовые модели и библиотека LoRA `loras/<проект>/` |
| `data/input` | картинки из интерфейса и загруженные входы (`/upload/image`) |
| `data/output` | результаты: картинки, папки частей, GIF |
| `data/user` | настройки интерфейса и сохранённые графы |
| `data/jobs` | записи обучений |

Вся `data/` — в `.gitignore`.

## Узлы

Свои узлы инструмента — для операций, которых нет среди встроенных; в интерфейсе они в разделе **local-comfyui**. Работают в любом графе: собранном в интерфейсе или присланном в штатный `POST /prompt`.

| Узел | Входы | Выходы |
| --- | --- | --- |
| Pose Skeleton | `points` — JSON `{"neck": [x, y], ...}` (раздел «Персонаж из частей»), `width`, `height`, `face` — выключить для закрытого шлема | скелет для ControlNet позы |
| Frame Layout | `width`, `height`, `window` — `[x, y, ширина, высота]` или пусто (квадрат в половину стороны по центру) | заготовка-кольцо для img2img (`denoise` 0,9), окно |
| Cut Frame | картинка, маска из Remove Background, `window` из Frame Layout, `size` | рамка с прозрачностью, окно в пикселях рамки |
| Icon | предмет, его маска, `frame` (необязательно), `window` рамки, `size`, `margin`, `background`, `tint` | иконка с прозрачностью |
| Cut Plan | картинка, маска, `points` её позы | картинка со стёртыми руками и маска туловища под ними — для дорисовки: VAE Encode → Set Latent Noise Mask → KSampler (`denoise` 1) с промптом + «, torso without arms, side of the chest» и негативом «arm, hand, » + негатив |
| Assemble Parts | картинка, маска, `points`, `size`, `slot` (`none` — база целиком), `filename_prefix`; `filled` — дорисованное туловище, `base` — чистая база, для оружия | PNG частей и `rig.json` в папке `output` (`<prefix>_00001_/`); `rig` и `folder` — дальше в Animation Preview |
| Slot Zone | `points`, `width`, `height`, `slot` | зона слота — маска для Set Latent Noise Mask на картинке базы (`denoise` 1) |
| Transfer Parts | база, `points` её позы, `source_points` — поза, на которой сделан предмет, `rig` предмета, `folder` его частей, `slot` | база с частями предмета по её костям — для img2img в зоне слота (`denoise` 0,45) |
| Animation Preview | `rig` (`size`, `bones`, `parts` сзади вперёд — можно из разных rig одного скелета), `folder`, `poses` — углы костей, `steps`, `fps`, `background` | GIF в `output` |

Соглашения: точки и окна — строки JSON, чтобы клиент подставлял их в граф как текст. Маска — передний план, как её даёт Remove Background. Картинка с прозрачностью — `IMAGE` с четырьмя каналами, SaveImage пишет её в RGBA. Рамка, загруженная через LoadImage, получает прозрачность обратно через Join Image with Alpha. Окно рамки, `rig`, папку частей и предупреждения узлы кладут и в `/history` (`outputs.<узел>.window`, `rig`, `folder`, `warnings`, части — в `images`); файлы из `output`, не только картинки (`rig.json`, GIF), отдаёт штатный `GET /view`. Ошибки входов — в `/history`, `status.messages`, `execution_error`.

Части с диска (`folder`) берутся из входных файлов: клиент загружает PNG частей через `POST /upload/image` с полем `subfolder` и пишет его в `folder`, имена — как в `rig`. Папка результата Assemble Parts — с `[output]` в конце: `parts/armor_00001_ [output]`.

## Типовые графы

Меню (логотип слева вверху) → Browse Templates → раздел **local-comfyui**: `txt2img`, `txt2img-lora` (LoRA из библиотеки), `txt2img-pose` (скелет узлом Pose Skeleton), `frame` (рамка иконки), `icon` (предмет в рамке), `parts` (база в части и превью анимации), `equipment` (снаряжение по слоту на базе), `transfer` (снаряжение на другое телосложение), `pixel-art` (предмет или здание в пиксель-арте: уменьшение `area`, 24 цвета `ImageQuantize`, жёсткий край по маске — только встроенные узлы). Сохранённая копия уходит в `data/user`, в проект — через меню → File → Export, для отправки в `/prompt` — File → Export (API).

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
