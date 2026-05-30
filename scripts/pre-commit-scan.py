#!/usr/bin/env python3
"""Pre-commit: scan staged files for secrets before allowing commit."""
import os, re, subprocess, sys

SAFE_EMAILS = {
    'xxx@icloud.com', 'yourname@icloud.com', 'alias@icloud.com',
    'example@icloud.com', 'test@icloud.com', 'user@icloud.com',
    'botch.sear_8w@icloud.com',
}

RULES = [
    (r'ghp_[A-Za-z0-9]{36}', 'GitHub Token'),
    (r'[\'"](?:N2Ml4PT|qYDJOV)[A-Za-z0-9]+[\'"]', 'SMSBower Key'),
    (r'[a-zA-Z0-9._%+-]+@(icloud|qq|gmail|163|126|outlook)\.com', 'personal email'),
    (r'(?:password|passwd|pwd)\s*[:=]\s*[\'"](?!YOUR_)(?!\s*[\'"])\S{4,}[\'"]', 'hardcoded password'),
]

staged = subprocess.check_output(
    ['git', 'diff', '--cached', '--name-only', '--diff-filter=ACM'],
    text=True
).strip().split('\n')
staged = [f for f in staged if f and os.path.isfile(f)]

found = 0
for f in staged:
    if f in ('.gitignore', 'config.example.json'):
        continue
    try:
        for i, line in enumerate(open(f, encoding='utf-8', errors='ignore'), 1):
            if 'YOUR_' in line:
                continue
            for pat, name in RULES:
                m = re.search(pat, line)
                if m:
                    val = m.group(0).lower()
                    if name == 'personal email' and (val in SAFE_EMAILS or val.startswith('xxx') or val.startswith('your')):
                        continue
                    print(f'BLOCKED: {f}:{i} — {name}: {m.group(0)[:60]}')
                    found += 1
    except Exception:
        pass

if found:
    print(f'\n{found} sensitive item(s) found. Commit blocked.')
    sys.exit(1)

print('pre-commit scan passed')
sys.exit(0)
