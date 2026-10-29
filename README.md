# local-comfyui

Локальная генерация картинок и обучение LoRA: ComfyUI с моделью SDXL в Docker на Windows-машине с видеокартой NVIDIA. Генерация и обучение идут целиком на этой машине и без интернета.

Инструмент — сервис. На Windows его один раз разворачивают; с Mac или любой другой машины в сети им пользуются как платным сервисом: визуальный интерфейс ComfyUI в браузере и HTTP API на том же адресе — генерация, обучение LoRA, библиотека LoRA, статус. Инструмент общий и ничего не знает о конкретных играх: всё, что задаёт проект (стиль, промпты, референсы, позы), живёт в репозитории проекта и приходит в запросах. На Windows хранятся только базовые модели и библиотека LoRA.

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

WSL 2 по умолчанию отдаёт контейнерам половину оперативной памяти (на 32 ГБ ComfyUI пишет в логе `total RAM 15914 MB`). Для генерации SDXL и обучения LoRA этого хватает; лимит поднимается через `%USERPROFILE%\.wslconfig`, если понадобится. Но сборку образа (`docker compose build`, `up --build`) не запускай во время генерации: вместе они упираются в эту память, и одна картинка шла 12 минут вместо 13 секунд.

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

Образ — `docker/comfyui/Dockerfile`: Python 3.13, PyTorch с CUDA 13.0, ComfyUI закреплённой версии. Обновлять их вместе и записывать в [docs/LOG.md](docs/LOG.md), иначе одни и те же настройки начнут давать другие картинки. Сторонние расширения (custom nodes) в образ не ставим; свой пакет `local-comfyui` без узлов показывает типовые графы и добавляет API. Тренер LoRA — отдельный образ `docker/trainer` (раздел «Обучение LoRA»).

1. Скопируй `.env.example` в `.env`. Базовые модели и библиотека LoRA лягут в `data/models` внутри репозитория: папка в `.gitignore`, и всё хранится в одном месте, а не разбросано по дискам. Если на диске с репозиторием мало места, укажи в `.env` другую папку:
   ```
   MODELS_DIR=D:/comfyui-models
   ```
2. Положи модели (раздел «Модели»).
3. Собери образы и запусти. Первая сборка качает несколько гигабайт и идёт долго:
   ```powershell
   docker compose up -d --build
   ```
4. Открой http://localhost:8188 — интерфейс ComfyUI. Проверь API: `curl.exe http://localhost:8188/tool/status` отвечает `"state": "idle"` и `"trainer": "available"`.

Поменял `.env` — пересоздай контейнеры той же командой `docker compose up -d`: папки монтируются при создании контейнера, и простого перезапуска мало. Проверить, какие папки подключены: `docker compose config`, раздел `volumes`.

### Доступ с Mac

С другой машины в той же сети — `http://<IP Windows-машины>:8188`: в браузере визуальный интерфейс ComfyUI, для скриптов — API инструмента (раздел «API»). При первом подключении Windows спросит разрешение брандмауэра для Docker.

У инструмента нет авторизации: кто достучался до порта, тот может генерировать, читать результаты и менять библиотеку LoRA. Порт не пробрасываем в интернет; для доступа не из дома — VPN вроде Tailscale.

## API

Проекты пользуются инструментом с Mac как платным сервисом: обычные HTTP-запросы, без клиента и без общих папок. API — пакет `docker/comfyui/local-comfyui` внутри ComfyUI, поэтому адрес у него тот же, что у интерфейса, а задания из API и из интерфейса идут в одну очередь. Все пути доступны и с префиксом `/api`. Ответы — JSON в UTF-8; ошибки — `{"errors": [...]}`, все сразу.

