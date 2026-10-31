#!/usr/bin/env node
// Client of local-comfyui for other machines: node packages and graphs of a project, generation, LoRA and training.
// Node.js 18+, no dependencies. The server gives the current version: GET /tool/client.mjs, or `client.mjs update`.
// Address: COMFY_URL or --url, by default http://localhost:8188. `client.mjs help` lists the commands.
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HELP = `Команды (адрес — COMFY_URL или --url):
  status                                  состояние инструмента, очередь, видеопамять, обучение
  logs [comfyui|trainer] [--lines N] [--job ID]   хвост лога ComfyUI или обучения
  restart                                 перезапустить ComfyUI (очередь должна быть пуста)
  cleanup --older-days N [--folders input,output] очистить кэш входов и результатов на сервере
  nodes                                   узлы проектов на сервере
  push-nodes <проект> <папка пакета>      отправить пакет узлов проекта (папка с __init__.py)
  rm-nodes <проект>                       убрать узлы проекта с сервера
  workflows                               общие графы инструмента
  workflow <имя> [--out файл]             общий граф в формате API
  pull <имя> [--dir папка]                граф последнего запуска из интерфейса → <имя>.ui.json и <имя>.api.json
  push <граф.ui.json> [--name имя]        граф в боковую панель Workflows интерфейса
  check <граф.api.json>                   есть ли на сервере узлы, модели и LoRA графа
  run <граф.api.json> [--set Узел.вход=значение] [--set-file Узел.вход=файл]
      [--upload Узел[.вход]=файл] [--project имя] [--out папка]
                                          запустить граф и скачать результаты; Узел — id или заголовок
  train <проект/имя> <папка эталонов> [--params JSON] [--trigger слово] [--no-wait]
                                          обучить LoRA: картинки и подписи .txt того же имени
  lora list | push <проект/имя> <файл> [--overwrite] | pull <проект/имя> [файл] | rm <проект/имя>
  update                                  скачать с сервера свежую версию клиента`;

const IMAGES = new Set([".png", ".jpg", ".jpeg", ".webp"]);

function parse(argv) {
  // Positional arguments and --options; options that repeat (--set, --upload) collect into lists
  const args = [], options = {};
  for (let i = 0; i < argv.length; i++) {
    if (!argv[i].startsWith("--")) { args.push(argv[i]); continue; }
    const key = argv[i].slice(2);
    const value = argv[i + 1] === undefined || argv[i + 1].startsWith("--") ? true : argv[++i];
    if (["set", "set-file", "upload"].includes(key)) (options[key] ??= []).push(value);
    else options[key] = value;
  }
  return { args, options };
}

const { args, options } = parse(process.argv.slice(2));
const BASE = String(options.url ?? process.env.COMFY_URL ?? "http://localhost:8188").replace(/\/$/, "");

function fail(message) {
  console.error(message);
  process.exit(1);
}

async function request(method, url, body, headers = {}) {
  let response;
  try {
    response = await fetch(BASE + url, { method, body, headers });
  } catch (error) {
    fail(`сервер ${BASE} недоступен: ${error.cause?.code ?? error.message}`);
  }
  return response;
}

async function json(method, url, body) {
  const response = await request(method, url, body === undefined ? undefined : JSON.stringify(body),
    body === undefined ? {} : { "Content-Type": "application/json" });
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { errors: [text] }; }
  if (!response.ok) {
    const errors = data.errors ?? [data.error?.message ?? `${response.status} ${response.statusText}`];
    fail(errors.join("\n"));
  }
  return data;
}

