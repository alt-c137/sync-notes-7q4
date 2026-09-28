"""
Проверка и починка копии бота на ПК — запускается сама при старте панели.

Если компьютер выключили резко (свет, кнопка, зависание), служебные файлы git могут остаться
недописанными — тогда бот на ПК не может ни скачать обновления, ни отправить состояние.
Здесь это находится и чинится само. Настройки (.env) и ваши файлы не трогаются никогда:
всё испорченное не удаляется, а откладывается в папку .git/broken-<дата>.

Также включает «запись сразу на диск» для git (core.fsync), чтобы такое случалось реже.
Запуск вручную: python repo_doctor.py
"""

import os
import re
import shutil
import subprocess
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
GIT = os.path.join(ROOT, ".git")
SHA = re.compile(r"^[0-9a-f]{40}$")


def git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def stash_aside(path, report):
    """Не удаляем — откладываем испорченный файл в .git/broken-<дата>/."""
    folder = os.path.join(GIT, "broken-" + time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.relpath(path, GIT).replace(os.sep, "__"))
    shutil.move(path, target)
    report.append(f"🛠 Испорченный файл {os.path.relpath(path, ROOT)} отложен в {os.path.relpath(folder, ROOT)}")


def bad_text(path):
    """Файл пустой, из нулей или с мусором вместо текста."""
    with open(path, "rb") as f:
        data = f.read()
    return not data.strip(b"\x00 \r\n\t") or b"\x00" in data


def check_and_repair():
    report = []
    if not os.path.isdir(GIT):
        return ["⚠️ Папка бота — не копия git (нет .git). Скачайте заново: git clone …"]

    # 1) запись git сразу на диск — меньше шансов испортить файлы при резком выключении
    if git("config", "core.fsync").stdout.strip() != "committed":
        git("config", "core.fsync", "committed")
        git("config", "core.fsyncMethod", "fsync")

    # 2) .git/shallow — список «обрезанных» коммитов; бывает забит нулями
    shallow = os.path.join(GIT, "shallow")
    if os.path.exists(shallow):
        with open(shallow, "rb") as f:
            lines = f.read().split(b"\n")
        if any(line and not SHA.match(line.decode("ascii", "replace").strip()) for line in lines) or bad_text(shallow):
            stash_aside(shallow, report)

    # 3) ссылки на ветки (.git/refs/…) — в каждой должен быть 40-значный номер коммита или «ref: …»
    for folder, _, files in os.walk(os.path.join(GIT, "refs")):
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                text = f.read().decode("utf-8", "replace").strip()
            if not (SHA.match(text) and text != "0" * 40) and not text.startswith("ref: "):
                stash_aside(path, report)

    # 4) HEAD
    head = os.path.join(GIT, "HEAD")
    if not os.path.exists(head) or bad_text(head):
        if os.path.exists(head):
            stash_aside(head, report)
        with open(head, "w") as f:
            f.write("ref: refs/heads/main\n")
        report.append("🛠 Восстановлен .git/HEAD")

    # 5) индекс (список файлов для коммита) — если испорчен, пересобираем; рабочие файлы не меняются
    status = git("status", "--porcelain")
    if status.returncode != 0 and "index" in status.stderr.lower():
        stash_aside(os.path.join(GIT, "index"), report)
        git("reset", "-q")
        report.append("🛠 Пересобран индекс git")

    # 6) целостность истории; не в порядке — берём .git заново с GitHub (рабочие файлы и .env остаются)
    fsck = git("fsck", "--connectivity-only")
    main_ok = git("rev-parse", "--verify", "-q", "refs/heads/main").returncode == 0
    if fsck.returncode != 0 or not main_ok:
        report.append("🛠 История git повреждена — скачиваю её заново с GitHub (ваши файлы и .env не трогаю)…")
        report += reclone()

    # 7) догоняем GitHub
    if git("fetch", "-q", "origin").returncode == 0:
        behind = git("rev-list", "--count", "HEAD..origin/main").stdout.strip()
        if behind not in ("", "0"):
            r = git("merge", "-q", "--ff-only", "origin/main")
            report.append("⬇️ Скачано обновлений: " + behind + ("" if r.returncode == 0 else
                          " (не применились сами: " + r.stderr.strip()[:120] + ")"))
            if r.returncode == 0:
                report.append("ℹ️ Обновление скачано — перезапустите панель, чтобы оно заработало.")
    else:
        report.append("⚠️ Нет связи с GitHub — проверю в следующий раз.")

    # 8) папка ветки черновиков — просто кэш; если испорчена, удаляем (скачается заново сама)
    inbox = os.path.join(ROOT, "inbox-branch")
    if os.path.isdir(inbox) and git("status", "--porcelain", cwd=inbox).returncode != 0:
        shutil.rmtree(inbox, ignore_errors=True)
        report.append("🛠 Папка inbox-branch была испорчена — удалена, скачается заново")

    return report or ["✅ Копия бота на ПК в порядке"]


def reclone():
    url = git("config", "--get", "remote.origin.url").stdout.strip() or "https://github.com/alt-c137/sync-notes-7q4.git"
    tmp = os.path.join(ROOT, ".git-fresh")
    shutil.rmtree(tmp, ignore_errors=True)
    r = subprocess.run(["git", "clone", "-q", "--no-checkout", url, tmp], capture_output=True, text=True)
    if r.returncode != 0:
        return ["⚠️ Не получилось скачать историю с GitHub: " + r.stderr.strip()[:200]]
    backup = GIT + ".broken-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.move(GIT, backup)
    shutil.move(os.path.join(tmp, ".git"), GIT)
    shutil.rmtree(tmp, ignore_errors=True)
    git("reset", "-q")   # индекс по новой истории; рабочие файлы не меняются
    return [f"✅ История git скачана заново. Старая отложена в {os.path.basename(backup)}"]


if __name__ == "__main__":
    for line in check_and_repair():
        print(line)
