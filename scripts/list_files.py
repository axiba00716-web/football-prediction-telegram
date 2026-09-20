"""列出将通过 API 推送的文件（dry-run 检查用）。"""
import os
from pathlib import Path

INCLUDE = {
    ".env.example", ".gitignore", "Dockerfile", "railway.toml",
    "requirements.txt", "README.md", "LICENSE",
    "verify_all.py", "verify_db.py", "scripts/pre-commit-check.sh",
}
EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".git", "*.db", ".env", "scripts"}


def should_include(rel):
    if any(part in EXCLUDED_NAMES for part in Path(rel).parts):
        return False
    if rel in INCLUDE or rel.startswith("app/") or rel.startswith("tests/"):
        return True
    if rel.startswith("scripts/") and rel.endswith((".py", ".sh")):
        return True
    return False


files = []
for p in Path(".").rglob("*"):
    if not p.is_file():
        continue
    rel = str(p).replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    if should_include(rel):
        files.append(rel)

for f in sorted(files):
    print(f)
print(f"\n共 {len(files)} 个文件")
