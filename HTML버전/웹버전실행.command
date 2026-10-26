#!/bin/zsh
set -e
SCRIPT_DIR="${0:A:h}"
for PYTHON_PATH in /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if [[ -x "$PYTHON_PATH" ]] && "$PYTHON_PATH" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    exec "$PYTHON_PATH" "$SCRIPT_DIR/launch.py" "$@"
  fi
done
print "Python 3.10 이상을 설치한 뒤 다시 실행해 주세요."
read -r "REPLY?엔터를 눌러 닫기"