function print(data) {
  console.log(JSON.stringify(data, null, 2));
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- zip without compression: enough for node packages and sets of references, and needs no library

const CRC = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

function crc32(buffer) {
  let c = 0xffffffff;
  for (const byte of buffer) c = CRC[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function zip(files) {
  // files: [{name, data}] → Buffer of a zip with the stored method
  const parts = [], central = [];
  let offset = 0;
  for (const { name, data } of files) {
    const nameBytes = Buffer.from(name, "utf8"), crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x0800, 6);
    local.writeUInt32LE(crc, 14); local.writeUInt32LE(data.length, 18); local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0); entry.writeUInt16LE(20, 4); entry.writeUInt16LE(20, 6);
    entry.writeUInt16LE(0x0800, 8); entry.writeUInt32LE(crc, 16); entry.writeUInt32LE(data.length, 20);
    entry.writeUInt32LE(data.length, 24); entry.writeUInt16LE(nameBytes.length, 28); entry.writeUInt32LE(offset, 42);
    parts.push(local, nameBytes, data);
    central.push(entry, nameBytes);
    offset += 30 + nameBytes.length + data.length;
  }
  const size = central.reduce((sum, b) => sum + b.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(files.length, 8); end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(size, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, ...central, end]);
}

async function walk(root, skip = () => false) {
  // Files under root as paths relative to it, with forward slashes
  const found = [];
  for (const entry of await fs.readdir(root, { withFileTypes: true })) {
    if (skip(entry.name)) continue;
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) found.push(...(await walk(full, skip)).map((p) => `${entry.name}/${p}`));
    else found.push(entry.name);
  }
  return found;
}

// --- graphs

function find(graph, selector) {
  // A node by its id or by its title (_meta.title)
  if (graph[selector]) return selector;
  const ids = Object.keys(graph).filter((id) => graph[id]._meta?.title === selector);
  if (ids.length === 1) return ids[0];
  if (ids.length > 1) fail(`заголовок «${selector}» у нескольких узлов: ${ids.join(", ")} — укажи id`);
  fail(`в графе нет узла с id или заголовком «${selector}»`);
}

function target(graph, key, defaultInput) {
  // "Узел.вход" — the node is everything before the last dot, so titles may contain dots
  const dot = key.lastIndexOf(".");
  const [selector, input] = dot > 0 ? [key.slice(0, dot), key.slice(dot + 1)] : [key, defaultInput];
  if (!input) fail(`«${key}»: укажи вход — Узел.вход`);
  const id = find(graph, selector);
  return { id, input };
}

function assign(graph, key, raw) {
  // The value takes the type of the one in the graph, so a prompt stays text and a seed stays a number
  const eq = key.indexOf("=");
  if (eq < 0) fail(`«${key}»: нужно Узел.вход=значение`);
  const { id, input } = target(graph, key.slice(0, eq));
  const value = raw ?? key.slice(eq + 1);
  const current = graph[id].inputs[input];
  if (Array.isArray(current)) fail(`${id}.${input} подключён к узлу ${current[0]} — значение подставляется туда`);
  if (typeof current === "number") {
    if (Number.isNaN(Number(value))) fail(`${id}.${input}: нужно число, а не «${value}»`);
    graph[id].inputs[input] = Number(value);
  } else if (typeof current === "boolean") graph[id].inputs[input] = value === "true";
  else graph[id].inputs[input] = value;
}

async function readGraph(file) {
  let graph;
  try { graph = JSON.parse(await fs.readFile(file, "utf8")); } catch (error) { fail(`${file}: ${error.message}`); }
  if (Array.isArray(graph.nodes)) fail(`${file} — граф в формате интерфейса; для API нужен .api.json (pull или File → Export (API))`);
  return graph;
}

async function check(graph) {
  // Everything the graph names must be on the server: node types, models, LoRA and other choices from lists
  const info = await json("GET", "/object_info");
  const problems = [];
  for (const [id, node] of Object.entries(graph)) {
    const spec = info[node.class_type];
    const label = `${id} (${node._meta?.title ?? node.class_type})`;
    if (!spec) { problems.push(`${label}: на сервере нет узла ${node.class_type}`); continue; }
    const inputs = { ...spec.input.required, ...spec.input.optional };
    for (const [name, value] of Object.entries(node.inputs)) {
      if (Array.isArray(value) || !inputs[name]) continue;
      const [type, extra] = inputs[name];
      const choices = Array.isArray(type) ? type : type === "COMBO" ? extra?.options : null;
      if (choices && choices.length && !choices.includes(value)) {
        problems.push(`${label}: ${name} = «${value}» — на сервере нет такого; есть: ${choices.slice(0, 8).join(", ")}${choices.length > 8 ? " …" : ""}`);
      }
    }
  }
  return problems;
}

