# ── PythonAnywhere WSGI 설정 예시 ────────────────────────────────
# 이 파일을 그대로 쓰는 게 아니라, PythonAnywhere의 [Web] 탭에 있는
# "WSGI configuration file"(예: /var/www/USERNAME_pythonanywhere_com_wsgi.py)
# 을 열어 기존 내용을 지우고 아래 내용을 붙여넣은 뒤, USERNAME 과 경로만 수정하세요.

import sys

project_home = "/home/USERNAME/toy-workshop-flask"   # ← 본인 경로로 수정
if project_home not in sys.path:
    sys.path.insert(0, project_home)

from app import app as application   # noqa: F401  (WSGI는 변수명이 application 이어야 함)
