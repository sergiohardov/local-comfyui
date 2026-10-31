# Node packages of projects, sent over the API instead of living in the tool: a project keeps a standard ComfyUI
# node package (__init__.py with NODE_CLASS_MAPPINGS) and uploads it; it lands in project_nodes/<project>/ and its
# nodes join ComfyUI as <project>.<node> without a restart. The folder is a cache: on start every package in it
# is registered again, and a project can always send its package anew.
import importlib.util
import io
import shutil
import sys
import time
import traceback
import zipfile
from pathlib import Path

import nodes

ROOT = Path("/opt/comfyui/project_nodes")
MAX_PACKAGE_BYTES = 50 * 2**20
# Names of the nodes each project has registered, to take them out when its package is replaced
registered = {}
# Every class registered under a name so far: ComfyUI keeps the node instances it made, by node and name, and
# calls them again, so the old classes are pointed at the new code
earlier = {}


def module_name(project):
    return "project_nodes_" + project.replace("-", "_")


def unregister(project):
    for key in registered.pop(project, []):
        nodes.NODE_CLASS_MAPPINGS.pop(key, None)
        nodes.NODE_DISPLAY_NAME_MAPPINGS.pop(key, None)
    for name in [m for m in sys.modules if m == module_name(project) or m.startswith(module_name(project) + ".")]:
        del sys.modules[name]


def stamped(cls, stamp):
    # ComfyUI keeps a node's result while its inputs stay the same; after new code arrives the same inputs must give
    # a new result, so the stamp of the upload joins whatever the node itself says about being changed
    own = getattr(cls, "IS_CHANGED", None)

    def is_changed(_cls, *args, **kwargs):
        return stamp, own(*args, **kwargs) if own else None

    cls.IS_CHANGED = classmethod(is_changed)


def forward(old_classes, new):
    # An instance kept from before runs the function of the new class, on a fresh instance of it
    function = new.FUNCTION

    def run(self, *args, **kwargs):
        return getattr(new(), function)(*args, **kwargs)

    for old in old_classes:
        setattr(old, function, run)


def register(project):
    # Imports the package from its folder and puts its nodes into ComfyUI; returns their names, or raises with
    # the error of the package
    folder = ROOT / project
    unregister(project)
    name = module_name(project)
    spec = importlib.util.spec_from_file_location(name, folder / "__init__.py", submodule_search_locations=[str(folder)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        mappings = getattr(module, "NODE_CLASS_MAPPINGS", None)
        if not isinstance(mappings, dict) or not mappings:
            raise ValueError("в __init__.py пакета нет NODE_CLASS_MAPPINGS с узлами")
    except BaseException:
        unregister(project)
        raise
    titles = getattr(module, "NODE_DISPLAY_NAME_MAPPINGS", {})
    stamp = time.time()
    keys = []
    for key, cls in mappings.items():
        full = f"{project}.{key}"
        stamped(cls, stamp)
        forward(earlier.get(full, []), cls)
        earlier.setdefault(full, []).append(cls)
        cls.RELATIVE_PYTHON_MODULE = f"project_nodes.{project}"
        nodes.NODE_CLASS_MAPPINGS[full] = cls
        nodes.NODE_DISPLAY_NAME_MAPPINGS[full] = f"{titles.get(key, key)} ({project})"
        keys.append(full)
    registered[project] = keys
    return keys


def error_text():
    # The end of the traceback: where in the package it broke and why
    return "".join(traceback.format_exc(limit=-4)).strip()


def check_zip(data):
    # A zip of the package folder: relative paths only, nothing outside it, __init__.py at the top
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None, "package: не zip-архив"
    names = [i.filename for i in archive.infolist() if not i.is_dir()]
    if any(n.startswith("/") or "\\" in n or ".." in Path(n).parts or ":" in n for n in names):
        return None, "package: пути в архиве — только внутри папки пакета"
    if "__init__.py" not in names:
        return None, "package: в корне архива нет __init__.py"
    if sum(i.file_size for i in archive.infolist()) > MAX_PACKAGE_BYTES:
        return None, "package: распакованный пакет больше 50 МБ"
    return archive, None


def install(project, archive):
    # The new package replaces the old one only if it registers; otherwise the old one comes back as it was
    ROOT.mkdir(parents=True, exist_ok=True)
    folder, new, old = ROOT / project, ROOT / f".{project}.new", ROOT / f".{project}.old"
    for path in (new, old):
        shutil.rmtree(path, ignore_errors=True)
    archive.extractall(new)
    if folder.exists():
        folder.rename(old)
    new.rename(folder)
    try:
        keys = register(project)
    except BaseException:
        error = error_text()
        shutil.rmtree(folder, ignore_errors=True)
        if old.exists():
            old.rename(folder)
            try:
                register(project)
            except BaseException:
                pass
        return None, error
    shutil.rmtree(old, ignore_errors=True)
    return keys, None


def remove(project):
    unregister(project)
    shutil.rmtree(ROOT / project, ignore_errors=True)


def register_all():
    # On start: every package left from before, each on its own, so one broken package does not stop the others
    if not ROOT.exists():
        return
    for folder in sorted(p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            keys = register(folder.name)
            print(f"[local-comfyui] узлы проекта {folder.name}: {', '.join(keys)}", flush=True)
        except BaseException:
            print(f"[local-comfyui] пакет проекта {folder.name} не загрузился:\n{error_text()}", flush=True)