async function upload(file, subfolder) {
  const form = new FormData();
  form.append("image", new Blob([await fs.readFile(file)]), path.basename(file));
  form.append("subfolder", subfolder);
  form.append("overwrite", "true");
  const response = await request("POST", "/upload/image", form);
  if (!response.ok) fail(`${file}: загрузка не удалась, ${response.status}`);
  const data = await response.json();
  return data.subfolder ? `${data.subfolder}/${data.name}` : data.name;
}

async function run(file) {
  const graph = await readGraph(file);
  const project = options.project ?? "client";
  for (const item of options.set ?? []) assign(graph, item);
  for (const item of options["set-file"] ?? []) {
    const eq = item.indexOf("=");
    assign(graph, item.slice(0, eq) + "=", await fs.readFile(item.slice(eq + 1), "utf8"));
  }
  for (const item of options.upload ?? []) {
    const eq = item.indexOf("=");
    if (eq < 0) fail(`--upload ${item}: нужно Узел[.вход]=файл`);
    const { id, input } = target(graph, item.slice(0, eq), "image");
    graph[id].inputs[input] = await upload(item.slice(eq + 1), project);
  }
  const response = await request("POST", "/prompt", JSON.stringify({ prompt: graph, client_id: `client-${project}` }),
    { "Content-Type": "application/json" });
  const answer = await response.json().catch(() => ({}));
  if (!response.ok) {
    const lines = [answer.error?.message ?? `${response.status}`];
    for (const [id, node] of Object.entries(answer.node_errors ?? {})) {
      for (const e of node.errors) lines.push(`  ${id} (${graph[id]?._meta?.title ?? node.class_type}): ${e.message}${e.details ? " — " + e.details : ""}`);
    }
    fail(lines.join("\n"));
  }
  const id = answer.prompt_id;
  console.error(`задание ${id}`);
  let entry;
  while (!(entry = (await json("GET", `/history/${id}`))[id])) {
    const status = await json("GET", "/tool/status");
    const step = status.current?.prompt_id === id && status.current.step ? ` шаг ${status.current.step}/${status.current.steps}` : "";
    process.stderr.write(`\r${status.current?.prompt_id === id ? "идёт" : "в очереди"}${step}      `);
    await sleep(1000);
  }
  process.stderr.write("\r");
  if (entry.status.status_str !== "success") {
    const errors = entry.status.messages.filter(([kind]) => kind === "execution_error")
      .map(([, m]) => `${m.node_id} (${graph[m.node_id]?._meta?.title ?? m.node_type}): ${m.exception_message.trim()}`);
    fail(`ошибка выполнения:\n${errors.join("\n") || "ComfyUI не выполнил граф"}`);
  }
  // Every file a node reports goes to the output folder, and the whole report next to them
  const out = String(options.out ?? "out");
  await fs.mkdir(out, { recursive: true });
  const saved = [];
  for (const [nodeId, outputs] of Object.entries(entry.outputs)) {
    for (const value of Object.values(outputs)) {
      if (!Array.isArray(value)) continue;
      for (const item of value) {
        if (!item?.filename || item.type === "temp") continue;
        const query = new URLSearchParams({ filename: item.filename, subfolder: item.subfolder ?? "", type: item.type ?? "output" });
        const data = Buffer.from(await (await request("GET", `/view?${query}`)).arrayBuffer());
        const name = saved.includes(item.filename) ? `${nodeId}-${item.filename}` : item.filename;
        await fs.writeFile(path.join(out, name), data);
        saved.push(name);
      }
    }
  }
  const report = Object.fromEntries(Object.entries(entry.outputs).map(([nodeId, o]) => [nodeId, { title: graph[nodeId]?._meta?.title, ...o }]));
  await fs.writeFile(path.join(out, "outputs.json"), JSON.stringify({ prompt_id: id, outputs: report }, null, 2));
  console.log(`${out}/: ${saved.join(", ") || "файлов нет"}, outputs.json`);
}

