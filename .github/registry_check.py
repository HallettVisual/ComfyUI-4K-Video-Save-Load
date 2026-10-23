"""Check a ComfyUI node pack against what the Comfy Registry's security review flags.

    python registry_check.py scan  <pack folder>     before every release
    python registry_check.py status <registry id>    after a release, with the reasons

A flagged version can't be installed from ComfyUI Manager, only as "nightly" from git.
What gets flagged (learned from the 4K Video Save Load and Smart Upscaler packs, 2026-10):
  - any os.environ / getenv read            -> python_environment_manipulation
  - files reachable outside ComfyUI folders -> reviewer: "policy-v0.5: arbitrary-file-read"
  - file names built from request/node data -> reviewer: "policy-v0.5: path-traversal"
  - subprocess                              -> info only (allowed: argument lists, no shell)
"""
import json
import os
import re
import sys
import urllib.request

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".github", "tests", "test", "docs", "examples", "workflows"}

# (severity, label, regex). BLOCK fails the check.
RULES = [
    ("BLOCK", "environment variable read (flagged: python_environment_manipulation)",
     r"os\.environ|os\.getenv|os\.putenv|getenv\("),
    ("BLOCK", "shell command (os.system / shell=True)", r"os\.system\(|os\.popen\(|shell\s*=\s*True"),
    ("BLOCK", "dynamic code (eval / exec / compile / __import__)", r"(?<![\w.])(eval|exec|compile)\(|__import__\("),
    ("BLOCK", "unsafe deserialisation (pickle / marshal)", r"pickle\.loads?\(|marshal\.loads?\("),
    ("BLOCK", "installs packages at runtime", r"pip(\.main|\s+install)|ensurepip|-m['\"],?\s*['\"]pip"),
    ("WARN", "network access (should be none, or user-initiated model download only)",
     r"requests\.(get|post)|urllib\.request|http\.client|socket\.socket|ClientSession\(|https?://(?!github\.com/HallettVisual)"),
    ("INFO", "subprocess (allowed as an argument list, without a shell)", r"subprocess\.(run|Popen|call|check_output)"),
]
CONTAINMENT = re.compile(r"is_within_directory|commonpath|get_annotated_filepath.*contained|contained\(")


def shipped_files(root):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith((".py", ".js")):
                yield os.path.join(base, f)


def scan(root):
    blocks = warns = 0
    texts = {}
    for path in shipped_files(root):
        rel = os.path.relpath(path, root)
        text = open(path, encoding="utf-8", errors="replace").read()
        texts[rel] = text
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0] if path.endswith(".py") else line.split("//", 1)[0]
            for severity, label, pattern in RULES:
                if re.search(pattern, code):
                    print(f"  {severity:5s} {rel}:{n}  {label}\n         {line.strip()[:110]}")
                    blocks += severity == "BLOCK"
                    warns += severity == "WARN"

    # Anything that turns outside data into a file path needs a containment check.
    py = {k: v for k, v in texts.items() if k.endswith(".py")}
    contained = any(CONTAINMENT.search(t) for t in py.values())
    for rel, text in py.items():
        routes = re.findall(r"routes\.(get|post|put|delete)\(\s*[\"']([^\"']+)", text)
        reads_query = re.search(r"rel_url\.query|request\.query|await request\.(post|json)\(", text)
        for method, url in routes:
            ok = contained and reads_query
            print(f"  {'INFO' if ok or not reads_query else 'BLOCK':5s} {rel}  web route {method.upper()} {url}"
                  + ("" if not reads_query else " reads request data; " + ("a containment check exists in the pack" if contained
                     else "NO containment check -> arbitrary-file-read / path-traversal")))
            blocks += bool(reads_query and not contained)
        path_inputs = re.findall(r"[\"'](\w*(?:path|folder|dir|directory|file|filename)\w*)[\"']\s*:\s*\(\s*[\"']STRING", text)
        for name in path_inputs:
            if not contained:
                print(f"  BLOCK {rel}  STRING input '{name}' is used as a file path with no containment check")
                blocks += 1
    if contained:
        print("  OK    containment helper found (folder_paths.is_within_directory or equivalent)")
    print(f"\n{blocks} blocking, {warns} warnings  ->  {'NOT ready for the Registry' if blocks else 'ready to release'}")
    return 1 if blocks else 0


def status(node_id):
    url = f"https://api.comfy.org/nodes/{node_id}/versions?include_status_reason=true"
    versions = json.load(urllib.request.urlopen(url, timeout=30))
    versions = versions if isinstance(versions, list) else versions.get("versions", [])

    def findings(raw):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if isinstance(data, dict):
            print(f"    reviewer: {data.get('message')}")
            for h in data.get("statusHistory", []):
                yield from findings(h.get("message"))
        elif isinstance(data, list):
            yield from (f for f in data if isinstance(f, dict))

    for v in versions[:6]:
        print(f"{v['version']:>8}  {v['status'].replace('NodeVersionStatus', '')}")
        seen = set()
        for f in findings(v.get("status_reason") or "[]"):
            key = (f.get("issue_type"), f.get("file_path"), f.get("line_number"))
            if key not in seen:
                seen.add(key)
                print(f"    {f.get('severity', '?'):5s} {f.get('issue_type')}  {f.get('file_path')}:{f.get('line_number')}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("scan", "status"):
        sys.exit(__doc__)
    sys.exit(scan(sys.argv[2]) if sys.argv[1] == "scan" else status(sys.argv[2]))
