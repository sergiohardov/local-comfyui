# local-comfyui

Генерация 2D-графики и обучение LoRA: ComfyUI с SDXL в Docker на Windows-машине с видеокартой NVIDIA. Сервис для нескольких проектов: с Mac им пользуются через визуальный интерфейс ComfyUI и HTTP API на одном адресе. Инструмент не знает о проектах: стиль, промпты, скелеты и LoRA живут в репозитории проекта и приходят в запросах; на Windows — только базовые модели и библиотека LoRA. Почему всё устроено так — в [docs/LOG.md](docs/LOG.md).

Что умеет:

- **персонаж из частей** для анимации на костях: база по скелету телосложения, нарезка на части, превью анимации;
- **снаряжение** — доспех, закрытый шлем, оружие — генерируется на базе и заменяет её части; переносится между телосложениями;
- **рамки и иконки** предметов и навыков;
- **обучение LoRA** стиля и библиотека LoRA.

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

## API

Ответы — JSON в UTF-8, ошибки — `{"errors": [...]}`, все сразу. Пути работают и с префиксом `/api`. Задания из API и из интерфейса идут в одну очередь; задания живут в памяти до перезапуска, файлы — в `data/output/api/<job_id>/`.

| Запрос | Что делает |
| --- | --- |
| `POST /tool/generate` | ставит генерацию в очередь: `202`, `job_id`, seed каждой картинки и все параметры |
| `GET /tool/jobs/{job_id}` | по каждой картинке: `queued`, `running` (шаг, `stage`), `done` со ссылками, `error` |
| `GET /view?...` | файл по ссылке из задания (API ComfyUI) |
| `GET /tool/status` | `idle`, `generating` или `training`, текущее задание, очередь, видеопамять, базовые модели |
| `POST /tool/post` | рамка, иконка или части из готовой картинки, без генерации |
| `POST /tool/animate` | превью анимации частей, GIF |
| `POST /tool/pose` | картинка скелета по точкам, PNG |
| `GET /tool/loras`, `GET` / `DELETE /tool/loras/{имя}`, `POST /tool/loras` | библиотека LoRA: список, скачать, удалить, загрузить |
| `POST /tool/train` | обучить LoRA |

### `POST /tool/generate`

| Поле | По умолчанию | Что значит |
| --- | --- | --- |
| `prompt` | обязательно | |
| `negative` | `""` | |
| `seed` | случайный | от 0 до 2⁶⁴−1; при `count` > 1 seed идут подряд |
| `count` | 1 | до 16 картинок |
| `loras` | `[]` | `[{"name": "проект/имя", "strength": 0.8}]`, цепочка в порядке списка |
| `checkpoint` | `sd_xl_base_1.0.safetensors` | база из `MODELS_DIR/checkpoints` |
| `width`, `height` | 1024 | кратно 8 |
| `steps`, `cfg`, `sampler`, `scheduler` | 28, 6, `dpmpp_2m`, `karras` | |
| `pose` | — | скелет, ниже |
| `init` | — | исходная картинка или снаряжение на базе, ниже |
| `post` | — | что сделать с результатом: `parts`, `frame`, `icon`, ниже |

С теми же параметрами и seed картинка повторяется пиксель в пиксель, в том числе картинка из интерфейса. Параметры из ответа проект хранит у себя.

```bash
curl -X POST http://192.168.1.50:8188/tool/generate -H "Content-Type: application/json" -d '{
  "prompt": "lootstyle, a young man in a linen shirt and trousers, bald head, empty hands, full body, front view",
  "seed": 31, "loras": [{"name": "lootlords/style-v2", "strength": 0.8}]}'
curl http://192.168.1.50:8188/tool/jobs/<job_id>
```

### `pose` — скелет

18 точек OpenPose в пикселях холста: `nose`, `neck`, `right_/left_` + `shoulder`, `elbow`, `wrist`, `hip`, `knee`, `ankle`, `eye`, `ear`. `right_*` — правая сторона фигуры, то есть слева на картинке. Скелет с толстыми линиями, как его учили у ControlNet от xinsir, рисует инструмент; ссылка на него — `pose_url` в ответе, без генерации — `POST /tool/pose` с `points` (и `width`, `height`, если холст не 1024).

Телосложение — это скелет. Для частей — поза с руками в стороны (кисти не касаются бёдер, ноги чуть врозь) и местом под ступнями. Высокое телосложение:

```json
{"points": {"nose": [512, 200], "neck": [512, 292],
  "right_shoulder": [430, 302], "right_elbow": [372, 432], "right_wrist": [326, 556],
  "left_shoulder": [594, 302], "left_elbow": [652, 432], "left_wrist": [698, 556],
  "right_hip": [468, 565], "right_knee": [452, 760], "right_ankle": [440, 945],
  "left_hip": [556, 565], "left_knee": [572, 760], "left_ankle": [584, 945],
  "right_eye": [496, 185], "left_eye": [528, 185], "right_ear": [476, 195], "left_ear": [548, 195]}}
```