| Запрос | Что делает |
| --- | --- |
| `GET /tool/status` | что делает инструмент: `idle`, `generating` или `training`, текущее задание и его шаг, очередь, видеопамять, базовые модели |
| `POST /tool/generate` | ставит генерацию в очередь, отвечает `202` с `job_id`, seed каждой картинки и всеми параметрами |
| `GET /tool/jobs/{job_id}` | состояние задания и по каждой картинке: `queued`, `running` с шагом, `done` со ссылкой `url`, `error` |
| `GET /view?...` | сама картинка по ссылке из `url` (это API ComfyUI) |
| `GET /tool/loras` | библиотека LoRA: имя, размер, дата |
| `GET /tool/loras/{имя}` | скачать LoRA |
| `POST /tool/loras` | загрузить LoRA: `multipart/form-data`, поле `name` (`проект/имя`) перед полем `file`; есть такая — `409`, заменить — `?overwrite=true` |
| `DELETE /tool/loras/{имя}` | удалить LoRA |
| `POST /tool/train` | обучить LoRA по референсам (раздел «Обучение LoRA») |
| `POST /tool/pose` | картинка скелета по точкам позы, без генерации (PNG) |

**Генерация.** Обязательно только `prompt`, остальное по умолчанию: `checkpoint` `sd_xl_base_1.0.safetensors`, 1024×1024, 28 шагов, `cfg` 6, `dpmpp_2m` / `karras`, одна картинка. `count` до 16 картинок; seed у них идут подряд от `seed` (без него — случайный, от 0 до 2⁶⁴−1, как в интерфейсе). `loras` — список LoRA из библиотеки с весами, пустой — генерация без LoRA. `pose` — поза персонажа, описана ниже. Ответ возвращает все итоговые параметры: их стоит сохранить в проекте, с ними картинка повторяется пиксель в пиксель (проверено, в том числе с seed из интерфейса).

```bash
curl -X POST http://192.168.1.50:8188/tool/generate -H "Content-Type: application/json" -d '{
  "prompt": "mystyle, a knight with a giant hammer",
  "negative": "photo, realistic, blurry, text",
  "seed": 42, "count": 2,
  "loras": [{"name": "lootlords/style-v2", "strength": 0.7}, {"name": "lootlords/weapons-v1", "strength": 0.8}]
}'
```

```bash
curl http://192.168.1.50:8188/tool/jobs/<job_id>
curl -o knight-42.png "http://192.168.1.50:8188/view?filename=42_00001_.png&subfolder=api/<job_id>&type=output"
```

Задания живут в памяти до перезапуска инструмента; картинки остаются на Windows в `data/output/api/<job_id>/`, их можно чистить.

**Поза.** Поле `pose` ставит персонажа в позу и пропорции скелета: ControlNet позы (`controlnet/xinsir-openpose-sdxl.safetensors`) между промптом и сэмплером. Так все персонажи проекта получаются в одной позе, с головой, руками и ногами на одних и тех же местах холста — и с LoRA, и без неё. Проект присылает не картинку, а 18 точек OpenPose в пикселях холста; скелет с нужной толщиной линий рисует инструмент, а те же точки потом служат точками крепления снаряжения.

```json
{
  "prompt": "mystyle, a knight in armor, full body, standing, simple background",
  "seed": 11, "loras": [{"name": "lootlords/style-v2", "strength": 0.8}],
  "pose": {
    "points": {
      "nose": [512, 205], "neck": [512, 300],
      "right_shoulder": [430, 310], "right_elbow": [395, 450], "right_wrist": [375, 585],
      "left_shoulder": [594, 310], "left_elbow": [629, 450], "left_wrist": [649, 585],
      "right_hip": [468, 575], "right_knee": [462, 775], "right_ankle": [458, 965],
      "left_hip": [556, 575], "left_knee": [562, 775], "left_ankle": [566, 965],
      "right_eye": [496, 190], "left_eye": [528, 190], "right_ear": [476, 200], "left_ear": [548, 200]
    }
  }
}
```

| Поле `pose` | Что значит |
| --- | --- |
| `points` | точки OpenPose, `[x, y]` в пикселях холста `width`×`height`; `right_*` — правая сторона самой фигуры, то есть слева на картинке, если фигура смотрит на зрителя. Отсутствующая точка просто не рисуется |
| `strength` | сила ControlNet от 0 до 2, по умолчанию 1: поза держится строго; меньше — свободнее |
| `start`, `end` | на какой доле шагов сэмплера ControlNet действует, по умолчанию 0 и 1 |
| `model` | ControlNet из `MODELS_DIR/controlnet`, по умолчанию `xinsir-openpose-sdxl.safetensors` |