// --- training

async function train(name, folder) {
  const files = (await walk(folder)).filter((f) => !f.includes("/"));
  const images = files.filter((f) => IMAGES.has(path.extname(f).toLowerCase()));
  if (!images.length) fail(`${folder}: нет картинок (${[...IMAGES].join(", ")})`);
  const problems = [], entries = [];
  for (const image of images) {
    const caption = image.slice(0, -path.extname(image).length) + ".txt";
    if (!files.includes(caption)) { problems.push(`${image}: нет подписи ${caption}`); continue; }
    const text = (await fs.readFile(path.join(folder, caption), "utf8")).trim();
    if (!text) problems.push(`${caption}: пустая подпись`);
    else if (options.trigger && !text.startsWith(options.trigger)) problems.push(`${caption}: начинается не с «${options.trigger}»`);
    entries.push({ name: image, data: await fs.readFile(path.join(folder, image)) }, { name: caption, data: Buffer.from(text) });
  }
  if (problems.length) fail(problems.join("\n"));
  const form = new FormData();
  form.append("name", name);
  form.append("params", typeof options.params === "string" ? options.params : "{}");
  form.append("dataset", new Blob([zip(entries)]), "dataset.zip");
  const response = await request("POST", "/tool/train", form);
  const job = await response.json().catch(() => ({}));
  if (!response.ok) fail((job.errors ?? [`${response.status}`]).join("\n"));
  console.log(`обучение ${job.version}: ${job.images} картинок, ${job.steps} шагов, задание ${job.job_id}`);
  if (options["no-wait"]) return;
  // ComfyUI restarts once the training starts, so the server may not answer for a while
  for (;;) {
    await sleep(10000);
    let state;
    try { state = await (await fetch(`${BASE}/tool/jobs/${job.job_id}`)).json(); } catch { continue; }
    if (state.errors) continue;
    process.stderr.write(`\r${state.state} ${state.step ?? 0}/${state.steps}${state.loss ? ` loss ${state.loss}` : ""}      `);
    if (!["starting", "training"].includes(state.state)) {
      process.stderr.write("\n");
      print(state);
      if (state.state !== "done") process.exit(1);
      return;
    }
  }
}

// --- commands