Ещё поля: `strength` (0–2, по умолчанию 1), `start` и `end` (доля шагов, 0 и 1), `model` (`xinsir-openpose-sdxl.safetensors`).

### `post: parts` — персонаж из частей

`{"type": "parts", "size": 512}`, поза обязательна. Результат режется на 10 частей по костям: `torso`, `head`, `right_upper_arm`, `right_forearm`, `left_upper_arm`, `left_forearm`, `right_thigh`, `right_shin`, `left_thigh`, `left_shin` (кисть — с предплечьем, стопа — с голенью). Бока туловища под руками дорисовываются второй генерацией с тем же стилем — в задании это `running` со `stage`. Генерация ложится на холст `size` целиком, в одном масштабе, по оси бёдер.

В задании у картинки — `parts` (ссылка на PNG каждой части) и `rig`; копия `rig` — в `rig.json` рядом с частями:

```json
{"size": 512, "ground": 508.5,
 "bones": [{"name": "torso", "parent": null, "pivot": [256.0, 282.5]},
           {"name": "right_forearm", "parent": "right_upper_arm", "pivot": [186.0, 216.0]}],
 "parts": [{"name": "right_thigh", "bone": "right_thigh", "image": "right_thigh.png", "offset": [205, 247], "order": 0}]}
```

`bones` — от корня к концам, `pivot` — точка поворота на холсте. `parts` — сзади вперёд, `offset` — левый верхний угол PNG на холсте. `ground` — линия пола (низ силуэта). В PixiJS кость — `Container` с `pivot`, часть — `Sprite` в нём.

### `init` — исходная картинка и снаряжение

`{"image": "<PNG в base64>", "denoise": 0.75}` — img2img: 0.3–0.5 — вариация, 0.9 — остаются раскладка и цвета. В ответе — `init_url`.

**Снаряжение на базе:** `init.image` — картинка базы (её `url` из задания, проект хранит у себя), `init.slot` — слот, поза — та же, что у базы. Перерисовывается только зона слота (`denoise` 1, в ответе `zone_url`); с `post: parts` в результате только части слота, в координатах базы — подменяй ими части базы.

| `slot` | Перерисовывается | Части |
| --- | --- | --- |
| `armor` | тело без головы, кистей и стоп | туловище и конечности; можно взять только верх или низ |
| `helmet` | голова выше челюсти; нужны глаза и уши в позе | `head` |
| `right_hand`, `left_hand` | зона у кулака, рука не трогается | `right_weapon` / `left_weapon` на кости предплечья |

Шлемы — только закрытые. База — без шлема и с пустыми руками.

**Другое телосложение:** `init.from` — части предмета, сделанного на опорном телосложении, и его скелет. Инструмент переносит их по костям на скелет базы и дорабатывает в зоне слота (`denoise` 0.45): дизайн тот же, предмет садится на тело.

```json
"init": {"image": "<PNG базы дварфа>", "slot": "armor",
         "from": {"points": {"...": "скелет опорного телосложения"}, "size": 512,
                  "parts": [{"name": "torso", "image": "<PNG части>", "offset": [216, 152]}]}}
```

Промпты, проверенные на базовой SDXL:

| Что | Промпт | Негатив |
| --- | --- | --- |
| база | «… bald head, empty hands, arms held away from the body, full body, front view» | «helmet, hat, hood, weapon, sword, shield, cloak, cape, armor, holding» |
| доспех | «a man wearing … armor, bald head, bare hands, full body» | «helmet, weapon, cloak, gauntlets» |
| шлем | «a knight wearing a closed … helm covering the whole face» | «face, eyes, beard, hat, hood» |
| оружие | «a man holding a … in the right hand, plain light grey background» | «sky, blue background, gradient background» |

На другом телосложении — тот же промпт с поправкой на носителя: «a stocky dwarf wearing …».

### `post: frame` и `post: icon` — рамки и иконки

**`frame`** — `{"type": "frame", "size": 128, "window": [256, 256, 512, 512], "denoise": 0.9}`. Инструмент рисует заготовку-кольцо вокруг окна (`window` в пикселях генерации, по умолчанию квадрат в половину стороны) и генерирует рамку от неё. Результат — `frame_url` и `window` в пикселях рамки; рамку с `window` проект хранит у себя.

**`icon`** — предмет без фона вписывается в окно рамки по центру. В задании — `icon_url`.

| Поле | По умолчанию | Что значит |
| --- | --- | --- |
| `frame` | — | PNG рамки в base64; без неё — предмет на холсте `size` |
| `window` | — | окно рамки из её генерации; с рамкой обязательно |
| `size` | 128 | холст без рамки |
| `margin` | 6 | отступ предмета от края окна |
| `background` | — | цвет окна `#rrggbb` |
| `tint` | — | цвет, в который тонируется рамка, — одна рамка на все редкости |

У всех типов `post` есть `model` — модель удаления фона, по умолчанию `birefnet.safetensors`.

### `POST /tool/post`

