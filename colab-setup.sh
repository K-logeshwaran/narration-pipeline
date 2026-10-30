#!/usr/bin/env bash
set -e
apt-get update -qq
apt-get install -y -qq tmux ripgrep fd-find git curl unzip build-essential
ln -sf "$(which fdfind)" /usr/local/bin/fd

# Neovim (the apt version is too old for most configs)
curl -fsSL https://github.com/neovim/neovim/releases/latest/download/nvim-linux-x86_64.tar.gz | tar -xz -C /opt
ln -sf /opt/nvim-linux-x86_64/bin/nvim /usr/local/bin/nvim

# Your nvim config
[ -d ~/.config/nvim ] || git clone https://github.com/K-logeshwaran/nvim-config.git ~/.config/nvim

# Python env for the pipeline
pip install -q uv
uv venv --python 3.12 /content/venv
grep -v -E '^(torch|nvidia-|triton)' requirements.txt > /tmp/req_colab.txt
uv pip install --python /content/venv/bin/python -r /tmp/req_colab.txt
echo "setup done"
