#!/usr/bin/env bash
#
# Script to setup workstation for ansible
# so we can actually install everything
# from scratch.

sudo 2>/dev/null 1>&2 apt install -y python3-pip sshpass
2>/dev/null 1>&2 pip install ansible pre-commit

if [[ ! $PATH =~ ".local/bin" ]]
then
  SHELL_NAME=$(basename $SHELL)
  echo "export PATH=\"\$PATH:\$HOME/.local/bin\"" >> "$HOME/.${SHELL_NAME}rc"
  echo "export PATH=\"\$PATH:\$HOME/.local/bin\""
fi

2>/dev/null 1>&2 pre-commit install
