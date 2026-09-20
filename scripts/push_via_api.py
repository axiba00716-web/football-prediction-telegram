"""通过 GitHub REST API 推送当前工作区到 main 分支（绕过 git:// 出站限制）。

流程：base_sha → 逐文件创建 blob → 构建 tree → 创建 commit → 更新 ref。
用法：
    GITHUB_TOKEN=xxx REPO=owner/name python3 scripts/push_via_api.py
"""
import os
import sys
import json
import base64
import urllib.request
import urllib.error
from pathlib import Path

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ.get("REPO", "axiba00716-web/football-prediction-telegram")
API = "https://api.github.com/repos/" + REPO
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "Content-Type": "application/json",
}


def api(method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        print(f"  [{method} {path}] HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
        raise


# 要推送的文件：相对路径 -> API 树路径
INCLUDE = {
    ".env.example", ".gitignore", "Dockerfile", "railway.toml",
    "requirements.txt", "README.md", "LICENSE",
    "verify_all.py", "verify_db.py",
    "scripts/pre-commit-check.sh",
}
APP = {f"app/{p}" for p in os.listdir("app") if p.endswith(".py")}
TESTS = {f"tests/{p}" for p in os.listdir("tests") if p.endswith(".py")}
SCRIPTS = {f"scripts/{p}" for p in os.listdir("scripts") if p.endswith((".py", ".sh"))}
TARGETS = INCLUDE | APP | TESTS | SCRIPTS

# 排除项
EXCLUDE = {".env", "*.db", "package_marker.py", "# package marker"}
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".git", "*.db", ".env"}


def should_include(rel: str) -> bool:
    if any(part in EXCLUDED_NAMES for part in Path(rel).parts):
        return False
    if rel in TARGETS or rel.startswith("app/") or rel.startswith("tests/"):
        return True
    return False


def collect_files():
    files = {}
    for p in Path(".").rglob("*"):
        if not p.is_file():
            continue
        rel = str(p).replace("\\", "/")
        if rel.startswith("./"):
            rel = rel[2:]
        if not should_include(rel):
            continue
        content = p.read_bytes()
        # .gitignore 等含特殊字符，统一 base64 编码
        encoded = base64.b64encode(content).decode()
        files[rel] = encoded
    return files


def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. 获取 base commit SHA
    branch = api("GET", "/branches/main")
    base_sha = branch["commit"]["sha"]
    base_tree_sha = branch["commit"]["commit"]["tree"]["sha"]
    print(f"base commit: {base_sha[:7]}  tree: {base_tree_sha[:7]}")

    # 2. 收集文件并创建 blobs
    files = collect_files()
    print(f"待提交文件: {len(files)} 个")
    tree_items = []
    for rel, encoded in sorted(files.items()):
        blob = api("POST", "/git/blobs", {"content": encoded, "encoding": "base64"})
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  + {rel}  ({blob['sha'][:7]})")

    # 3. 创建 tree
    tree = api("POST", "/git/trees", {"base_tree": base_tree_sha, "tree": tree_items})
    print(f"新 tree: {tree['sha'][:7]}")

    # 4. 创建 commit
    msg = os.environ.get("COMMIT_MSG", "Fix prediction data model and production deployment")
    commit = api("POST", "/git/commits", {
        "message": msg,
        "tree": tree["sha"],
        "parents": [base_sha],
    })
    new_sha = commit["sha"]
    print(f"新 commit: {new_sha[:7]}  <-  {msg}")

    # 5. 更新 ref（force 仅在同一分支内更新，非跨分支）
    api("PATCH", "/git/refs/heads/main", {"sha": new_sha, "force": False})
    print(f"\n✅ 已推送到 main: {new_sha}")
    print(f"   https://github.com/{REPO}/commit/{new_sha}")


if __name__ == "__main__":
    main()
