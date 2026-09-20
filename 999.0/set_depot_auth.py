# -*- coding: utf-8 -*-
"""交互式写入笔记服务的 depot 登录凭据文件（密码不回显、不进仓库）。

    py_312/python.exe set_depot_auth.py
"""
import getpass
import json
import os
from pathlib import Path

p = Path.home() / ".lugwit" / "l_notepad_server" / "depot_auth.json"
user = input("lugwit 账号: ").strip()
pwd = getpass.getpass("lugwit 密码: ")
if not (user and pwd):
    raise SystemExit("账号/密码不能为空")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(
    json.dumps({"lugwit_user": user, "lugwit_password": pwd},
               ensure_ascii=False, indent=2),
    encoding="utf-8")
try:
    os.chmod(p, 0o600)
except OSError:
    pass
print("written:", p)
print("生效方式：刷新知识库页面重新触发索引即可（无需重启笔记服务）。")
