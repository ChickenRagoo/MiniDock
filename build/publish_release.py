"""Публикует релиз на GitHub: тег vX.Y.Z и установщик из папки release — после него у всех срабатывает автообновление.

python build/publish_release.py ChickenRagoo/MiniDock v1.3.1 release/MiniDock-Setup-1.3.1.exe "Что нового"
Токен берётся из сохранённых учётных данных git (тех же, что для git push) и никуда не выводится.
"""
import json
import subprocess
import sys
import urllib.request

repo, tag, path, notes = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                     capture_output=True, text=True, env={**__import__("os").environ, "GIT_TERMINAL_PROMPT": "0"}).stdout
token = next(l.split("=", 1)[1] for l in out.splitlines() if l.startswith("password="))
H = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "MiniDock-release"}


def call(method, url, data=None, ctype="application/json"):
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, method=method, headers={**H, "Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


rel = call("POST", f"https://api.github.com/repos/{repo}/releases",
           {"tag_name": tag, "target_commitish": "main", "name": f"MiniDock {tag.lstrip('v')}", "body": notes,
            "draft": False, "prerelease": False})
name = path.replace("\\", "/").rsplit("/", 1)[-1]
with open(path, "rb") as f:
    asset = call("POST", f"https://uploads.github.com/repos/{repo}/releases/{rel['id']}/assets?name={name}",
                 f.read(), "application/octet-stream")
print("release:", rel["html_url"])
print("asset:", asset["name"], asset["size"], asset.get("digest"))