const [command, ...rest] = args;
const commands = {
  help: async () => console.log(HELP),
  status: async () => print(await json("GET", "/tool/status")),
  logs: async () => {
    const query = new URLSearchParams({ service: rest[0] ?? "comfyui", lines: String(options.lines ?? 200) });
    if (options.job) query.set("job", options.job);
    const response = await request("GET", `/tool/logs?${query}`);
    const text = await response.text();
    if (!response.ok) fail(text);
    process.stdout.write(text);
  },
  restart: async () => print(await json("POST", "/tool/restart")),
  cleanup: async () => {
    if (options["older-days"] === undefined) fail("cleanup --older-days N: удалить старше N дней, 0 — всё");
    print(await json("POST", "/tool/cleanup", {
      older_than_days: Number(options["older-days"]),
      ...(options.folders ? { folders: String(options.folders).split(",") } : {}),
    }));
  },
  nodes: async () => print(await json("GET", "/tool/nodes")),
  "push-nodes": async () => {
    const [project, folder] = rest;
    if (!project || !folder) fail("push-nodes <проект> <папка пакета>");
    const names = await walk(folder, (n) => n === "__pycache__" || n.startsWith("."));
    const files = await Promise.all(names.map(async (name) => ({ name, data: await fs.readFile(path.join(folder, name)) })));
    const form = new FormData();
    form.append("package", new Blob([zip(files)]), `${project}.zip`);
    const response = await request("POST", `/tool/nodes/${project}`, form);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) fail((data.errors ?? [`${response.status}`]).join("\n"));
    console.log(`${project}: ${data.nodes.join(", ")}\nв интерфейсе — обнови страницу`);
  },
  "rm-nodes": async () => print(await json("DELETE", `/tool/nodes/${rest[0] ?? fail("rm-nodes <проект>")}`)),
  workflows: async () => print(await json("GET", "/tool/workflows")),
  workflow: async () => {
    const graph = await json("GET", `/tool/workflows/${rest[0] ?? fail("workflow <имя>")}`);
    if (options.out) await fs.writeFile(String(options.out), JSON.stringify(graph, null, 2) + "\n");
    else print(graph);
  },
  pull: async () => {
    // The newest run queued from the UI: it carries both the graph for the UI and the one for the API
    const name = rest[0] ?? fail("pull <имя>");
    const history = await json("GET", "/history?max_items=50");
    const runs = Object.values(history).filter((e) => e.prompt?.[3]?.extra_pnginfo?.workflow).sort((a, b) => b.prompt[0] - a.prompt[0]);
    if (!runs.length) fail("в истории нет запусков из интерфейса: собери граф и нажми Run");
    const dir = String(options.dir ?? "workflows");
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(dir, `${name}.ui.json`), JSON.stringify(runs[0].prompt[3].extra_pnginfo.workflow, null, 2) + "\n");
    await fs.writeFile(path.join(dir, `${name}.api.json`), JSON.stringify(runs[0].prompt[2], null, 2) + "\n");
    console.log(`${dir}/${name}.ui.json, ${dir}/${name}.api.json`);
  },
  push: async () => {
    const file = rest[0] ?? fail("push <граф.ui.json>");
    const name = options.name ?? path.basename(file).replace(/(\.ui)?\.json$/, "");
    const response = await request("POST", `/userdata/${encodeURIComponent(`workflows/${name}.json`)}?overwrite=true`,
      await fs.readFile(file), { "Content-Type": "application/json" });
    if (!response.ok) fail(`${response.status} ${await response.text()}`);
    console.log(`в интерфейсе: Workflows → ${name}.json`);
  },
  check: async () => {
    const problems = await check(await readGraph(rest[0] ?? fail("check <граф.api.json>")));
    if (problems.length) fail(problems.join("\n"));
    console.log("всё есть на сервере");
  },
  run: async () => run(rest[0] ?? fail("run <граф.api.json>")),
  train: async () => {
    const [name, folder] = rest;
    if (!name || !folder) fail("train <проект/имя> <папка эталонов>");
    await train(name, folder);
  },
  lora: async () => {
    const [action, name, file] = rest;
    if (action === "list") return print(await json("GET", "/tool/loras"));
    if (!name) fail("lora list | push <проект/имя> <файл> | pull <проект/имя> [файл] | rm <проект/имя>");
    if (action === "rm") return print(await json("DELETE", `/tool/loras/${name}`));
    if (action === "pull") {
      const response = await request("GET", `/tool/loras/${name}`);
      if (!response.ok) fail(`нет LoRA ${name}`);
      const target = file ?? `${path.basename(name)}.safetensors`;
      await fs.writeFile(target, Buffer.from(await response.arrayBuffer()));
      return console.log(target);
    }
    if (action === "push") {
      if (!file) fail("lora push <проект/имя> <файл>");
      const form = new FormData();
      form.append("name", name);
      form.append("file", new Blob([await fs.readFile(file)]), path.basename(file));
      const response = await request("POST", `/tool/loras${options.overwrite ? "?overwrite=true" : ""}`, form);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) fail((data.errors ?? [`${response.status}`]).join("\n").replace("?overwrite=true", "--overwrite"));
      return print(data);
    }
    fail(`lora ${action}: list, push, pull или rm`);
  },
  update: async () => {
    const response = await request("GET", "/tool/client.mjs");
    if (!response.ok) fail(`${response.status}`);
    const self = fileURLToPath(import.meta.url);
    await fs.writeFile(self, await response.text());
    console.log(`${self} — версия сервера ${BASE}`);
  },
};

await (commands[command] ?? commands.help)();