В ответе на генерацию с позой есть `pose_url` — ссылка на скелет, по которому шла генерация. Тот же скелет без генерации — `POST /tool/pose` с `points` (и, если холст не 1024×1024, `width` и `height`): удобно смотреть позу, пока её правишь, и загружать в шаблон `txt2img-pose` в интерфейсе.

Пачку — кандидаты в эталоны, варианты фонов, сравнение версий LoRA — проект делает сам: `count` в одном запросе или цикл запросов из своего скрипта. Список промптов, seed и итоговые параметры из ответов хранятся в репозитории проекта.

**Библиотека LoRA** — `data/models/loras` на Windows, имена `проект/имя` (у обученных — с версией: `проект/имя-v2`). Все LoRA видны и в интерфейсе, в узле LoraLoader. Проект держит копии своих LoRA у себя и, если на Windows их нет, загружает:

```bash
curl -F name=lootlords/style-v2 -F file=@style-v2.safetensors http://192.168.1.50:8188/tool/loras
curl -o style-v2.safetensors http://192.168.1.50:8188/tool/loras/lootlords/style-v2
```

Загрузка проверяет, что файл — safetensors, и возвращает его sha256.

## Папки

Всё, что инструмент хранит на Windows, — в `data/`, папка в `.gitignore`:

| Папка | Что в ней |
| --- | --- |
| `data/models` (или `MODELS_DIR`) | базовые модели, общие для всех проектов (`checkpoints`, `controlnet`, `vae`, `upscale_models`), и библиотека LoRA `loras/<проект>/` |
| `data/input` | картинки, загруженные в интерфейсе |
| `data/output` | всё сгенерированное; из API — в `output/api/<job_id>/` |
| `data/user` | настройки интерфейса ComfyUI и сохранённые в нём графы |
| `data/jobs` | записи обучений: параметры и лог sd-scripts |

## Типовые графы

Типовые графы инструмента лежат в `docker/comfyui/local-comfyui/example_workflows/` и видны в интерфейсе: Workflow → Browse Templates → раздел **local-comfyui** внизу списка. Они собираются в образ и обновляются вместе с инструментом. Открыл, подстроил модель, промпты и размер, сохранил (Workflow → Save) — копия ушла в `data/user`, а типовой граф остался как был; чтобы граф стал частью проекта, экспортируй его (Workflow → Export) в репозиторий проекта.

| Граф | Что делает |
| --- | --- |
| `txt2img` | текст → картинка на SDXL без LoRA: кандидаты в эталоны, фоны |
| `txt2img-lora` | то же с LoRA из библиотеки: в узле LoraLoader выбери `проект/имя` |
| `txt2img-pose` | то же по позе: в LoadImage загрузи кнопкой upload скелет из `POST /tool/pose` |

API `/tool/generate` строит те же графы, с цепочкой LoRA вместо одной: картинка из интерфейса с теми же параметрами и seed повторяется через API пиксель в пиксель.

## Обучение LoRA

