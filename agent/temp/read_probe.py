# -*- coding: utf-8 -*-
import io, sys, json, os, re
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 1) 看已有的 sso_probe.json
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sso_probe.json')
if os.path.exists(p):
    txt = io.open(p, encoding='utf-8', errors='replace').read()
    print('=== sso_probe.json (%d chars) ===' % len(txt))
    print(txt[:4000])
else:
    print('sso_probe.json not found at', p)