Те же `parts`, `frame`, `icon` из готовой картинки: другая редкость иконки, повторная нарезка. `multipart/form-data`: `post` (JSON), `pose` (`{"points": ...}`, для `parts`), `image`. Картинка с прозрачностью берётся по своей альфе, без неё — через BiRefNet. Результат — в `GET /tool/jobs/{job_id}`.

```bash
curl -F 'post={"type": "icon", "frame": "<PNG рамки в base64>", "window": [28, 28, 71, 71], "tint": "#a335ee"}' -F image=@helmet.png http://192.168.1.50:8188/tool/post
```

### `POST /tool/animate`

Превью анимации частей в GIF. JSON: `size` и `bones` из `rig`; `parts` — сзади вперёд, `{"bone", "image": "<PNG в base64>", "offset"}`, можно из разных результатов одного скелета; `poses` — 1–32 позы, углы костей в градусах по часовой: `{"right_upper_arm": 60, "right_forearm": 50}`; `steps` (8 кадров между позами), `fps` (12), `background` (`#ffffff`).

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

Тренер — kohya sd-scripts в своём образе `docker/trainer`, только во внутренней сети Docker; настройки под 10 ГБ ставит он сам. Обучение стартует при пустой очереди (иначе `409`), и до конца любая генерация получает `423`. Прогресс — в `GET /tool/status` и `GET /tool/jobs/{job_id}`; готовое задание — имя LoRA, sha256 и промежуточные сохранения. Точку выбирают генерацией с одним seed и разными сохранениями. RTX 3080: около 1,3 с на шаг, 8,2 ГБ видеопамяти.

**Перед обучением на Windows:**

1. Закрой программы, которые держат видеопамять: браузеры, Discord, Telegram, VS Code, GitHub Desktop, оверлей NVIDIA App. Тренеру нужно 8,5 ГБ свободных; сколько занято — `nvidia-smi`.
2. Перезапусти ComfyUI: `docker compose restart comfyui`. После множества генераций он держит в оперативной памяти до 10 ГБ, а вместе с тренером (до 12,5 ГБ) это больше, чем WSL отдаёт контейнерам. Тогда sd-scripts убивается на загрузке модели, и в задании ошибка «died with SIGKILL».

## Папки

| Папка | Что в ней |
| --- | --- |
| `data/models` (или `MODELS_DIR`) | базовые модели и библиотека LoRA `loras/<проект>/` |
| `data/input` | картинки из интерфейса и входы заданий API |
| `data/output` | результаты; из API — `output/api/<job_id>/` |
| `data/user` | настройки интерфейса и сохранённые графы |
| `data/jobs` | записи обучений |

Вся `data/` — в `.gitignore`.

## Узлы

Свои узлы инструмента — для операций, которых нет среди встроенных; в интерфейсе они в разделе **local-comfyui**. Работают в любом графе: собранном в интерфейсе или присланном в штатный `POST /prompt`. Картинки получаются те же, что у `/tool/generate` с теми же параметрами.

| Узел | Входы | Выходы |
| --- | --- | --- |
| Pose Skeleton | `points` — JSON `{"neck": [x, y], ...}` (точки как у `pose`), `width`, `height`, `face` — выключить для закрытого шлема | скелет для ControlNet позы |
| Frame Layout | `width`, `height`, `window` — `[x, y, ширина, высота]` или пусто (квадрат в половину стороны по центру) | заготовка-кольцо для img2img (`denoise` 0,9), окно |
| Cut Frame | картинка, маска из Remove Background, `window` из Frame Layout, `size` | рамка с прозрачностью, окно в пикселях рамки |
| Icon | предмет, его маска, `frame` (необязательно), `window` рамки, `size`, `margin`, `background`, `tint` | иконка с прозрачностью |

Соглашения: точки и окна — строки JSON, чтобы клиент подставлял их в граф как текст. Маска — передний план, как её даёт Remove Background. Картинка с прозрачностью — `IMAGE` с четырьмя каналами, SaveImage пишет её в RGBA. Рамка, загруженная через LoadImage, получает прозрачность обратно через Join Image with Alpha. Окно рамки и предупреждения узлы кладут и в `/history` (`outputs.<узел>.window`, `warnings`); файлы из `output`, не только картинки, отдаёт штатный `GET /view`. Ошибки входов — в `/history`, `status.messages`, `execution_error`.

## Типовые графы

Workflow → Browse Templates → раздел **local-comfyui**: `txt2img`, `txt2img-lora` (LoRA из библиотеки), `txt2img-pose` (скелет узлом Pose Skeleton), `frame` (рамка иконки), `icon` (предмет в рамке). Сохранённая копия уходит в `data/user`, в проект — через Workflow → Export, для отправки в `/prompt` — Workflow → Export (API).

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

```bash
curl -X POST http://localhost:8188/tool/generate -H "Content-Type: application/json" -d '{"prompt": "cartoon fantasy dwarf warrior, full body", "seed": 1}'
curl http://localhost:8188/tool/jobs/<job_id>
```

Задание `done` со ссылкой `url` — работает. RTX 3080: первая картинка 1024×1024 — около 30 с, следующие — около 10 с.

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