LoRA обучается через API: проект присылает zip с референсами и параметры, инструмент обучает и кладёт LoRA в библиотеку. Под капотом — [kohya sd-scripts](https://github.com/kohya-ss/sd-scripts) в отдельном образе `docker/trainer`: у тренера свои версии Python и PyTorch (3.10 и 2.8, на них sd-scripts проверяется), с ComfyUI они не уживаются. Сервис `trainer` работает только внутри сети Docker, наружу его порт не открыт.

```bash
curl -F name=lootlords/style -F 'params={"steps": 1500}' -F dataset=@style-refs.zip http://192.168.1.50:8188/tool/train
```

Поля `multipart/form-data` — в таком порядке: `name`, `params`, `dataset`.

- **`name`** — `проект/имя`; версия добавится сама: первая обученная — `lootlords/style-v1`, следующая — `-v2`.
- **`dataset`** — zip с картинками (png, jpg, webp) и подписями: `.txt` с тем же именем рядом с каждой. Папки внутри архива не важны, файлы складываются в одну. Картинка без подписи — ошибка до начала обучения.
- **`params`** — JSON, всё необязательно:

| Поле | Что значит |
| --- | --- |
| `checkpoint` | база, на которой обучать, по умолчанию `sd_xl_base_1.0.safetensors`. LoRA работает только с той базой, на которой обучена |
| `steps` | сколько шагов обучать, по умолчанию 1500 |
| `rank` | ранг LoRA, по умолчанию 16: больше — больше деталей и больше файл |
| `alpha` | по умолчанию `rank / 2` |
| `learning_rate` | по умолчанию 0.0001 |
| `resolution` | размер обучения, кратно 64, по умолчанию 1024; картинки других пропорций раскладываются по корзинам сами |
| `repeats` | сколько раз каждая картинка встречается за проход по набору, по умолчанию 10 |
| `save_every` | промежуточное сохранение каждые N шагов, по умолчанию 250 |
| `seed` | по умолчанию 42 |

**Подписи** пишутся руками. Для LoRA стиля подпись описывает содержимое и начинается со слова-триггера, а слова о стиле в неё не пишут: тогда стиль привязывается к триггеру, например `mystyle, a dwarf warrior with an axe`.

Настройки, нужные из-за 10 ГБ видеопамяти, проект не задаёт — их ставит тренер по рекомендациям sd-scripts для SDXL: обучается только U-Net, выходы текстовых энкодеров и латенты кешируются, gradient checkpointing, bf16, оптимизатор AdamW8bit.

**Видеокарта.** Обучение и генерация вместе в 10 ГБ не помещаются. Обучение стартует, только когда очередь генерации пуста (иначе `409`), ComfyUI выгружает свои модели, и до конца обучения любая генерация — из API и из интерфейса — получает отказ `423` с именем обучаемой LoRA. Второе обучение в это время — тоже `409`.

**Прогресс** — в `GET /tool/status` (`state: training`, шаг, loss, оставшиеся секунды) и в `GET /tool/jobs/{job_id}` задания обучения. Готовое задание показывает имя LoRA, её sha256 и промежуточные сохранения:

```json
{"type": "train", "state": "done", "lora": "lootlords/style-v1", "sha256": "be6c6c…",
 "intermediate": ["lootlords/style-v1/style-v1-step00000250", "…"], "minutes": 33.5}
```

Дальше — `GET /tool/loras/lootlords/style-v1`, чтобы забрать файл в проект, а генерация с ней — как с любой LoRA из библиотеки.

На RTX 3080 шаг занимает около 1,3 с, видеопамяти уходит около 8,2 ГБ: 600 шагов по 20 картинкам — 13 минут, вместе с загрузкой модели и кешированием около 17. Оперативной памяти тренер берёт до 12,5 ГБ из 16, что WSL отдаёт по умолчанию; ComfyUI после выгрузки моделей держит около 1,6 ГБ.

**Какую точку взять.** Недообученная LoRA почти не меняет картинку, переобученная копирует эталоны: на проверке к шагу 400–600 появились рамки и «печати» из набора, а объекты промпта начали пропадать. Промежуточные сохранения лежат в библиотеке рядом с итоговой, поэтому их сравнивают обычной генерацией: один промпт, один seed, разные `loras`, и одна генерация без LoRA. Картинок-примеров по ходу обучения нет: sd-scripts умеет их рисовать, но на 10 ГБ после второго раунда примеров обучение зависло.

Записи обучения — параметры и лог sd-scripts — остаются на Windows в `data/jobs/<job_id>/`; сами референсы после обучения удаляются, они принадлежат проекту.

## Модели

| Что | Файл в `MODELS_DIR` | Размер, байт | sha256 | Откуда | Лицензия |
| --- | --- | --- | --- | --- | --- |
| Базовая SDXL | `checkpoints/sd_xl_base_1.0.safetensors` | 6938078334 | `31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b` | [stabilityai/stable-diffusion-xl-base-1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors) | CreativeML Open RAIL++-M |
| ControlNet позы | `controlnet/xinsir-openpose-sdxl.safetensors` | 2502139104 | `b8524e557a7df60d081f5d4a0eb109967d107df217943bf88c2d99b9ebcc06c5` | [xinsir/controlnet-openpose-sdxl-1.0](https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/resolve/main/diffusion_pytorch_model.safetensors), файл переименовываем: в оригинале безликое имя | Apache 2.0 |

По sha256 проверяют, что файл скачан целиком и это та самая версия: другая версия базы даёт другие картинки, а LoRA работает только с той базой, на которой обучена.

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

У ControlNet от xinsir есть особенность: его учили на скелетах с толстыми линиями. Со стандартной тонкой отрисовкой OpenPose поза держится хуже, поэтому инструмент рисует скелет сам, линиями около 1/64 размера холста (16 px на 1024), а проект присылает только точки.

## Проверка

В ComfyUI: Workflow → Browse Templates → «SDXL Simple». Шаблон собран из двух ступеней, base и refiner, и просит ещё `sd_xl_refiner_1.0.safetensors` — его не качаем: в узле `Load Checkpoint - REFINER` выбери `sd_xl_base_1.0.safetensors`, и предупреждение пропадёт. Стартовый workflow, который ComfyUI открывает сам, собран не на SDXL и тоже жалуется на отсутствующие модели — это нормально, просто открой шаблон.

Задай 1024×1024, 25–30 шагов, CFG 5–7, сэмплер `dpmpp_2m`, планировщик `karras`. Промпт, например, `cartoon fantasy dwarf warrior, full body, bold black outline, flat colors, simple background`, негативный — `photo, realistic, 3d render, blurry, text, watermark`. Картинка появилась в `data/output` — генерация работает.

То же через API, с Mac или с самой Windows:

```bash
curl -X POST http://localhost:8188/tool/generate -H "Content-Type: application/json" -d '{"prompt": "cartoon fantasy dwarf warrior, full body", "seed": 1}'
curl http://localhost:8188/tool/jobs/<job_id>
```

Задание в состоянии `done` со ссылкой `url` — API работает.

На RTX 3080 первая картинка 1024×1024 после запуска занимает около 30 с (модель грузится в видеопамять), следующие — около 10 с, примерно 3,3 шага в секунду.

Если ComfyUI не видит модели:

1. `GET /tool/status` — в `checkpoints` список базовых моделей, которые видит ComfyUI.
2. `docker compose config` — в `volumes` у `/opt/comfyui/models` та папка, где лежат модели: `data/models` или твой `MODELS_DIR`.
3. `docker compose exec comfyui ls -la models/checkpoints models/controlnet` — файлы на месте и нужного размера. Если нет, контейнер создан до правки `.env`: `docker compose up -d`.
4. Узел ждёт другое имя файла (как refiner в шаблоне выше) — выбери свой файл в узле.

## Команды

| Команда | Что получим |
| --- | --- |
| `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi` | проверка, что Docker видит видеокарту: таблица с её именем и памятью |
| `docker compose up -d --build` | собранные образы ComfyUI и тренера, инструмент на http://localhost:8188; первая сборка долгая |
| `docker compose up -d` | контейнеры пересозданы, если поменялся `.env` или `compose.yaml` |
| `curl.exe http://localhost:8188/tool/status` | что делает инструмент: простаивает, генерирует или обучает, и с каким прогрессом |
| `docker compose logs -f comfyui` | логи ComfyUI и API |
| `docker compose logs -f trainer` | логи тренера: что обучается и что пишет sd-scripts |
| `docker compose stop` | инструмент остановлен, видеопамять свободна; модели и библиотека LoRA остаются на диске |

## Документация

- [docs/ROADMAP.md](docs/ROADMAP.md) — что сделано и что дальше
- [docs/LOG.md](docs/LOG.md) — журнал изменений и принятых решений
- [CLAUDE.md](CLAUDE.md) — правила работы для Claude Code
